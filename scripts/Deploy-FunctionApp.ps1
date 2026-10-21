[CmdletBinding()]
param(
    [string]$Subscription,
    [string]$Environment = "dev",
    [string]$ProjectName = "ach-remit",
    [string]$ResourceGroup,
    [string]$FunctionAppName,
    [int]$MaxPublishAttempts = 3
)

# Publishes apps/remittance-processor (Service Bus starter + Durable orchestrator + activities) with a
# remote build. In private mode the publish needs line-of-sight to the storage private endpoint (VPN).

$ErrorActionPreference = "Stop"
Import-Module (Join-Path $PSScriptRoot "common/DeploymentFunctions.psm1") -Force
Assert-Command az
Assert-Command func

$repoRoot = Resolve-RepoRoot
if (-not $ResourceGroup) { $ResourceGroup = "rg-$ProjectName-$Environment" }
if ($Subscription) { az account set --subscription $Subscription | Out-Null }
if (-not $FunctionAppName) {
    $FunctionAppName = Get-OutputValue (Get-InfraOutputs -ResourceGroup $ResourceGroup) 'functionAppName'
}
if (-not $FunctionAppName) { throw "Function app name not resolved. Pass -FunctionAppName or deploy infra first." }

Write-Phase "Publishing Python Function to '$FunctionAppName' with remote build"
Push-Location (Join-Path $repoRoot "apps/remittance-processor")
try {
    for ($attempt = 1; $attempt -le $MaxPublishAttempts; $attempt++) {
        func azure functionapp publish $FunctionAppName --python --build remote
        if ($LASTEXITCODE -eq 0) {
            Write-Host "[function] Published '$FunctionAppName'." -ForegroundColor Green
            return
        }
        if ($attempt -lt $MaxPublishAttempts) {
            $wait = 15 * $attempt
            Write-Host "[function] Publish attempt $attempt/$MaxPublishAttempts failed; retrying in ${wait}s." -ForegroundColor Yellow
            Start-Sleep -Seconds $wait
        }
    }
    throw "Function publish failed after $MaxPublishAttempts attempts. In private mode, publish from a machine with network access to the landing-zone storage private endpoint."
}
finally {
    Pop-Location
}
