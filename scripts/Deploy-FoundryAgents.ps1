[CmdletBinding()]
param(
    [string]$Subscription,
    [string]$Environment = "dev",
    [string]$ProjectName = "ach-remit",
    [string]$ResourceGroup,
    [string]$ProjectEndpoint,
    # Defaults to the Content Understanding completion deployment (gpt-5.5) from the infra outputs.
    [string]$ModelDeployment,
    [string]$SearchConnectionName,
    [string]$IndexName,
    [int]$MaxAttempts = 6
)

# Creates a new version of each Foundry agent (agents/deploy.py): ACH Extraction Expert and ACH Assurance
# Expert. The Function references agents by NAME (EXTRACTION_AGENT_NAME / ASSURANCE_AGENT_NAME, set by infra),
# so the latest version is used with no id injection.

$ErrorActionPreference = "Stop"
Import-Module (Join-Path $PSScriptRoot "common/DeploymentFunctions.psm1") -Force
Import-DotEnv   # PRO_CREDIT_MEMO_SUFFIX for the ACH Assurance Expert's instructions
Assert-Command az
Assert-Command python

$repoRoot = Resolve-RepoRoot
if (-not $ResourceGroup) { $ResourceGroup = "rg-$ProjectName-$Environment" }
if ($Subscription) { az account set --subscription $Subscription | Out-Null }

Write-Phase "Reading Foundry project settings from infra outputs"
$outputs = Get-InfraOutputs -ResourceGroup $ResourceGroup
if (-not $ProjectEndpoint) { $ProjectEndpoint = Get-OutputValue $outputs 'foundryProjectEndpoint' }
if (-not $ModelDeployment) { $ModelDeployment = Get-OutputValue $outputs 'contentUnderstandingCompletionDeploymentName' }
if (-not $SearchConnectionName) { $SearchConnectionName = Get-OutputValue $outputs 'searchConnectionName' }
if (-not $IndexName) { $IndexName = Get-OutputValue $outputs 'policyIndexName' }
$reasoningDeployment = Get-OutputValue $outputs 'reasoningDeploymentName'
$agentName = @((Get-OutputValue $outputs 'extractionAgentName'), (Get-OutputValue $outputs 'resolutionAgentName'), (Get-OutputValue $outputs 'assuranceAgentName')) -join ', '
if (-not $ProjectEndpoint) { throw "Could not resolve the Foundry project endpoint. Pass -ProjectEndpoint or deploy infra first." }

Write-Phase "Installing agent SDK dependencies"
python -m pip install --quiet -r (Join-Path $repoRoot "agents/requirements.txt")
if ($LASTEXITCODE -ne 0) { throw "pip install failed" }

# Fresh Azure AI User assignments can take a few minutes to apply; retry briefly.
Write-Phase "Deploying Foundry agents ($agentName on $ModelDeployment)"
if ($env:PRO_CREDIT_MEMO_SUFFIX) {
    Write-Host "[agents] Credit-memo PRO suffix for instructions: $($env:PRO_CREDIT_MEMO_SUFFIX)" -ForegroundColor Green
}
else {
    Write-Host "[agents] PRO_CREDIT_MEMO_SUFFIX not set; instructions use the generic example (-CR1)." -ForegroundColor Yellow
}
$reasoningArgs = if ($reasoningDeployment) { @('--reasoning-deployment', $reasoningDeployment) } else { @() }
for ($attempt = 1; $attempt -le $MaxAttempts; $attempt++) {
    python (Join-Path $repoRoot "agents/deploy.py") `
        --project-endpoint       $ProjectEndpoint `
        --model-deployment       $ModelDeployment `
        --search-connection-name $SearchConnectionName `
        --index-name             $IndexName `
        @reasoningArgs
    if ($LASTEXITCODE -eq 0) {
        Write-Host "[agents] Agents deployed; the Function resolves $agentName (latest versions) by name." -ForegroundColor Green
        return
    }
    if ($attempt -lt $MaxAttempts) {
        Write-Host "[agents] Attempt $attempt/$MaxAttempts failed (Azure AI User propagation?); retrying in 30s." -ForegroundColor Yellow
        Start-Sleep -Seconds 30
    }
}
throw "agents/deploy.py failed after $MaxAttempts attempts."
