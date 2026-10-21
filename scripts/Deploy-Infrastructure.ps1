[CmdletBinding()]
param(
    [string]$Subscription,
    [string]$Location = "eastus2",
    [string]$Environment = "dev",
    [string]$ProjectName = "ach-remit",
    [string]$ResourceGroup,
    [string]$DevPrincipalId = "",

    # App Service plan SKU for the Function. B1 is fine for single uploads; use S1/P1v3 for load tests.
    [string]$FunctionPlanSku = "B1",
    # Max concurrent Durable activities per Function instance (throttles CU / agent calls).
    [int]$MaxConcurrentActivities = 8,
    # Comma-separated names of the receiving org (payee) for payer-vs-payee resolution; kept out of the
    # repo - defaults from the RECEIVING_ORG_ALIASES environment variable.
    [string]$ReceivingOrgAliases = $env:RECEIVING_ORG_ALIASES,
    # The customer's PRO rules (Function app settings PRO_*); default from .env / the environment. Empty
    # means neutral rules (6-12 digits, no suffixes).
    [string]$ProMinDigits = $env:PRO_MIN_DIGITS,
    [string]$ProMaxDigits = $env:PRO_MAX_DIGITS,
    [string]$ProStripSuffixes = $env:PRO_STRIP_SUFFIXES,
    [string]$ProSuffixExamples = $env:PRO_SUFFIX_EXAMPLES,

    # Private networking: restrict Storage to private endpoints and VNet-integrate the Function and
    # Container Apps against a pre-provisioned landing-zone VNet + central private DNS zones.
    [switch]$Private,
    [string]$LandingZoneTag = "role=private-landing-zone",
    # Explicit subnet overrides (otherwise resolved / created in the tagged landing zone).
    [string]$FunctionSubnetResourceId = "",
    [string]$PrivateEndpointSubnetResourceId = "",
    [string]$AcaSubnetResourceId = "",
    # With -Private: the UI is reachable only from the VNet (e.g. the landing-zone VPN) - a private
    # endpoint for the Container Apps environment and public access disabled. Creates the
    # privatelink.<region>.azurecontainerapps.io zone (and its VNet link) in the landing zone if missing.
    [switch]$PrivateUi,

    [switch]$WhatIf
)

$ErrorActionPreference = "Stop"
Import-Module (Join-Path $PSScriptRoot "common/DeploymentFunctions.psm1") -Force
# Run on its own, the phase still reads .env; param defaults bind before this, so re-resolve them.
Import-DotEnv
if ([string]::IsNullOrEmpty($ReceivingOrgAliases)) { $ReceivingOrgAliases = $env:RECEIVING_ORG_ALIASES }
if ([string]::IsNullOrEmpty($ProMinDigits)) { $ProMinDigits = $env:PRO_MIN_DIGITS }
if ([string]::IsNullOrEmpty($ProMaxDigits)) { $ProMaxDigits = $env:PRO_MAX_DIGITS }
if ([string]::IsNullOrEmpty($ProStripSuffixes)) { $ProStripSuffixes = $env:PRO_STRIP_SUFFIXES }
if ([string]::IsNullOrEmpty($ProSuffixExamples)) { $ProSuffixExamples = $env:PRO_SUFFIX_EXAMPLES }
Assert-Command az

$repoRoot = Resolve-RepoRoot
$template = Join-Path $repoRoot "infra/main.bicep"
if (-not $ResourceGroup) { $ResourceGroup = "rg-$ProjectName-$Environment" }
if ($Subscription) { az account set --subscription $Subscription | Out-Null }

# ── Subnet CIDR helpers (private mode: allocate free /26 Function and /23 ACA subnets) ──
function ConvertTo-UInt32Ip([string]$Address) {
    $ip = [System.Net.IPAddress]::Parse($Address)
    if ($ip.AddressFamily -ne [System.Net.Sockets.AddressFamily]::InterNetwork) {
        throw "Subnet allocation requires an IPv4 address, got '$Address'."
    }
    $b = $ip.GetAddressBytes(); [Array]::Reverse($b)
    return [BitConverter]::ToUInt32($b, 0)
}
function ConvertFrom-UInt32Ip([uint32]$Address) {
    $b = [BitConverter]::GetBytes($Address); [Array]::Reverse($b)
    return ([System.Net.IPAddress]::new($b)).ToString()
}
function Get-CidrRange([string]$Cidr) {
    $p = $Cidr.Split('/')
    if ($p.Count -ne 2 -or [int]$p[1] -lt 1 -or [int]$p[1] -gt 30) { throw "Unsupported IPv4 CIDR '$Cidr'." }
    $start = ConvertTo-UInt32Ip $p[0]
    $size = [uint64][math]::Pow(2, 32 - [int]$p[1])
    return @{ Start = [uint64]$start; End = [uint64]$start + $size - 1 }
}
# First aligned /$NewPrefix block inside $VnetCidr that doesn't overlap any $UsedCidrs.
function Get-FreeSubnetCidr([string]$VnetCidr, [string[]]$UsedCidrs, [int]$NewPrefix = 26) {
    $v = Get-CidrRange $VnetCidr
    $used = @($UsedCidrs | Where-Object { $_ } | ForEach-Object { Get-CidrRange $_ })
    $step = [uint64][math]::Pow(2, 32 - $NewPrefix)
    $start = [uint64]([math]::Ceiling($v.Start / $step) * $step)
    for ($s = $start; ($s + $step - 1) -le $v.End; $s += $step) {
        $e = $s + $step - 1
        $overlap = $false
        foreach ($u in $used) { if ($s -le $u.End -and $u.Start -le $e) { $overlap = $true; break } }
        if (-not $overlap) { return "$(ConvertFrom-UInt32Ip ([uint32]$s))/$NewPrefix" }
    }
    throw "No free /$NewPrefix block available in VNet $VnetCidr."
}
# Stable short token so delegated subnet names are unique per deployment within a SHARED landing zone
# (subscription + project + environment + location), yet identical across re-runs so they are reused.
function Get-DeploymentToken([string]$Seed) {
    $sha = [System.Security.Cryptography.SHA256]::Create()
    try { $bytes = $sha.ComputeHash([System.Text.Encoding]::UTF8.GetBytes($Seed)) }
    finally { $sha.Dispose() }
    return (($bytes | ForEach-Object { $_.ToString('x2') }) -join '').Substring(0, 10)
}

if ($Private) {
    Write-Phase "Resolving private landing zone (tag '$LandingZoneTag')"
    $landingZoneRg = az group list --tag $LandingZoneTag --query "[0].name" -o tsv 2>$null
    if (-not $landingZoneRg) { throw "Private networking is enabled but no resource group tagged '$LandingZoneTag' was found." }
    $vnetName = az network vnet list --resource-group $landingZoneRg --query "[0].name" -o tsv 2>$null
    if (-not $vnetName) { throw "No virtual network found in landing zone resource group '$landingZoneRg'." }

    # The Function, Container Apps environment and storage private endpoints are created in -Location,
    # so it MUST match the landing-zone VNet region (a delegated subnet / PE can't bind across regions).
    $vnetLocation = (az network vnet show --resource-group $landingZoneRg --name $vnetName --query location -o tsv 2>$null).Trim()
    if ($Location -ne $vnetLocation) {
        throw "Private mode: -Location '$Location' must match the landing-zone VNet '$vnetName' region '$vnetLocation'."
    }

    # Windows PowerShell 5.1 mangles an 'az ... -o json' array piped into ConvertFrom-Json, so read
    # subnet names as tsv and look up each subnet's delegation/id as scalars.
    $subnetNames = @(az network vnet subnet list --resource-group $landingZoneRg --vnet-name $vnetName --query "[].name" -o tsv 2>$null | Where-Object { $_ })
    if (-not $subnetNames) { throw "Could not list subnets in landing-zone VNet '$vnetName'." }
    function Get-SubnetDelegation([string]$Name) {
        return (az network vnet subnet show --resource-group $landingZoneRg --vnet-name $vnetName --name $Name --query "delegations[0].serviceName" -o tsv 2>$null)
    }
    function Get-SubnetId([string]$Name) {
        return (az network vnet subnet show --resource-group $landingZoneRg --vnet-name $vnetName --name $Name --query "id" -o tsv 2>$null)
    }
    function New-DelegatedSubnet([string]$Name, [int]$Prefix, [string]$Delegation) {
        # Re-query ranges each time so a subnet created earlier in this run counts as used.
        $vnetCidr = az network vnet show --resource-group $landingZoneRg --name $vnetName --query "addressSpace.addressPrefixes[0]" -o tsv 2>$null
        $usedCidrs = @(az network vnet subnet list --resource-group $landingZoneRg --vnet-name $vnetName --query "[].addressPrefix" -o tsv 2>$null)
        $cidr = Get-FreeSubnetCidr -VnetCidr $vnetCidr -UsedCidrs $usedCidrs -NewPrefix $Prefix
        Write-Host "[infra] Creating subnet '$Name' ($cidr) delegated to $Delegation..." -ForegroundColor Yellow
        $id = az network vnet subnet create --resource-group $landingZoneRg --vnet-name $vnetName `
            --name $Name --address-prefixes $cidr --delegations $Delegation --query id -o tsv
        if ($LASTEXITCODE -ne 0 -or -not $id) { throw "Could not create subnet '$Name'." }
        return $id
    }

    $reservedNames = @('GatewaySubnet', 'AzureBastionSubnet', 'AzureFirewallSubnet', 'AzureFirewallManagementSubnet', 'RouteServerSubnet')
    $candidateNames = @($subnetNames | Where-Object { $reservedNames -notcontains $_ })

    # Private endpoint subnet: prefer 'snet-pe', else the first non-delegated candidate.
    if (-not $PrivateEndpointSubnetResourceId) {
        $ordered = @($candidateNames | Where-Object { $_ -eq 'snet-pe' }) + @($candidateNames | Where-Object { $_ -ne 'snet-pe' })
        foreach ($name in $ordered) {
            if (-not (Get-SubnetDelegation $name)) { $PrivateEndpointSubnetResourceId = Get-SubnetId $name; break }
        }
        if (-not $PrivateEndpointSubnetResourceId) { throw "No non-delegated private-endpoint subnet was found in '$vnetName'." }
    }

    $subId = az account show --query id -o tsv 2>$null
    $subnetProject = $ProjectName -replace '[^A-Za-z0-9-]', ''

    # Function subnet: the Dedicated plan's regional VNet integration needs Microsoft.Web/serverFarms.
    if (-not $FunctionSubnetResourceId) {
        $functionSubnetName = "snet-func-$subnetProject-$Environment-$(Get-DeploymentToken "$subId|$ProjectName|$Environment|$Location")"
        if ($subnetNames -contains $functionSubnetName) {
            $delegation = Get-SubnetDelegation $functionSubnetName
            if ($delegation -eq 'Microsoft.App/environments') {
                # Left over from the Flex Consumption pattern; re-delegate (only succeeds if unused).
                Write-Phase "Re-delegating '$functionSubnetName' to Microsoft.Web/serverFarms (Dedicated plan)"
                az network vnet subnet update --resource-group $landingZoneRg --vnet-name $vnetName `
                    --name $functionSubnetName --delegations 'Microsoft.Web/serverFarms' --output none
                if ($LASTEXITCODE -ne 0) {
                    throw "Could not re-delegate '$functionSubnetName'. Remove its prior Flex integration, or pass -FunctionSubnetResourceId."
                }
            }
            elseif ($delegation -ne 'Microsoft.Web/serverFarms') {
                throw "Existing Function subnet '$functionSubnetName' has unexpected delegation '$delegation'."
            }
            $FunctionSubnetResourceId = Get-SubnetId $functionSubnetName
            Write-Host "[infra] Reusing Function subnet '$functionSubnetName'." -ForegroundColor Green
        }
        elseif (-not $WhatIf) {
            $FunctionSubnetResourceId = New-DelegatedSubnet $functionSubnetName 26 'Microsoft.Web/serverFarms'
        }
    }

    # Container Apps subnet: an environment binds 1:1 to a delegated /23.
    if (-not $AcaSubnetResourceId) {
        $acaSubnetName = "snet-aca-$subnetProject-$Environment-$(Get-DeploymentToken "$subId|$ProjectName|$Environment|$Location|aca")"
        if ($subnetNames -contains $acaSubnetName) {
            $delegation = Get-SubnetDelegation $acaSubnetName
            if ($delegation -ne 'Microsoft.App/environments') {
                throw "Existing Container Apps subnet '$acaSubnetName' has unexpected delegation '$delegation'."
            }
            $AcaSubnetResourceId = Get-SubnetId $acaSubnetName
            Write-Host "[infra] Reusing Container Apps subnet '$acaSubnetName'." -ForegroundColor Green
        }
        elseif (-not $WhatIf) {
            $AcaSubnetResourceId = New-DelegatedSubnet $acaSubnetName 23 'Microsoft.App/environments'
        }
    }

    # Private UI: the Container Apps private DNS zone, linked to the landing-zone VNet (the resolver
    # answers for it, so VPN clients resolve the UI to its private endpoint). Additive and idempotent.
    if ($PrivateUi) {
        # List queries, not 'show': a missing zone returns empty instead of an error on stderr (which
        # $ErrorActionPreference = 'Stop' turns into a terminating error in Windows PowerShell).
        $acaZone = "privatelink.$Location.azurecontainerapps.io"
        $zoneExists = az network private-dns zone list --resource-group $landingZoneRg --query "[?name=='$acaZone'].name | [0]" -o tsv
        if (-not $zoneExists -and -not $WhatIf) {
            Write-Phase "Creating private DNS zone '$acaZone' in '$landingZoneRg'"
            az network private-dns zone create --resource-group $landingZoneRg --name $acaZone --output none
            if ($LASTEXITCODE -ne 0) { throw "Could not create private DNS zone '$acaZone' in '$landingZoneRg'." }
            $zoneExists = $acaZone
        }
        $vnetId = az network vnet show --resource-group $landingZoneRg --name $vnetName --query id -o tsv
        $linked = if ($zoneExists) {
            az network private-dns link vnet list --resource-group $landingZoneRg --zone-name $acaZone `
                --query "[?virtualNetwork.id=='$vnetId'].name | [0]" -o tsv
        }
        if ($zoneExists -and -not $linked -and -not $WhatIf) {
            Write-Phase "Linking '$acaZone' to '$vnetName'"
            az network private-dns link vnet create --resource-group $landingZoneRg --zone-name $acaZone `
                --name "link-$vnetName" --virtual-network $vnetId --registration-enabled false --output none
            if ($LASTEXITCODE -ne 0) { throw "Could not link private DNS zone '$acaZone' to '$vnetName'." }
        }
        Write-Host "[infra] Private UI:          zone '$acaZone' linked to '$vnetName'" -ForegroundColor Green
    }

    Write-Host "[infra] Landing zone RG:     $landingZoneRg" -ForegroundColor Green
    Write-Host "[infra] Landing zone VNet:   $vnetName ($vnetLocation)" -ForegroundColor Green
    Write-Host "[infra] Function subnet:     $FunctionSubnetResourceId" -ForegroundColor Green
    Write-Host "[infra] PE subnet:           $PrivateEndpointSubnetResourceId" -ForegroundColor Green
    Write-Host "[infra] ACA subnet:          $AcaSubnetResourceId" -ForegroundColor Green
}

Write-Phase "Ensuring resource group '$ResourceGroup' in '$Location' (private=$([bool]$Private))"
if (-not $WhatIf) {
    az group create --name $ResourceGroup --location $Location --output none
    if ($LASTEXITCODE -ne 0) { throw "Could not create or update resource group '$ResourceGroup'." }

    # Cognitive Services soft-delete guard: deleting the Foundry account (or its RG) leaves a soft-deleted
    # account that blocks recreating the same name (FlagMustBeSetForRestore). Purge any soft-deleted
    # 'aif-<project>-<env>-' account in THIS RG; irreversible, fine for an environment rebuilt from Bicep.
    $foundryPrefix = "aif-$ProjectName-$Environment-"
    $deleted = @(az cognitiveservices account list-deleted `
        --query "[?starts_with(name, '$foundryPrefix') && contains(id, '/resourceGroups/$ResourceGroup/')].name" `
        -o tsv 2>$null | Where-Object { $_ })
    foreach ($name in $deleted) {
        $deletedLocation = az cognitiveservices account list-deleted --query "[?name=='$name'].location | [0]" -o tsv 2>$null
        if (-not $deletedLocation) { $deletedLocation = $Location }
        Write-Host "[infra] Purging soft-deleted Foundry account '$name' ($deletedLocation) so it can be recreated." -ForegroundColor Yellow
        az cognitiveservices account purge --location $deletedLocation --resource-group $ResourceGroup --name $name --output none 2>$null
        if ($LASTEXITCODE -ne 0) {
            Write-Host "[infra] WARNING: could not purge '$name'; the deploy may fail with a soft-delete error." -ForegroundColor Yellow
        }
    }
}

$deployArgs = @(
    "deployment", "group", $(if ($WhatIf) { "what-if" } else { "create" }),
    "--resource-group", $ResourceGroup,
    "--name", "main",
    "--template-file", $template,
    "--parameters",
        "projectName=$ProjectName", "environmentName=$Environment", "location=$Location",
        "privateNetworking=$($Private.IsPresent.ToString().ToLowerInvariant())",
        "privateUi=$(($Private.IsPresent -and $PrivateUi.IsPresent).ToString().ToLowerInvariant())",
        "functionSubnetResourceId=$FunctionSubnetResourceId",
        "privateEndpointSubnetResourceId=$PrivateEndpointSubnetResourceId",
        "acaSubnetResourceId=$AcaSubnetResourceId",
        "devPrincipalId=$DevPrincipalId",
        "functionPlanSku=$FunctionPlanSku",
        "maxConcurrentActivities=$MaxConcurrentActivities",
        "receivingOrgAliases=$ReceivingOrgAliases",
        "proMinDigits=$ProMinDigits",
        "proMaxDigits=$ProMaxDigits",
        "proStripSuffixes=$ProStripSuffixes",
        "proSuffixExamples=$ProSuffixExamples"
)
if (-not $WhatIf) { $deployArgs += @("--output", "none") }

if ($ProMinDigits -or $ProMaxDigits -or $ProStripSuffixes) {
    Write-Host ("[infra] PRO rules: {0}-{1} digits, strip suffixes [{2}]" -f $(if ($ProMinDigits) { $ProMinDigits } else { 6 }), $(if ($ProMaxDigits) { $ProMaxDigits } else { 12 }), $ProStripSuffixes) -ForegroundColor Green
}
else {
    Write-Host "[infra] PRO_* not set (.env): neutral PRO rules (6-12 digits, no suffixes). Suffixed PROs will be flagged for review." -ForegroundColor Yellow
}

Write-Phase "Deploying infra/main.bicep"
az @deployArgs
if ($LASTEXITCODE -ne 0) { throw "Infrastructure deployment failed (az exit code $LASTEXITCODE). See the deployment errors above." }

if (-not $WhatIf -and $Private) {
    $outputs = Get-InfraOutputs -ResourceGroup $ResourceGroup
    if ((Get-OutputValue $outputs 'privateNetworkingEffective') -ne $true) {
        throw "Infrastructure did not activate private networking. Review the deployment output before proceeding."
    }
}

Write-Host "[infra] Infrastructure deployment complete for '$ResourceGroup'." -ForegroundColor Green
