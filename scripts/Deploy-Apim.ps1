[CmdletBinding()]
param(
    [string]$Subscription,
    [string]$Environment = "dev",
    [string]$ProjectName = "ach-remit",
    [string]$ResourceGroup,
    [string]$LandingZoneTag = "role=private-landing-zone",
    # Publisher email on the APIM instance (APIM sends service notifications to it).
    [string]$PublisherEmail = "apim-admin@example.com",
    # Name given to the MCP server in the manual APIM export; it sets the MCP URL path.
    [string]$McpServerName = "ach-review-mcp",
    # Foundry project connection the ACH Review Expert's MCP tool uses (agents/deploy.py).
    [string]$ConnectionName = "ach-review-mcp"
)

# Deploys the solution's dedicated API Management for the ACH Review Expert (infra/apim/apim.bicep):
# Developer tier, injected into the landing-zone VNet in external mode, with the ACH Review API on it.
# Then creates the Foundry connection holding the MCP URL + APIM subscription key
# (infra/core/ai/foundry-mcp-connection.bicep). Private deployments only (-Private -PrivateUi), after the
# apps phase. Then: export the API as an MCP server in the portal (printed below), redeploy the agents.

$ErrorActionPreference = "Stop"
Import-Module (Join-Path $PSScriptRoot "common/DeploymentFunctions.psm1") -Force
Assert-Command az

$repoRoot = Resolve-RepoRoot
if (-not $ResourceGroup) { $ResourceGroup = "rg-$ProjectName-$Environment" }
if ($Subscription) { az account set --subscription $Subscription | Out-Null }

Write-Phase "Reading solution outputs"
$outputs = Get-InfraOutputs -ResourceGroup $ResourceGroup
if ((Get-OutputValue $outputs 'privateUiEffective') -ne $true) {
    throw "The ACH Review Expert's API Management is for private deployments: deploy with -Private -PrivateUi first."
}
$location = (az group show --name $ResourceGroup --query location -o tsv).Trim()
$uiFqdn = az containerapp show --name ach-remit-ui --resource-group $ResourceGroup --query "properties.configuration.ingress.fqdn" -o tsv 2>$null
if (-not $uiFqdn) { throw "Container app 'ach-remit-ui' not found in '$ResourceGroup'; run the apps phase first." }

# The Foundry project, from the project endpoint (https://<subdomain>.services.ai.azure.com/api/projects/<project>).
# The host is the account's custom subdomain, not its resource name, so look the account up by it.
$projectEndpoint = Get-OutputValue $outputs 'foundryProjectEndpoint'
if ($projectEndpoint -notmatch '^https://([^.]+)\.[^/]+/api/projects/([^/]+)') { throw "Unexpected Foundry project endpoint '$projectEndpoint'." }
$subdomain, $projectName = $Matches[1], $Matches[2]
$accountName = az cognitiveservices account list --resource-group $ResourceGroup `
    --query "[?properties.customSubDomainName=='$subdomain'].name | [0]" -o tsv 2>$null
if (-not $accountName) { throw "No Foundry account with subdomain '$subdomain' in '$ResourceGroup'." }

Write-Phase "Resolving private landing zone (tag '$LandingZoneTag')"
$landingZoneRg = az group list --tag $LandingZoneTag --query "[0].name" -o tsv 2>$null
if (-not $landingZoneRg) { throw "No resource group tagged '$LandingZoneTag' was found." }
$vnetName = az network vnet list --resource-group $landingZoneRg --query "[0].name" -o tsv 2>$null
if (-not $vnetName) { throw "No virtual network found in landing zone resource group '$landingZoneRg'." }

# APIM's own /27, named per deployment like the Function and ACA subnets so re-runs reuse it.
$subId = az account show --query id -o tsv 2>$null
$subnetProject = $ProjectName -replace '[^A-Za-z0-9-]', ''
$subnetName = "snet-apim-$subnetProject-$Environment-$(Get-DeploymentToken "$subId|$ProjectName|$Environment|$location|apim")"
$subnetPrefix = az network vnet subnet list --resource-group $landingZoneRg --vnet-name $vnetName `
    --query "[?name=='$subnetName'].addressPrefix | [0]" -o tsv 2>$null
if (-not $subnetPrefix) {
    $vnetCidr = az network vnet show --resource-group $landingZoneRg --name $vnetName --query "addressSpace.addressPrefixes[0]" -o tsv 2>$null
    $usedCidrs = @(az network vnet subnet list --resource-group $landingZoneRg --vnet-name $vnetName --query "[].addressPrefix" -o tsv 2>$null)
    $subnetPrefix = Get-FreeSubnetCidr -VnetCidr $vnetCidr -UsedCidrs $usedCidrs -NewPrefix 27
}
Write-Host "[apim] Subnet: $subnetName ($subnetPrefix) in $vnetName" -ForegroundColor Green

Write-Phase "Deploying API Management (Developer, VNet-injected) - the first deployment takes ~30-45 minutes"
$apim = az deployment group create --resource-group $ResourceGroup --name apim `
    --template-file (Join-Path $repoRoot "infra/apim/apim.bicep") `
    --parameters projectName=$ProjectName environmentName=$Environment location=$location `
    landingZoneResourceGroup=$landingZoneRg vnetName=$vnetName subnetName=$subnetName subnetPrefix=$subnetPrefix `
    uiFqdn=$uiFqdn publisherEmail=$PublisherEmail `
    --query "properties.outputs" -o json | ConvertFrom-Json
if ($LASTEXITCODE -ne 0) { throw "API Management deployment failed (az exit code $LASTEXITCODE)." }
$apimName = $apim.apimName.value
$gatewayUrl = $apim.gatewayUrl.value

$apimId = az apim show --name $apimName --resource-group $ResourceGroup --query id -o tsv
$subscriptionKey = az rest --method post `
    --uri "https://management.azure.com$apimId/subscriptions/$($apim.subscriptionName.value)/listSecrets?api-version=2023-05-01-preview" `
    --query primaryKey -o tsv
if (-not $subscriptionKey) { throw "Could not read the APIM subscription key for '$($apim.subscriptionName.value)'." }

$mcpUrl = "$gatewayUrl/$McpServerName/mcp"
Write-Phase "Creating Foundry connection '$ConnectionName' -> $mcpUrl"
az deployment group create --resource-group $ResourceGroup --name ach-review-mcp-connection `
    --template-file (Join-Path $repoRoot "infra/core/ai/foundry-mcp-connection.bicep") `
    --parameters accountName=$accountName projectName=$projectName connectionName=$ConnectionName `
    target=$mcpUrl "subscriptionKey=$subscriptionKey" `
    --query "properties.provisioningState" -o tsv | Out-Null
if ($LASTEXITCODE -ne 0) { throw "Foundry connection deployment failed (az exit code $LASTEXITCODE)." }

Write-Host ""
Write-Host "[apim] $apimName - ACH Review API: $gatewayUrl/$($apim.apiPath.value)  (product 'ACH Review', subscription key required)" -ForegroundColor Green
Write-Host ""
Write-Host "=== MANUAL STEP: export the ACH Review API as an MCP server ===" -ForegroundColor Cyan
Write-Host "  1. Azure portal -> API Management '$apimName' -> APIs -> MCP servers -> + Create -> Expose an API as an MCP server."
Write-Host "  2. API: 'ACH Review API'. Operations: all five (List runs, Get run, Get source text, Complete review, Get pipeline status)."
Write-Host "  3. Name: EXACTLY '$McpServerName' (sets the URL $mcpUrl). Product: 'ACH Review' (subscription required)."
Write-Host "  4. Redeploy the agents so the ACH Review Expert binds the MCP server:"
Write-Host "       ./deploy.ps1 -Private -PrivateUi -Skip infra,contentunderstanding,dataseed,function,apps,apim"
Write-Host "  5. Publish the ACH Review Expert to Microsoft 365 Copilot from the Foundry portal (docs/review-agent.md)."
Write-Host ""
