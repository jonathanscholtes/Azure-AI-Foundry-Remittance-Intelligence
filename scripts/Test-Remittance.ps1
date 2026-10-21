[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$MsgPath,

    # Demo UI URL printed by deploy.ps1 (its nginx proxies /api), e.g. https://ach-remit-ui.<env-domain>.azurecontainerapps.io
    [Parameter(Mandatory = $true)]
    [string]$ApiUrl,

    [Parameter(Mandatory = $false)]
    [int]$TimeoutSeconds = 300,

    [Parameter(Mandatory = $false)]
    [string]$OutputDirectory = "."
)

# Submits one .msg/.eml through the real intake path (API -> Blob -> Service Bus -> Durable
# orchestration) and follows it to a terminal state via the ledger API. No VPN needed: the API
# reaches private storage server-side.

$ErrorActionPreference = "Stop"

if (-not (Test-Path -LiteralPath $MsgPath)) {
    throw "Message file not found: $MsgPath"
}
$base = $ApiUrl.TrimEnd('/')
$terminal = @('Completed', 'ReviewRequired', 'Failed', 'DuplicateSkipped')

Write-Host "[test] Submitting $(Split-Path -Leaf $MsgPath) to $base/api/remittances" -ForegroundColor Cyan
$form = @{ file = Get-Item -LiteralPath $MsgPath }
try {
    $queued = Invoke-RestMethod -Method Post -Uri "$base/api/remittances" -Form $form
}
catch [System.Management.Automation.ParameterBindingException] {
    # Windows PowerShell 5.1 has no -Form; fall back to curl.exe for the multipart upload.
    $queued = (& curl.exe -s -f -F "file=@$MsgPath" "$base/api/remittances") | ConvertFrom-Json
}
if (-not $queued.processingId) { throw "Submission failed." }
$processingId = $queued.processingId
Write-Host "[test] Queued:       $processingId (correlation $($queued.correlationId))"

$deadline = (Get-Date).AddSeconds($TimeoutSeconds)
$lastState = ''
$run = $null
while ((Get-Date) -lt $deadline) {
    try {
        $run = Invoke-RestMethod -Uri "$base/api/remittances/$processingId"
        $state = if ($run.currentState) { $run.currentState } else { $run.status }
        if ($state -ne $lastState) {
            Write-Host ("[test] {0:HH:mm:ss}  {1}" -f (Get-Date), $state)
            $lastState = $state
        }
        if ($terminal -contains $run.status) { break }
    }
    catch {
        # 404 until the orchestration records 'Received'.
    }
    Start-Sleep -Seconds 2
}

if (-not $run -or $terminal -notcontains $run.status) {
    Write-Host "`n[test] Not finished after $TimeoutSeconds s (last state: $lastState). Check the Live Pipeline page." -ForegroundColor Yellow
    exit 1
}

$resultPath = Join-Path $OutputDirectory "$processingId.json"
$run | ConvertTo-Json -Depth 20 | Set-Content -LiteralPath $resultPath -Encoding utf8
Write-Host "`n[test] RESULT" -ForegroundColor Green
Write-Host "  status:         $($run.status)"
Write-Host "  payer:          $($run.payerName)"
Write-Host "  lines:          $($run.lineCount)"
Write-Host "  stated / sum:   $($run.paymentTotal) / $($run.calculatedPaymentTotal)"
Write-Host "  warnings:       $($run.warnings -join ', ')"
Write-Host "  agent decision: $($run.assuranceDecision)"
Write-Host "  csv:            $($run.csvFileName)"
Write-Host "  duration:       $($run.durationMs) ms"
Write-Host "  (full detail saved to $resultPath)"
