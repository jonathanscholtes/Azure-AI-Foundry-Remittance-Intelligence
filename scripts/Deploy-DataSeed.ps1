[CmdletBinding()]
param(
    [string]$Subscription,
    [string]$Environment = "dev",
    [string]$ProjectName = "ach-remit",
    [string]$ResourceGroup,
    [string]$SearchEndpoint,
    [string]$IndexName,
    [int]$MaxAttempts = 6
)

# Seeds the assurance agent's policy knowledge: creates/refreshes the 'ach-policy' AI Search index and loads the
# neutral deduction-policy corpus (agents/corpus/*.json) via scripts/load_policy_corpus.py.

$ErrorActionPreference = "Stop"
Import-Module (Join-Path $PSScriptRoot "common/DeploymentFunctions.psm1") -Force
Assert-Command az
Assert-Command python

$repoRoot = Resolve-RepoRoot
if (-not $ResourceGroup) { $ResourceGroup = "rg-$ProjectName-$Environment" }
if ($Subscription) { az account set --subscription $Subscription | Out-Null }

if (-not $SearchEndpoint -or -not $IndexName) {
    $outputs = Get-InfraOutputs -ResourceGroup $ResourceGroup
    if (-not $SearchEndpoint) { $SearchEndpoint = Get-OutputValue $outputs 'searchEndpoint' }
    if (-not $IndexName) { $IndexName = Get-OutputValue $outputs 'policyIndexName' }
}
if (-not $SearchEndpoint) { throw "AI Search endpoint not resolved. Pass -SearchEndpoint or deploy infra first." }
if (-not $IndexName) { $IndexName = "ach-policy" }

Write-Phase "Installing agent tooling (agents/requirements.txt)"
python -m pip install --quiet -r (Join-Path $repoRoot "agents/requirements.txt")
if ($LASTEXITCODE -ne 0) { throw "pip install failed" }

# Fresh Search RBAC can take a few minutes to apply; retry briefly.
Write-Phase "Loading the deduction-policy corpus into '$IndexName'"
for ($attempt = 1; $attempt -le $MaxAttempts; $attempt++) {
    python (Join-Path $repoRoot "scripts/load_policy_corpus.py") --search-endpoint $SearchEndpoint --index-name $IndexName
    if ($LASTEXITCODE -eq 0) {
        Write-Host "[dataseed] Policy corpus loaded into '$IndexName'." -ForegroundColor Green
        return
    }
    if ($attempt -lt $MaxAttempts) {
        Write-Host "[dataseed] Attempt $attempt/$MaxAttempts failed (RBAC propagation?); retrying in 30s." -ForegroundColor Yellow
        Start-Sleep -Seconds 30
    }
}
throw "Policy corpus load failed after $MaxAttempts attempts."
