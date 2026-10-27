<#
.SYNOPSIS
    Assemble Outlook .msg files from a JSON manifest written by generate.py (Windows + Outlook).

.DESCRIPTION
    Uses the Outlook object model to create each message and SaveAs .msg (Unicode). Nothing is sent
    and nothing is saved to a mailbox folder: every item is discarded after SaveAs.

    Each entry: {path, subject, to, fromName, fromEmail, sentUtc ("yyyy-MM-dd HH:mm"), bodyText |
    bodyHtml, attachments: [{path, cid}]}. An attachment that is a .msg is embedded as an email
    (a forwarded or attached message); a cid marks an inline image referenced by the HTML body.
    The sender, the dates and the "sent" flag are set as MAPI properties so the file reads like a
    received email rather than an unsent draft.
#>
param([Parameter(Mandatory)][string]$Manifest)

$ErrorActionPreference = 'Stop'
$tag = 'http://schemas.microsoft.com/mapi/proptag/'
$generator = 'Synthetic Sample Generator'

$entries = Get-Content -LiteralPath $Manifest -Raw -Encoding UTF8 | ConvertFrom-Json
$outlook = New-Object -ComObject Outlook.Application
foreach ($entry in $entries) {
    $mail = $outlook.CreateItem(0)  # olMailItem
    $mail.Subject = $entry.subject
    $mail.To = $entry.to
    if ($entry.bodyHtml) { $mail.HTMLBody = $entry.bodyHtml } else { $mail.Body = $entry.bodyText }

    $sent = [datetime]::ParseExact($entry.sentUtc, 'yyyy-MM-dd HH:mm', [Globalization.CultureInfo]::InvariantCulture)
    $props = $mail.PropertyAccessor
    $props.SetProperty($tag + '0x0E070003', 1)                  # PR_MESSAGE_FLAGS = read (not unsent)
    $props.SetProperty($tag + '0x0C1A001F', $entry.fromName)    # PR_SENDER_NAME
    $props.SetProperty($tag + '0x0C1E001F', 'SMTP')             # PR_SENDER_ADDRTYPE
    $props.SetProperty($tag + '0x0C1F001F', $entry.fromEmail)   # PR_SENDER_EMAIL_ADDRESS
    $props.SetProperty($tag + '0x5D01001F', $entry.fromEmail)   # PR_SENDER_SMTP_ADDRESS
    $props.SetProperty($tag + '0x0042001F', $entry.fromName)    # PR_SENT_REPRESENTING_NAME
    $props.SetProperty($tag + '0x0064001F', 'SMTP')             # PR_SENT_REPRESENTING_ADDRTYPE
    $props.SetProperty($tag + '0x0065001F', $entry.fromEmail)   # PR_SENT_REPRESENTING_EMAIL_ADDRESS
    $props.SetProperty($tag + '0x5D02001F', $entry.fromEmail)   # PR_SENT_REPRESENTING_SMTP_ADDRESS
    $props.SetProperty($tag + '0x00390040', $sent)              # PR_CLIENT_SUBMIT_TIME (UTC)
    $props.SetProperty($tag + '0x0E060040', $sent)              # PR_MESSAGE_DELIVERY_TIME (UTC)
    foreach ($name in '0x3FF8001F', '0x3FFA001F') {             # creator / last modifier: not the local user
        try { $props.SetProperty($tag + $name, $generator) } catch { }
    }

    foreach ($attachment in @($entry.attachments)) {
        if (-not $attachment) { continue }
        $added = $mail.Attachments.Add($attachment.path)
        if ($attachment.cid) {
            $added.PropertyAccessor.SetProperty($tag + '0x3712001F', $attachment.cid)  # PR_ATTACH_CONTENT_ID
            $added.PropertyAccessor.SetProperty($tag + '0x7FFE000B', $true)           # PR_ATTACHMENT_HIDDEN
        }
    }

    $mail.SaveAs($entry.path, 9)  # olMSGUnicode
    $mail.Close(1)                # olDiscard
    [void][System.Runtime.InteropServices.Marshal]::ReleaseComObject($mail)
}
Write-Host "built $(@($entries).Count) message(s)"
