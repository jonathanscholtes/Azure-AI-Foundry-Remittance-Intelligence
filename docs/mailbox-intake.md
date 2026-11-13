# Mailbox intake (Power Automate or Logic Apps)

Remittance emails can be submitted automatically as they arrive in a mailbox, instead of being uploaded
in the portal. A flow watches the mailbox, exports each new email as a complete MIME message (`.eml`,
attachments included) and posts it to the **ACH Intake API** on the solution's API Management instance.
From there, the email takes the same path as a portal upload: stored, queued and processed, then shown
in the portal with the `email` stream badge.

```
Mailbox (folder) ──> Power Automate / Logic App
                        │  Export email (V2) -> .eml
                        │  HTTPS POST, multipart, subscription key
                        ▼
                     API Management (public gateway) ── ACH Intake API (product "ACH Intake")
                        │  outbound through its subnet in the landing-zone VNet
                        ▼
                     Container Apps private endpoint -> UI /api proxy -> API POST /api/remittances
                        ▼
                     Blob Storage (incoming) + Service Bus (remittance-intake) -> Durable Functions
```

Intake uses the same dedicated API Management instance as the [ACH Review Expert](review-agent.md), so
it needs a private deployment (`-Private -PrivateUi`). The UI and API stay private, and the mailbox
flow, which runs in Microsoft's cloud, reaches them only through APIM.

| Piece | Where |
|---|---|
| ACH Intake API, operation, product, subscription | [infra/apim/intake-api.bicep](../infra/apim/intake-api.bicep) (module of [apim.bicep](../infra/apim/apim.bicep)) |
| Upload endpoint | `POST /api/remittances` in [apps/api/app/main.py](../apps/api/app/main.py) |
| Deployment | the `apim` phase of `deploy.ps1` ([scripts/Deploy-Apim.ps1](../scripts/Deploy-Apim.ps1)) |

## The API

`POST https://<apim>.azure-api.net/ach-intake/remittances?stream=email`

- **Header:** `Ocp-Apim-Subscription-Key: <key>`.
- **Body:** `multipart/form-data` with one part named `file`, which holds the whole email. The file name
  must end in `.eml` or `.msg`.
- **Success:** `202` with `processingId`, `correlationId` and `status: Queued`.

The processing id is a hash of the message bytes, and the intake queue drops duplicate ids. A flow
retry, or the same email submitted twice, gets the same processing id and isn't processed again, so
retries are safe.

| Response | Meaning |
|---|---|
| `202` | Accepted and queued |
| `400` | The file is empty, or isn't `.eml` / `.msg` (check the part name `file` and the file name) |
| `401` | Missing or wrong subscription key (from APIM) |
| `404` | The ACH Intake API isn't on APIM: rerun the `apim` phase |
| `413` | The email is larger than the API's limit (`MAX_MSG_SIZE_MB`, default 25 MB) |
| `502` / `503` | Storage or the intake queue is unavailable: retry |

The deploy prints the full URL at the end of the `apim` phase.

## Keys

The `ACH Intake` product requires a subscription key, and keys on it can only submit emails. They can't
read runs or sign off reviews, and the review agent's key can't submit emails. The deployment creates
one subscription, `ach-intake-mailbox`, for the mailbox flow.

Read its key in the Azure portal: API Management → **Subscriptions** → `ach-intake-mailbox` →
**Show/hide keys**. Or from the CLI:

```powershell
$apimId = az apim show -g <resource-group> -n <apim-name> --query id -o tsv
az rest --method post --uri "https://management.azure.com$apimId/subscriptions/ach-intake-mailbox/listSecrets?api-version=2023-05-01-preview" --query primaryKey -o tsv
```

**For testers, use a separate subscription.** You can then revoke a tester's key without breaking the
flow, and APIM analytics shows calls per subscriber:

```powershell
az rest --method put --uri "https://management.azure.com$apimId/subscriptions/ach-intake-tester?api-version=2023-05-01-preview" `
  --body "{\"properties\":{\"displayName\":\"ACH intake tester\",\"scope\":\"$apimId/products/ach-intake\",\"state\":\"active\"}}"
az rest --method post --uri "https://management.azure.com$apimId/subscriptions/ach-intake-tester/listSecrets?api-version=2023-05-01-preview" --query primaryKey -o tsv
```

To revoke a key, regenerate it or delete the subscription. If a key leaks, regenerate it and update
the flow.

## Build the flow

The same steps work in Power Automate and in a Logic App (Consumption), because both use the Office 365
Outlook connector. A Logic App can be deployed and versioned with the rest of the infrastructure, and
it costs a few cents a day at this volume. Power Automate is easier for a business team to own, but its
HTTP action needs a Premium licence.

1. **Trigger: "When a new email arrives (V3)"** (Office 365 Outlook).
   - **Folder:** the remittance folder. For a shared mailbox, use "When a new email arrives in a shared
     mailbox (V2)".
   - **Include attachments:** No. The next step exports the complete message.
2. **Action: "Export email (V2)".**
   - **Message Id:** the trigger's *Message Id*. The output is the full MIME message.
3. **Action: HTTP.**
   - **Method:** `POST`.
   - **URI:** the intake URL.
   - **Headers:** `Ocp-Apim-Subscription-Key` = the key.
   - **Settings:** turn on **Secure inputs**, so the key doesn't appear in run history.
   - **Body:**

     ```json
     {
       "$content-type": "multipart/form-data",
       "$multipart": [
         {
           "headers": { "Content-Disposition": "form-data; name=\"file\"; filename=\"remittance-@{utcNow('yyyyMMddHHmmss')}.eml\"" },
           "body": @{body('Export_email_(V2)')}
         }
       ]
     }
     ```
4. **Action: "Move email (V2)"** to a `Processed` subfolder.
5. **Action: "Move email (V2)"** to a `Failed` subfolder, with **Configure run after** set to "HTTP has
   failed".

The mailbox then shows what happened: new mail waits in the folder, then moves to `Processed` or
`Failed`, and a failed flow run can be resubmitted from its run history.

## Test without a dedicated mailbox

You don't need a shared mailbox to test. Use a folder in your own mailbox:

1. **Folder:** in Outlook, create a folder such as `ACH Remittances`.
2. **Rule:** add an inbox rule that moves mail whose subject contains a tag, such as `[ACH-TEST]`, into
   that folder.
3. **Trigger:** point the flow's trigger at that folder. Only mail the rule moves there is read.
4. **Send:** email yourself a tagged message with the synthetic test remittances from
   [samples/synthetic](../samples/synthetic) attached. Use synthetic data only.

To check the API without the flow, post a synthetic `.msg` directly:

```powershell
curl.exe -X POST "https://<apim>.azure-api.net/ach-intake/remittances?stream=email" `
  -H "Ocp-Apim-Subscription-Key: <key>" `
  -F "file=@samples/synthetic/03-pdf-remittance-advice/03-pdf-remittance-advice.msg"
```

A `202` with a `processingId` means the whole path works. The run appears in the portal's Processing
runs, with the `email` badge, a few seconds later. Sending the same file again returns the same
processing id and is skipped as a duplicate, so use a different file for each new test.

Things to check:
- **Account:** the Office 365 connection signs in as a user, whose mailbox access decides what the flow
  can read. If the Azure subscription and your mailbox are in different tenants, conditional access
  can block the connection. If so, use an account in the tenant that owns the mailbox.
- **Production:** for production, point the trigger at a shared remittance mailbox, and authorize the
  connection with a service account that has access to it. Nothing else changes.

## Alternative: Microsoft Graph

When security requires app-only access restricted to a single mailbox (Exchange RBAC for
Applications) instead of a user-delegated connection, use Microsoft Graph instead:
- a Graph change-notification subscription on the mailbox;
- an Azure Function that receives the notification, downloads the message's MIME content
  (`/messages/{id}/$value`) and posts it to the same intake API.

That route is more to build: renewing the subscription every few days, plus a catch-up sync for missed
notifications.
