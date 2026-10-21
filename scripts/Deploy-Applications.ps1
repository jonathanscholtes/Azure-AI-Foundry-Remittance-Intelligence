[CmdletBinding()]
param(
    [string]$Subscription,
    [string]$Environment = "dev",
    [string]$ProjectName = "ach-remit",
    [string]$ResourceGroup,
    # Image tag; defaults to a timestamp so each deploy rolls a new Container App revision.
    [string]$ImageTag,
    # Reuse images already at -ImageTag instead of building (skips the az acr build loop).
    [switch]$SkipBuild
)

# Builds the api (FastAPI) and ui (React) images with `az acr build` (no local Docker) and deploys
# infra/apps.bicep. The per-app env contract lives in apps.bicep; this script only resolves values.

$ErrorActionPreference = "Stop"
Import-Module (Join-Path $PSScriptRoot "common/DeploymentFunctions.psm1") -Force
Assert-Command az
Set-Utf8Console

$repoRoot = Resolve-RepoRoot
if (-not $ResourceGroup) { $ResourceGroup = "rg-$ProjectName-$Environment" }
if (-not $ImageTag) { $ImageTag = Get-Date -Format "yyyyMMddHHmmss" }
if ($Subscription) { az account set --subscription $Subscription | Out-Null }

# ---------------------------------------------------------------------------
# 1) Read the platform outputs from the 'main' infrastructure deployment.
# ---------------------------------------------------------------------------
Write-Phase "Reading infrastructure outputs from '$ResourceGroup'"
$o = Get-InfraOutputs -ResourceGroup $ResourceGroup
$registryLoginServer = Get-OutputValue $o 'containerRegistryLoginServer'
$environmentId = Get-OutputValue $o 'containerAppsEnvironmentId'
$serviceBusFqdn = Get-OutputValue $o 'serviceBusFqdn'
if (-not $registryLoginServer -or -not $environmentId -or -not $serviceBusFqdn) {
    throw "Container Apps / Service Bus outputs are missing; infrastructure incomplete."
}
$acrName = $registryLoginServer.Split('.')[0]

# ---------------------------------------------------------------------------
# 2) Build + push each image with `az acr build`. Start-Process -NoNewWindow keeps az on the real
#    console handle (Unicode-safe log streaming); a RELATIVE context + -WorkingDirectory keeps a repo
#    path with spaces ('OneDrive - Microsoft') from being split into extra args.
# ---------------------------------------------------------------------------
$builds = @(
    @{ name = 'ach-remit-api'; context = 'apps/api' },
    @{ name = 'ach-remit-ui'; context = 'apps/ui' }
)
if (-not $SkipBuild) {
    # az CLI (Python, MSI build) raises a cp1252 'charmap' UnicodeEncodeError when streaming the ACR
    # Docker build logs on Windows, and does not honor PYTHONIOENCODING. Use --no-logs so az does not
    # print the build log stream (it still WAITS for the build and returns the real exit code); check
    # the build result in ACR if one fails.
    $env:PYTHONIOENCODING = 'utf-8'
    $env:PYTHONUTF8 = '1'
    foreach ($b in $builds) {
        Write-Phase "az acr build $($b.name):$ImageTag (logs suppressed; see ACR 'az acr task logs' if it fails)"
        $azArgs = @('acr', 'build', '--no-logs', '--resource-group', $ResourceGroup, '--registry', $acrName,
            '--image', "$($b.name):$ImageTag", $b.context)
        $proc = Start-Process -FilePath 'az' -ArgumentList $azArgs -WorkingDirectory $repoRoot -NoNewWindow -Wait -PassThru
        if ($proc.ExitCode -ne 0) { throw "Image build failed for '$($b.name)' (az exit code $($proc.ExitCode)). Check the ACR build logs." }
    }
}
else {
    Write-Host "[apps] -SkipBuild: reusing existing images at tag $ImageTag" -ForegroundColor Yellow
}

# ---------------------------------------------------------------------------
# 3) Deploy apps.bicep from a generated parameters file (.generated/apps/apps.parameters.json).
# ---------------------------------------------------------------------------
$genDir = Join-Path $repoRoot ".generated/apps"
New-Item -ItemType Directory -Force -Path $genDir | Out-Null
$paramsPath = Join-Path $genDir "apps.parameters.json"
[ordered]@{
    "`$schema"     = "https://schema.management.azure.com/schemas/2019-04-01/deploymentParameters.json#"
    contentVersion = "1.0.0.0"
    parameters     = [ordered]@{
        environmentId             = @{ value = $environmentId }
        registryLoginServer       = @{ value = $registryLoginServer }
        managedIdentityResourceId = @{ value = (Get-OutputValue $o 'managedIdentityResourceId') }
        managedIdentityClientId   = @{ value = (Get-OutputValue $o 'managedIdentityClientId') }
        imageTag                  = @{ value = $ImageTag }
        storageBlobEndpoint       = @{ value = (Get-OutputValue $o 'storageBlobEndpoint') }
        storageTableEndpoint      = @{ value = (Get-OutputValue $o 'storageTableEndpoint') }
        ledgerTableName           = @{ value = (Get-OutputValue $o 'ledgerTableName') }
        eventsTableName           = @{ value = (Get-OutputValue $o 'eventsTableName') }
        serviceBusFqdn            = @{ value = $serviceBusFqdn }
        intakeQueueName           = @{ value = (Get-OutputValue $o 'intakeQueueName') }
    }
} | ConvertTo-Json -Depth 8 | Set-Content -Path $paramsPath -Encoding utf8

Write-Phase "Deploying container apps (api, ui)"
$uiFqdn = az deployment group create --resource-group $ResourceGroup --name apps `
    --template-file (Join-Path $repoRoot "infra/apps.bicep") `
    --parameters "@$paramsPath" `
    --query "properties.outputs.uiFqdn.value" -o tsv
if ($LASTEXITCODE -ne 0) { throw "Container apps deployment failed (az exit code $LASTEXITCODE). See the deployment errors above." }

if ($uiFqdn) { Write-Host "[apps] Demo UI: https://$uiFqdn" -ForegroundColor Green }
Write-Host "[apps] Applications deployed to '$ResourceGroup' (tag $ImageTag)." -ForegroundColor Green
