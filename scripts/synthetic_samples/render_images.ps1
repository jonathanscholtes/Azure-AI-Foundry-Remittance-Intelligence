<#
.SYNOPSIS
    Render synthetic remittance images (PNG, TIFF, JPEG) from a JSON job list written by generate.py.

.DESCRIPTION
    Each job is {path, format: png|tiff|jpeg, width, height, scan, items: [...]}. An item is either
    {kind: "text", text, x, y, size, bold, font, color} or {kind: "rect", x, y, w, h, color}.
    scan = true tilts the page slightly and adds speckle, like a scanned or faxed copy.
#>
param([Parameter(Mandatory)][string]$Jobs)

$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Drawing

function ConvertTo-Color([string]$hex) {
    if (-not $hex) { return [System.Drawing.Color]::Black }
    return [System.Drawing.ColorTranslator]::FromHtml($hex)
}

$list = Get-Content -LiteralPath $Jobs -Raw -Encoding UTF8 | ConvertFrom-Json
foreach ($job in $list) {
    $bitmap = New-Object System.Drawing.Bitmap ([int]$job.width), ([int]$job.height)
    $graphics = [System.Drawing.Graphics]::FromImage($bitmap)
    $graphics.Clear([System.Drawing.Color]::White)
    $graphics.TextRenderingHint = [System.Drawing.Text.TextRenderingHint]::AntiAliasGridFit
    foreach ($item in $job.items) {
        $color = ConvertTo-Color $item.color
        if ($item.kind -eq 'rect') {
            $brush = New-Object System.Drawing.SolidBrush $color
            $graphics.FillRectangle($brush, [single]$item.x, [single]$item.y, [single]$item.w, [single]$item.h)
            $brush.Dispose()
            continue
        }
        $style = if ($item.bold) { [System.Drawing.FontStyle]::Bold } else { [System.Drawing.FontStyle]::Regular }
        $family = if ($item.font) { $item.font } else { 'Arial' }
        $font = New-Object System.Drawing.Font($family, [single]$item.size, $style, [System.Drawing.GraphicsUnit]::Pixel)
        $brush = New-Object System.Drawing.SolidBrush $color
        $graphics.DrawString([string]$item.text, $font, $brush, [single]$item.x, [single]$item.y)
        $brush.Dispose(); $font.Dispose()
    }
    $graphics.Dispose()

    if ($job.scan) {
        $scanned = New-Object System.Drawing.Bitmap ([int]$job.width), ([int]$job.height)
        $g = [System.Drawing.Graphics]::FromImage($scanned)
        $g.Clear([System.Drawing.Color]::FromArgb(246, 245, 240))
        $g.TranslateTransform($job.width / 2, $job.height / 2)
        $g.RotateTransform(0.6)
        $g.TranslateTransform(-$job.width / 2, -$job.height / 2)
        $g.DrawImage($bitmap, 0, 0)
        $g.ResetTransform()
        $random = New-Object System.Random 7
        $speck = New-Object System.Drawing.SolidBrush ([System.Drawing.Color]::FromArgb(150, 90, 90, 90))
        for ($i = 0; $i -lt 2500; $i++) {
            $size = $random.Next(1, 3)
            $g.FillRectangle($speck, $random.Next(0, $job.width), $random.Next(0, $job.height), $size, $size)
        }
        $speck.Dispose(); $g.Dispose(); $bitmap.Dispose()
        $bitmap = $scanned
    }

    $format = switch ($job.format) {
        'png' { [System.Drawing.Imaging.ImageFormat]::Png }
        'tiff' { [System.Drawing.Imaging.ImageFormat]::Tiff }
        'jpeg' { [System.Drawing.Imaging.ImageFormat]::Jpeg }
        default { throw "unknown format $($job.format)" }
    }
    $bitmap.Save($job.path, $format)
    $bitmap.Dispose()
}
Write-Host "rendered $(@($list).Count) image(s)"
