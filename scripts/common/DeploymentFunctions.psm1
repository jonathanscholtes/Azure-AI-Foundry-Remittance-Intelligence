# ============================================================
# DeploymentFunctions.psm1 - shared helpers for phase scripts
# ============================================================

function Write-Phase {
    param([Parameter(Mandatory)][string]$Message)
    Write-Host "[deploy] $Message" -ForegroundColor Cyan
}

function Resolve-RepoRoot {
    return (Resolve-Path (Join-Path $PSScriptRoot "../..")).Path
}

function Assert-Command {
    param([Parameter(Mandatory)][string]$Name)
    if (-not (Get-Command $Name -ErrorAction SilentlyContinue)) {
        throw "Required command '$Name' not found on PATH."
    }
}

function Get-InfraOutputs {
    # Outputs of the 'main' infrastructure deployment in the resource group (deployed by
    # Deploy-Infrastructure.ps1). Every downstream phase resolves its inputs from these.
    param([Parameter(Mandatory)][string]$ResourceGroup)
    $raw = az deployment group show --resource-group $ResourceGroup --name main `
        --query properties.outputs --output json 2>$null
    if ($LASTEXITCODE -ne 0 -or -not $raw) {
        throw "Could not read 'main' deployment outputs in '$ResourceGroup'. Run Deploy-Infrastructure first."
    }
    return $raw | ConvertFrom-Json
}

function Get-OutputValue {
    param([object]$Outputs, [Parameter(Mandatory)][string]$Name)
    if ($Outputs -and $Outputs.PSObject.Properties.Name -contains $Name) {
        return $Outputs.$Name.value
    }
    return ""
}

function Get-ObjectIdFromArmToken {
    # The 'oid' claim of the ARM access token is the deploying principal's Entra object id and needs
    # no Microsoft Graph call, so it works when Graph is CAE-challenged but ARM is not.
    $previous = $ErrorActionPreference
    $ErrorActionPreference = 'SilentlyContinue'
    try { $accessToken = az account get-access-token --query accessToken --output tsv 2>$null }
    catch { $accessToken = $null }
    finally { $ErrorActionPreference = $previous }
    if ($LASTEXITCODE -ne 0 -or -not $accessToken) { return $null }
    try {
        $segment = $accessToken.Split('.')[1].Replace('-', '+').Replace('_', '/')
        switch ($segment.Length % 4) { 2 { $segment += '==' } 3 { $segment += '=' } }
        $claims = [System.Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($segment)) | ConvertFrom-Json
        if ($claims.oid) { return "$($claims.oid)".Trim() }
    }
    catch { return $null }
    return $null
}

function Get-SignedInObjectId {
    # Graph first (az ad signed-in-user show). That call can raise a Conditional Access / CAE challenge
    # ('TokenCreatedWithOutdatedPolicies / InteractionRequired', common right after connecting the VPN),
    # so fall back to the ARM token's 'oid' claim.
    $previous = $ErrorActionPreference
    $ErrorActionPreference = 'SilentlyContinue'
    try { $id = az ad signed-in-user show --query id --output tsv 2>$null }
    catch { $id = $null }
    finally { $ErrorActionPreference = $previous }
    if ($LASTEXITCODE -eq 0 -and $id) { return "$id".Trim() }

    $fromToken = Get-ObjectIdFromArmToken
    if ($fromToken) {
        Write-Host "[deploy] Graph lookup unavailable (likely a Conditional Access/CAE challenge); using the object id from the ARM token." -ForegroundColor Yellow
        return $fromToken
    }
    return $null
}

function Set-Utf8Console {
    # `az acr build` streams ACR Tasks logs locally; Vite emits Unicode (U+2713) that the default cp1252
    # console can't encode, crashing az mid-stream. Force UTF-8 for the console + the az (Python) child.
    $env:PYTHONIOENCODING = 'utf-8'
    $env:PYTHONUTF8 = '1'
    try { chcp 65001 | Out-Null } catch {}
    try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch {}
}

function Import-DotEnv {
    # Load the repo's .env (git-ignored) so organization-specific settings - RECEIVING_ORG_ALIASES, PRO_* -
    # stay out of the repo. A real environment variable or an explicit parameter always wins.
    param([string]$Path = (Join-Path (Resolve-RepoRoot) '.env'))
    if (-not (Test-Path $Path)) { return }
    foreach ($line in Get-Content $Path) {
        $trimmed = $line.Trim()
        if (-not $trimmed -or $trimmed.StartsWith('#') -or ($trimmed -notmatch '=')) { continue }
        $name, $value = $trimmed -split '=', 2
        $name = $name.Trim()
        $value = $value.Trim().Trim('"').Trim("'")
        if ($name -and -not [Environment]::GetEnvironmentVariable($name)) {
            Set-Item -Path "env:$name" -Value $value
        }
    }
}

Export-ModuleMember -Function Write-Phase, Resolve-RepoRoot, Assert-Command, Get-InfraOutputs, Get-OutputValue, Get-SignedInObjectId, Set-Utf8Console, Import-DotEnv
