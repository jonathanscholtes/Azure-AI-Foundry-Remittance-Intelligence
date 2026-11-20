<#
.SYNOPSIS
    Re-save synthetic workbooks through Excel, from a JSON job list written by generate.py.

.DESCRIPTION
    Each job is {source, target, format: xlsx|xls}. Excel opens the openpyxl workbook, recalculates it
    (so every formula carries its saved value, as in a real export) and saves it as Office Open XML
    (xlsx) or Excel 97-2003 (xls). The target name is kept as given. Document properties are cleared
    so no local identity is saved. generate.py sends only xlsx jobs: policies commonly block Excel
    from saving the legacy format, so it writes .xls with xlwt instead.
#>
param([Parameter(Mandatory)][string]$Jobs)

$ErrorActionPreference = 'Stop'
$formats = @{ xlsx = 51; xls = 56 }  # xlOpenXMLWorkbook, xlExcel8

$list = Get-Content -LiteralPath $Jobs -Raw -Encoding UTF8 | ConvertFrom-Json
if (-not $list) { return }
$excel = New-Object -ComObject Excel.Application
$excel.Visible = $false
$excel.DisplayAlerts = $false
try {
    foreach ($job in $list) {
        $book = $excel.Workbooks.Open([string]$job.source)
        $excel.CalculateFull()
        $book.RemovePersonalInformation = $true
        foreach ($name in 'Author', 'Last Author', 'Company', 'Manager') {
            try { $book.BuiltinDocumentProperties.Item($name).Value = 'synthetic-sample' } catch { }
        }
        # SaveAs picks the extension from the format; write to a temporary name, then rename to the target.
        $extension = if ($job.format -eq 'xls') { '.xls' } else { '.xlsx' }
        $temporary = [System.IO.Path]::ChangeExtension([string]$job.target, ".saving$extension")
        $book.SaveAs($temporary, $formats[[string]$job.format])
        $book.Close($false)
        Move-Item -LiteralPath $temporary -Destination ([string]$job.target) -Force
        Write-Host "  workbook $($job.format): $([System.IO.Path]::GetFileName([string]$job.target))"
    }
}
finally {
    $excel.Quit()
    [void][System.Runtime.InteropServices.Marshal]::ReleaseComObject($excel)
}
