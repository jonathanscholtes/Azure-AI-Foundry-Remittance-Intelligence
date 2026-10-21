[CmdletBinding()]
param(
    [string]$Subscription,
    [string]$Environment = "dev",
    [string]$ProjectName = "ach-remit",
    [string]$ResourceGroup,

    # Resolved from the 'main' infra outputs when omitted.
    [string]$Endpoint,
    [string]$CompletionDeploymentName,
    [string]$EmbeddingDeploymentName,
    [string]$AnalyzerId,
    [string]$AnalyzerDefinitionPath,

    [int]$MaxPollAttempts = 60
)

# Configures Content Understanding model defaults and creates the ach.remittance.v1 analyzer
# (GA API 2025-11-01, Entra auth). Retries through the CU write-path warm-up on fresh accounts.

$ErrorActionPreference = "Stop"
Import-Module (Join-Path $PSScriptRoot "common/DeploymentFunctions.psm1") -Force
Assert-Command az

$repoRoot = Resolve-RepoRoot
if (-not $ResourceGroup) { $ResourceGroup = "rg-$ProjectName-$Environment" }
if ($Subscription) { az account set --subscription $Subscription | Out-Null }
if (-not $AnalyzerDefinitionPath) { $AnalyzerDefinitionPath = Join-Path $repoRoot "schemas/ach.remittance-analyzer-v1.json" }

if (-not $Endpoint -or -not $CompletionDeploymentName -or -not $EmbeddingDeploymentName -or -not $AnalyzerId) {
    Write-Phase "Reading Content Understanding settings from infra outputs"
    $outputs = Get-InfraOutputs -ResourceGroup $ResourceGroup
    if (-not $Endpoint) { $Endpoint = Get-OutputValue $outputs 'contentUnderstandingEndpoint' }
    if (-not $CompletionDeploymentName) { $CompletionDeploymentName = Get-OutputValue $outputs 'contentUnderstandingCompletionDeploymentName' }
    if (-not $EmbeddingDeploymentName) { $EmbeddingDeploymentName = Get-OutputValue $outputs 'contentUnderstandingEmbeddingDeploymentName' }
    if (-not $AnalyzerId) { $AnalyzerId = Get-OutputValue $outputs 'contentUnderstandingAnalyzerId' }
}
if (-not $AnalyzerId) { $AnalyzerId = "ach.remittance.v1" }
if (-not $Endpoint -or -not $CompletionDeploymentName -or -not $EmbeddingDeploymentName) {
    throw "Content Understanding outputs are missing. Deploy infrastructure first."
}
if (-not (Test-Path -LiteralPath $AnalyzerDefinitionPath)) {
    throw "Analyzer definition was not found: $AnalyzerDefinitionPath"
}

$analyzerDefinition = Get-Content -LiteralPath $AnalyzerDefinitionPath -Raw | ConvertFrom-Json
$accessToken = az account get-access-token `
    --resource "https://cognitiveservices.azure.com/" `
    --query accessToken `
    --output tsv
if ($LASTEXITCODE -ne 0 -or -not $accessToken) {
    throw "Could not acquire a Content Understanding access token. Run 'az login' and verify the deployment identity has Content Understanding Contributor on the Foundry resource."
}

$headers = @{ Authorization = "Bearer $accessToken" }
$baseUri = $Endpoint.TrimEnd('/')
$apiVersion = '2025-11-01'
$defaultsUri = "$baseUri/contentunderstanding/defaults?api-version=$apiVersion"
$defaultsBody = @{
    modelDeployments = @{
        'gpt-5.5' = $CompletionDeploymentName
        'text-embedding-3-large' = $EmbeddingDeploymentName
        'prebuilt-analyzer-completion' = $CompletionDeploymentName
        'prebuilt-analyzer-completion-mini' = $CompletionDeploymentName
        'prebuilt-analyzer-embedding' = $EmbeddingDeploymentName
    }
} | ConvertTo-Json -Depth 5

Write-Host "[content-understanding] Configuring default model deployments." -ForegroundColor Cyan
$defaultsConfigured = $false
$maxDefaultsAttempts = 12
for ($attempt = 1; $attempt -le $maxDefaultsAttempts; $attempt++) {
    try {
        Invoke-RestMethod `
            -Method Patch `
            -Uri $defaultsUri `
            -Headers $headers `
            -ContentType 'application/json' `
            -Body $defaultsBody `
            -UseBasicParsing | Out-Null
        $defaultsConfigured = $true
        break
    }
    catch {
        $statusCode = if ($_.Exception.Response) { [int]$_.Exception.Response.StatusCode } else { $null }
        if ($attempt -eq $maxDefaultsAttempts) {
            throw @"
Could not configure Content Understanding model defaults after $maxDefaultsAttempts attempts (last HTTP status: $statusCode).
A 5xx here on a freshly created or purged-and-recreated Foundry account usually means the Content Understanding
write path is still warming up (model-deployment binding lags ARM 'Succeeded'; reads work but writes 500).
This is not a configuration error. Wait ~15-30 minutes, then re-run only the downstream phases:
  ./scripts/Deploy-ContentUnderstanding.ps1 -Environment <env>   (or ./deploy.ps1 <same args> -Skip infra)
If it still fails after an hour, the account subdomain may be in a bad state from the purge+recreate -
recreate the Foundry account under a fresh name. Underlying error: $($_.Exception.Message)
"@
        }
        Write-Host "[content-understanding] Defaults update attempt $attempt/$maxDefaultsAttempts failed (HTTP $statusCode); retrying after warm-up/propagation." -ForegroundColor Yellow
        Start-Sleep -Seconds 20
    }
}

if (-not $defaultsConfigured) {
    throw "Content Understanding model defaults were not configured."
}

$analyzerUri = "$baseUri/contentunderstanding/analyzers/$AnalyzerId`?api-version=$apiVersion"
$analyzerJson = $analyzerDefinition | ConvertTo-Json -Depth 30
Write-Host "[content-understanding] Ensuring analyzer '$AnalyzerId' exists." -ForegroundColor Cyan

# PUT is create-only: an existing analyzer returns 409. The deploy identity has
# Content Understanding Contributor, which can create and read analyzers but NOT
# delete/replace them (DELETE returns 401), so on 409 we reuse the existing analyzer
# after confirming it is ready. To apply a changed analyzer definition, delete the
# analyzer out of band (requires Content Understanding Owner) and re-run.
$response = $null
$alreadyExists = $false
$maxPutAttempts = 10
for ($putAttempt = 1; $putAttempt -le $maxPutAttempts; $putAttempt++) {
    try {
        $response = Invoke-WebRequest `
            -Method Put `
            -Uri $analyzerUri `
            -Headers $headers `
            -ContentType 'application/json' `
            -Body $analyzerJson `
            -UseBasicParsing
        break
    }
    catch {
        $statusCode = if ($_.Exception.Response) { [int]$_.Exception.Response.StatusCode } else { $null }
        if ($statusCode -eq 409) {
            $alreadyExists = $true
            Write-Host "[content-understanding] Analyzer '$AnalyzerId' already exists; reusing it." -ForegroundColor Yellow
            break
        }
        # 5xx/408/429 on a fresh account is usually Content Understanding warm-up - retry with backoff.
        if (($statusCode -ge 500 -or $statusCode -eq 408 -or $statusCode -eq 429) -and $putAttempt -lt $maxPutAttempts) {
            Write-Host "[content-understanding] Analyzer create attempt $putAttempt/$maxPutAttempts failed (HTTP $statusCode); retrying after warm-up." -ForegroundColor Yellow
            Start-Sleep -Seconds 20
            continue
        }
        throw
    }
}
if (-not $response -and -not $alreadyExists) {
    throw "Could not create analyzer '$AnalyzerId' after $maxPutAttempts attempts (likely Content Understanding warm-up). Wait ~15-30 minutes and re-run with -Skip infra."
}

if ($alreadyExists) {
    $existing = Invoke-RestMethod -Method Get -Uri $analyzerUri -Headers $headers -UseBasicParsing
    $state = "$($existing.status)"
    if ($state -and ($state -notin @('ready', 'succeeded'))) {
        throw "Analyzer '$AnalyzerId' exists but is in state '$state'. Wait for it to finish, or delete it (requires Content Understanding Owner) and re-run."
    }
    Write-Host "[content-understanding] Analyzer '$AnalyzerId' is ready (reused) at $baseUri." -ForegroundColor Green
}
else {
    $operationLocation = $response.Headers['Operation-Location']
    if ($operationLocation) {
        if (-not [Uri]::IsWellFormedUriString($operationLocation, [UriKind]::Absolute)) {
            $operationLocation = [Uri]::new([Uri]$baseUri, $operationLocation).AbsoluteUri
        }
        $completed = $false
        for ($attempt = 1; $attempt -le $MaxPollAttempts; $attempt++) {
            $operation = Invoke-RestMethod -Method Get -Uri $operationLocation -Headers $headers -UseBasicParsing
            if ($operation.status -ieq 'succeeded') {
                $completed = $true
                break
            }
            if ($operation.status -ieq 'failed') {
                throw "Content Understanding analyzer operation failed: $($operation.error | ConvertTo-Json -Compress -Depth 8)"
            }
            Start-Sleep -Seconds 5
        }
        if (-not $completed) {
            throw "Timed out waiting for analyzer '$AnalyzerId' after $MaxPollAttempts attempts."
        }
    }
    Write-Host "[content-understanding] Analyzer '$AnalyzerId' is ready at $baseUri." -ForegroundColor Green
}