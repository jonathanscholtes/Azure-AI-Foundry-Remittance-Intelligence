[CmdletBinding()]
param(
    [Parameter(Mandatory = $false)]
    [string]$Subscription,

    [Parameter(Mandatory = $false)]
    [string]$Location = "eastus2",

    [Parameter(Mandatory = $false)]
    [string]$Environment = "dev",

    # Run the Function + API unit tests before deploying. Default: skip.
    [Parameter(Mandatory = $false)]
    [bool]$SkipTests = $true,

    # Phases to skip, e.g. -Skip infra,contentunderstanding,dataseed,agents to redeploy just the code.
    # Order of phases: infra -> contentunderstanding -> dataseed -> agents -> function -> apps.
    [Parameter(Mandatory = $false)]
    [ValidateSet('infra', 'contentunderstanding', 'dataseed', 'agents', 'function', 'apps')]
    [string[]]$Skip = @(),

    # App Service plan SKU for the Function. B1 is fine for single uploads; use S1/P1v3 for load tests.
    [Parameter(Mandatory = $false)]
    [string]$FunctionPlanSku = "B1",

    # Max concurrent Durable activities per Function instance (throttles CU / agent calls).
    [Parameter(Mandatory = $false)]
    [int]$MaxConcurrentActivities = 8,

    # Object id of the deploying user (data-plane grants). Resolved from the signed-in user when omitted.
    [Parameter(Mandatory = $false)]
    [string]$DevPrincipalId = "",

    [Parameter(Mandatory = $false)]
    [switch]$Private,

    # With -Private: the UI is reachable only from the landing-zone VNet / VPN (private endpoint for the
    # Container Apps environment, public access disabled). Pass it on every infra deploy to keep it so.
    [Parameter(Mandatory = $false)]
    [switch]$PrivateUi,

    # Resource tag used to discover the shared private landing-zone VNet when -Private is set.
    [Parameter(Mandatory = $false)]
    [string]$LandingZoneTag = "role=private-landing-zone",

    # Explicit landing-zone subnet overrides (otherwise resolved / created by Deploy-Infrastructure).
    [Parameter(Mandatory = $false)]
    [string]$FunctionSubnetResourceId = "",

    [Parameter(Mandatory = $false)]
    [string]$PrivateEndpointSubnetResourceId = "",

    [Parameter(Mandatory = $false)]
    [string]$AcaSubnetResourceId = ""
)

$ErrorActionPreference = "Stop"

Import-Module (Join-Path $PSScriptRoot "scripts/common/DeploymentFunctions.psm1") -Force

# Organization settings (RECEIVING_ORG_ALIASES, PRO_*) come only from a local .env file (git-ignored), never
# from parameters, so every deploy applies the same values. The infra and agents phases read them from
# the environment; a real environment variable wins over .env for a one-off override.
Import-DotEnv (Join-Path $PSScriptRoot '.env')

function Write-Phase {
    param([string]$Message)
    Write-Host "[deploy] $Message" -ForegroundColor Cyan
}

function Resolve-RepoPath {
    param([string]$RelativePath)
    return Join-Path $PSScriptRoot $RelativePath
}

function Invoke-OptionalPhaseScript {
    param(
        [string]$Label,
        [string]$ScriptPath,
        [hashtable]$ScriptArgs
    )

    if (-not (Test-Path $ScriptPath)) {
        Write-Host "[deploy] Skipping $Label (script not found): $ScriptPath" -ForegroundColor Yellow
        return
    }

    Write-Phase "Running $Label"
    & $ScriptPath @ScriptArgs
}

# Set deployment constants
$ProjectName = "ach-remit"
$EnvironmentName = $Environment
$ResourceGroup = "rg-$ProjectName-$EnvironmentName"

Write-Host @"

============================================================
  ACH Remittance Intelligence - Deployment Orchestrator
============================================================

  Project:     $ProjectName
  Environment: $EnvironmentName
  Location:    $Location
  Networking:  $(if ($Private) { "private (landing zone)" } else { "public" })
  Function:    $FunctionPlanSku (max $MaxConcurrentActivities concurrent activities)
  Payee names: $(if ($env:RECEIVING_ORG_ALIASES) { "set (.env)" } else { "not set" })
  PRO rules:   $(if ($env:PRO_MIN_DIGITS -or $env:PRO_MAX_DIGITS -or $env:PRO_STRIP_SUFFIXES) { "set (.env)" } else { "not set - neutral (6-12 digits, no suffixes)" })


"@ -ForegroundColor Cyan

# Validate prerequisites: the Azure CLI must be present (func only when publishing the Function).
if (-not (Get-Command az -ErrorAction SilentlyContinue)) {
    throw "Azure CLI ('az') is required but was not found on PATH. Install: https://aka.ms/azcli"
}
if ($Skip -notcontains 'function' -and -not (Get-Command func -ErrorAction SilentlyContinue)) {
    throw "Azure Functions Core Tools ('func') is required unless the function phase is skipped."
}

if ($Subscription) {
    Write-Phase "Setting Azure subscription context"
    az account set --subscription $Subscription | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "Could not select subscription '$Subscription'." }
}

# Confirm we're logged in before doing any real work.
$account = az account show --query id -o tsv 2>$null
if (-not $account) {
    throw "Not logged in to Azure. Run 'az login' (and optionally 'az account set --subscription <id>') first."
}

# The deploying user's object id is REQUIRED (Storage Blob/Table Data Contributor for the Function
# publish, CU Contributor for the analyzer, Search/AI User for the corpus + agents), so stop if unresolved.
if (-not $DevPrincipalId) {
    $DevPrincipalId = Get-SignedInObjectId
    if ($DevPrincipalId) {
        Write-Host "[deploy] Resolved DevPrincipalId from signed-in user: $DevPrincipalId" -ForegroundColor Green
    }
    else {
        throw @"
Could not resolve your Microsoft Entra object id (Graph and ARM-token fallback both failed).
This is almost always a Conditional Access / CAE token challenge right after connecting the VPN.
Fix one of these, then re-run (the deploy is idempotent):
  1. az login   (if it persists: az login --scope https://graph.microsoft.com/.default)
  2. ./deploy.ps1 ... -DevPrincipalId <your-object-id>
"@
    }
}

# Optional unit-test phase (skipped by default).
if (-not $SkipTests) {
    Write-Phase "Running unit tests"
    Push-Location (Resolve-RepoPath "apps/remittance-processor")
    python -m unittest discover -s tests
    $code = $LASTEXITCODE
    Pop-Location
    if ($code -ne 0) { throw "Unit tests failed for apps/remittance-processor." }
    python -m unittest discover -s (Resolve-RepoPath "apps/api/tests")
    if ($LASTEXITCODE -ne 0) { throw "Unit tests failed for apps/api." }
}
else {
    Write-Host "[deploy] Skipping unit tests (-SkipTests `$true)" -ForegroundColor Yellow
}

$common = @{ Subscription = $Subscription; Environment = $EnvironmentName; ProjectName = $ProjectName }

if ($Skip -notcontains 'infra') {
    $infraArgs = @{
        Subscription = $Subscription
        Location = $Location
        Environment = $EnvironmentName
        ProjectName = $ProjectName
        DevPrincipalId = $DevPrincipalId
        FunctionPlanSku = $FunctionPlanSku
        MaxConcurrentActivities = $MaxConcurrentActivities
    }
    if ($Private) { $infraArgs.Private = $true; $infraArgs.LandingZoneTag = $LandingZoneTag }
    if ($Private -and $PrivateUi) { $infraArgs.PrivateUi = $true }
    if ($FunctionSubnetResourceId) { $infraArgs.FunctionSubnetResourceId = $FunctionSubnetResourceId }
    if ($PrivateEndpointSubnetResourceId) { $infraArgs.PrivateEndpointSubnetResourceId = $PrivateEndpointSubnetResourceId }
    if ($AcaSubnetResourceId) { $infraArgs.AcaSubnetResourceId = $AcaSubnetResourceId }
    Invoke-OptionalPhaseScript -Label "Infrastructure" -ScriptPath (Resolve-RepoPath "scripts/Deploy-Infrastructure.ps1") -ScriptArgs $infraArgs
}

# Content Understanding: model defaults + the ach.remittance.v1 analyzer.
if ($Skip -notcontains 'contentunderstanding') {
    Invoke-OptionalPhaseScript -Label "Content Understanding" -ScriptPath (Resolve-RepoPath "scripts/Deploy-ContentUnderstanding.ps1") -ScriptArgs $common
}

# Data seed: the deduction-policy corpus into the AI Search index.
if ($Skip -notcontains 'dataseed') {
    Invoke-OptionalPhaseScript -Label "Data Seed (policy corpus)" -ScriptPath (Resolve-RepoPath "scripts/Deploy-DataSeed.ps1") -ScriptArgs $common
}

# Agents: create a new version of the assurance agent (referenced by name by the Function).
if ($Skip -notcontains 'agents') {
    Invoke-OptionalPhaseScript -Label "Foundry Agents" -ScriptPath (Resolve-RepoPath "scripts/Deploy-FoundryAgents.ps1") -ScriptArgs $common
}

if ($Skip -notcontains 'function') {
    Invoke-OptionalPhaseScript -Label "Function App" -ScriptPath (Resolve-RepoPath "scripts/Deploy-FunctionApp.ps1") -ScriptArgs $common
}

if ($Skip -notcontains 'apps') {
    Invoke-OptionalPhaseScript -Label "Applications and UI" -ScriptPath (Resolve-RepoPath "scripts/Deploy-Applications.ps1") -ScriptArgs $common
}

# ---------------------------------------------------------------------------
# Deployment summary — resolve service endpoints from the infra outputs + apps.
# ---------------------------------------------------------------------------
Write-Host @"

============================================================
                 Deployment Summary
============================================================

  Project: $ProjectName   Environment: $EnvironmentName

"@ -ForegroundColor Cyan

# Phase checklist — reflects what actually ran (honors -Skip).
function Show-Phase($name, $ran) {
    if ($ran) { Write-Host "  [OK]   $name" -ForegroundColor Green }
    else { Write-Host "  [--]   $name (skipped)" -ForegroundColor DarkGray }
}
Show-Phase "Azure infrastructure deployed"      ($Skip -notcontains 'infra')
Show-Phase "Content Understanding configured"   ($Skip -notcontains 'contentunderstanding')
Show-Phase "Policy corpus seeded (AI Search)"   ($Skip -notcontains 'dataseed')
Show-Phase "Foundry agents deployed"            ($Skip -notcontains 'agents')
Show-Phase "Function App published"             ($Skip -notcontains 'function')
Show-Phase "Applications & UI deployed"         ($Skip -notcontains 'apps')

$out = $null
try { $out = Get-InfraOutputs -ResourceGroup $ResourceGroup } catch { }
function OutValue($name) { return (Get-OutputValue $out $name) }

Write-Host "`n  === Service Endpoints ===" -ForegroundColor Cyan
$uiFqdn = az containerapp show --resource-group $ResourceGroup --name "ach-remit-ui" --query properties.configuration.ingress.fqdn -o tsv 2>$null
if ($uiFqdn) { Write-Host ("  {0,-20} https://{1}" -f "Demo UI:", $uiFqdn) -ForegroundColor Cyan }
$functionAppName = OutValue "functionAppName"
if ($functionAppName) { Write-Host ("  {0,-20} {1}" -f "Function app:", $functionAppName) -ForegroundColor Green }
$sb = OutValue "serviceBusNamespace"
if ($sb) { Write-Host ("  {0,-20} {1} / {2}" -f "Service Bus:", $sb, (OutValue "intakeQueueName")) -ForegroundColor Green }
$cu = OutValue "contentUnderstandingResourceName"
if ($cu) { Write-Host ("  {0,-20} {1}  (analyzer {2})" -f "Foundry:", $cu, (OutValue "contentUnderstandingAnalyzerId")) -ForegroundColor Green }
$agent = @((OutValue "extractionAgentName"), (OutValue "resolutionAgentName"), (OutValue "assuranceAgentName")) -join ', '
if ($agent) { Write-Host ("  {0,-20} {1}  (index {2} on {3})" -f "Foundry agents:", $agent, (OutValue "policyIndexName"), (OutValue "searchServiceName")) -ForegroundColor Green }
$appi = OutValue "appInsightsName"
if ($appi) { Write-Host ("  {0,-20} {1}" -f "App Insights:", $appi) -ForegroundColor Green }
Write-Host "  Internal services (api): internal ingress only" -ForegroundColor DarkGray
if (-not $out) { Write-Host "  (Run 'az deployment group show -g $ResourceGroup -n main' for full outputs.)" -ForegroundColor DarkGray }

Write-Host @"

============================================================
              Deployment Complete!
============================================================

"@ -ForegroundColor Green
