# ACH Review Expert (Microsoft 365 Copilot)

The ACH Review Expert (`ach-review-expert`) is an optional Foundry agent for cash application analysts.
It is published to Microsoft 365 Copilot and Teams as **ACH Review Copilot**, so analysts can work from
Copilot or Teams chat without opening the portal. An analyst can:

- ask about processing: what is in flight, what failed, how many runs are waiting for review today,
  everything from one payer this week;
- review a run flagged for manual review: why it was flagged, what the ACH Assurance Expert
  recommends, and what the original email and documents actually say;
- ask what the deduction policy allows (the same AI Search policy index the ACH Assurance Expert uses);
- mark the review completed, with an outcome and notes, once they have decided.

The agent never changes payment lines, totals or payers, and never reprocesses a run. It completes a
review only after the analyst confirms it, and the sign-off is recorded against that run.

## How it fits together

```
Microsoft 365 Copilot ──> Foundry agent: ach-review-expert
                              │  MCP tool (Foundry connection ach-review-mcp: APIM subscription key)
                              ▼
                          API Management (dedicated, public gateway) ── MCP server "ach-review-mcp"
                              │  ACH Review API (product "ACH Review")
                              │  outbound through its subnet in the landing-zone VNet
                              ▼
                          Container Apps private endpoint
                              ▼
                          UI container app /api proxy ──> API container app /api/review/*
                                                              │
                                     Table Storage (runs, events) + Blob Storage (email, documents)
```

The agent is for **private deployments** (`-Private -PrivateUi`). The solution deploys its own API
Management instance (Developer tier) injected into the landing-zone VNet in external mode:
- **Gateway:** stays public, because Foundry Agent Service calls the MCP server from Microsoft's network.
- **Backend calls:** go out through APIM's subnet and reach the private Container Apps environment over
  its private endpoint, the same path VPN users take to the UI.
- **Why the UI proxy:** the API's own ingress is limited to its Container Apps environment, so APIM
  can't call the API directly. It calls the UI's `/api` proxy, which forwards to the API.

| Piece | Where |
|---|---|
| Agent definition (instructions, tools) | [agents/review_agent.py](../agents/review_agent.py), deployed by [agents/deploy.py](../agents/deploy.py) |
| Review endpoints | `/api/review/*` in [apps/api/app/main.py](../apps/api/app/main.py) |
| API Management instance, NSG, public IP | [infra/apim/apim.bicep](../infra/apim/apim.bicep) |
| APIM subnet in the landing-zone VNet | [infra/core/network/apim-subnet.bicep](../infra/core/network/apim-subnet.bicep) |
| APIM API, operations (tool descriptions), product, subscription | [infra/apim/review-mcp.bicep](../infra/apim/review-mcp.bicep) |
| Foundry connection to the MCP server | [infra/core/ai/foundry-mcp-connection.bicep](../infra/core/ai/foundry-mcp-connection.bicep) |
| Deployment | [scripts/Deploy-Apim.ps1](../scripts/Deploy-Apim.ps1) (`apim` phase of `deploy.ps1`) |

### Tools

APIM's MCP export turns each operation of the ACH Review API into a tool: the tool name is the operation
name in camelCase (`list_runs` becomes `listRuns`), and the operation description in [review-mcp.bicep](../infra/apim/review-mcp.bicep) is what the agent
reads to decide when and how to call it.

| Tool | Endpoint | What it returns |
|---|---|---|
| `listRuns` | `GET /api/review/runs` | Runs filtered by review state (`open` / `completed`), status (`InFlight`, `Failed`, ...), payer, text, dates |
| `getPipelineStatus` | `GET /api/review/pipeline` | Runs in flight by stage, throughput, latency, queue depth |
| `getRun` | `GET /api/review/runs/{id}` | The review case: totals and difference, review reasons, assurance findings, resolution explanation, payment lines, email header, sources, sign-off |
| `getSourceText` | `GET /api/review/runs/{id}/sources/{sourceId}/text` | The email (`message-body`, with its header) or a document's text as Content Understanding read it (`doc-NNN`), in 8,000-character pages |
| `completeReview` | `POST /api/review/runs/{id}/complete` | Records the sign-off; `409` if the run is not in review or was already completed |

### What completing a review records

`completeReview` takes `reviewer`, `outcome` and `notes`. It merges `reviewStatus=Completed`,
`reviewedBy`, `reviewedAtUtc`, `reviewOutcome`, `reviewNotes` and `reviewChannel` onto the run's ledger
row, and appends a `review_completed` event to the run's timeline. The run's pipeline `status` stays
`ReviewRequired`, so review-rate metrics are unchanged. The portal shows the run as **Reviewed**, with
the sign-off, and no longer counts it as needing review.

The outcome decides the downstream CSV, which the pipeline holds back for every review run:

| Outcome | When | CSV |
|---|---|---|
| `approved` | The extracted payment lines are correct, even if the analyst confirmed the payer or other header details | Written at sign-off (PRO, amount; same format and `payer_date_total.csv` name as a clean run), and the portal's download appears. Refused with `409` if a line has no PRO or amount |
| `handled_manually` | One or more lines are wrong; the analyst applies the cash manually outside this system | None: a CSV of the extracted lines would be wrong |
| `rejected` | No cash is applied: not a remittance, a duplicate, or sent back to the payer | None |

The write is conditional on the row's ETag, so a reprocess or a second reviewer writing at the same
moment gets a conflict instead of overwriting. A reprocess creates a new run, which needs its own review.

The reviewer is the name the analyst gives in chat. Copilot doesn't pass the signed-in user's identity
into the agent's prompt, so the agent asks once per conversation and then reuses the answer. Calls
reach the API as the agent, not as the signed-in Copilot user, so `reviewedBy` is recorded as stated,
not authenticated.

### Security

- **APIM's gateway is the only public entry point.** It exposes only the five review operations,
  each requiring a subscription key on the `ACH Review` product. The only key is held by the Foundry
  connection `ach-review-mcp`, and that subscription is scoped to the product. The product requires
  approval, so developer-portal users can't issue themselves a key.
- **The MCP export must not open its own path.** Create the MCP server with the `ACH Review` product and
  a subscription required. A keyless MCP server would make `completeReview` callable by anyone.
- **The UI and API stay private.** The Container Apps environment has public network access disabled,
  so the `/api` proxy is reachable only from the VNet (APIM, VPN clients). Like the portal's other API
  routes, `/api/review` has no authentication of its own inside the network.
- Copilot cannot show an approval prompt for MCP calls, so the agent's instructions require the
  analyst's explicit "yes" before `completeReview`, after restating the run, outcome and notes.

## Deploy

Prerequisites:
- The solution is deployed with `-Private -PrivateUi`, so the landing zone already has the Container
  Apps private DNS zone linked to the VNet.
- The landing-zone VNet has a free `/27` for API Management's subnet.
- If the landing zone forces outbound traffic through a firewall (a route table on new subnets), APIM's
  management traffic must be allowed. See
  [Deploy API Management in a virtual network](https://learn.microsoft.com/en-us/azure/api-management/api-management-using-with-vnet).

1. **API and APIM.** The `apim` phase runs on every `-Private -PrivateUi` deployment, after the apps
   phase. Opt out with `-Skip apim`; on public deployments it is skipped. To deploy just the apps (for
   the new review endpoints) and APIM:

   ```powershell
   ./deploy.ps1 -Private -PrivateUi -Skip infra,contentunderstanding,dataseed,agents,function
   ```

   The `apim` phase ([Deploy-Apim.ps1](../scripts/Deploy-Apim.ps1)) does three things:
   - creates or reuses the `snet-apim-<project>-<environment>-<token>` subnet in the landing-zone VNet;
   - deploys the dedicated API Management instance (Developer tier, external VNet mode; the first
     deployment takes about 30–45 minutes) with the ACH Review API;
   - creates the Foundry connection.

   To run only this step: `./scripts/Deploy-Apim.ps1 -Environment <environment>`.

2. **Export the MCP server (manual, portal).** API Management → **APIs** → **MCP servers** →
   **+ Create** → **Expose an API as an MCP server**:
   - **API:** `ACH Review API`.
   - **API operations:** all five (`Complete review`, `Get pipeline status`, `Get run`,
     `Get source text`, `List runs`).
   - **Display name:** `ACH Review MCP`.
   - **Name:** exactly `ach-review-mcp`. The name sets the URL path
     `https://<apim>.azure-api.net/ach-review-mcp/mcp`, which the Foundry connection already points to.
   - **Description:** `ACH remittance runs, manual reviews, source email and documents, and review
     sign-off for the ACH Review Expert.`
   - **Products:** `ACH Review`, which requires a subscription key.

3. **Agent.** Redeploy the agents. The ACH Review Expert is skipped, with a warning, until the
   connection exists:

   ```powershell
   ./deploy.ps1 -Private -PrivateUi -Skip infra,contentunderstanding,dataseed,function,apps,apim
   ```

4. **Try it** in the Foundry portal's agent playground: "Which runs are waiting for review?"

5. **Publish to Microsoft 365 Copilot (manual).** See below.

## Publish to Microsoft 365 Copilot and Teams (manual, Foundry portal)

The agent is published with Foundry's native publishing, which creates the Azure Bot Service and the
Teams app manifest, so there is no bot or channel adapter to build. See
[Publish an agent to Microsoft 365 Copilot and Teams](https://learn.microsoft.com/en-us/azure/foundry/agents/how-to/publish-copilot).

1. [Foundry portal](https://ai.azure.com) → your project → **Agents** → open **`ach-review-expert`** →
   **Publish** → **Microsoft 365 Copilot / Teams**.
2. Set the app manifest fields. These are what analysts see; the Foundry agent name stays
   `ach-review-expert`:
   - **Name (display):** `ACH Review Copilot`
   - **Short description:** `Follow ACH remittance processing and work the runs flagged for manual review.`
   - **Description:** `ACH Review Copilot helps the cash application team follow ACH remittance
     processing and work the runs flagged for manual review. Ask what is processing right now, what
     failed, or which runs are waiting for review. For a flagged run, it explains why it was flagged,
     what the assurance check recommends, and what the original email and remittance documents actually
     say, and it answers questions about the deduction policy. When you have decided, it records your
     review sign-off (outcome and notes) after you confirm. It never changes payment lines, totals or
     payers.`
   - **Publish version:** `1.0.0`. Bump it only when republishing the app itself.
   - **Azure bot services:** keep the generated name.
   - **Developer:** `Foundry Freight Corp`.
3. **Scope:** **Just you** for a demo (self-serve). Use **Your org** only when IT is ready to approve
   the app in the Teams admin center.
4. **Publish.** The published endpoint always uses the latest agent version, so redeploying the agents
   after an instruction change takes effect without republishing the Teams / Microsoft 365 app.

**Sample prompts** (Copilot, Teams or the Foundry playground):

| Prompt | Shows |
|---|---|
| Which runs are waiting for manual review? | Open reviews with payer, date, stated total, difference and the main reason |
| What is processing right now? | Runs in flight by stage, throughput and queue depth |
| Did anything fail today? | Failed runs and why |
| Walk me through REM-0123456789ABCDEF0123. | The case: what is wrong, the assurance recommendation, and what the email and documents show |
| Show me the original email for that run. | The email body with its header, read from the source |
| Does the attached advice support the short pay? | Lines and amounts checked against the document text and the deduction policy |
| What does our policy say about early-payment discounts? | Cited policy sections from the policy index |
| Approve that review: the lines match the advice. | Restates the run, outcome and notes, asks for confirmation, then records the sign-off |

> The agent answers in conversational prose, leads with the answer and the numbers, and quotes PROs and
> amounts as stored. It reads and advises; its only write is the review sign-off, after the analyst confirms.

## Changing the review endpoints

A review endpoint is declared in three places, which must stay in step: the route in `main.py` (its
`operation_id`), the operation in `review-mcp.bicep`, and `ALLOWED_TOOLS` in `review_agent.py`, which uses
the tool names APIM exports: the operation name in camelCase (`list_runs` becomes `listRuns`). After a
change, rerun the `apim` phase and update the MCP server's operations in the portal.
`apps/api/tests/test_review_api.py` fails if the three names drift apart.

## Troubleshooting

| Symptom | Check |
|---|---|
| `agents/deploy.py` warns `Skipped ach-review-expert` | The Foundry connection `ach-review-mcp` does not exist: run the `apim` phase |
| Tool calls fail with `404` from the API | The apps phase has not been redeployed since the review endpoints were added |
| MCP calls fail with `401` from APIM | The MCP server is not attached to the `ACH Review` product, or the connection key is stale: rerun the `apim` phase |
| The agent lists no tools | The MCP server name is not `ach-review-mcp`, or the export has no operations |
| `Deploy-Apim.ps1` says it is for private deployments | The solution was not deployed with `-Private -PrivateUi` |
| Tool calls fail with `502`/timeouts from APIM | APIM cannot resolve or reach the UI. Check that the `privatelink.<region>.azurecontainerapps.io` zone is linked to the VNet, and that no route table or firewall on the APIM subnet blocks it |
| APIM deployment fails on the subnet or NSG | A landing-zone policy may have added a route table or denied the NSG rules. See APIM's [VNet requirements](https://learn.microsoft.com/en-us/azure/api-management/api-management-using-with-vnet) |

## Removing it

Deleting the solution's resource group removes the API Management instance. It is soft-deleted for
48 hours, which blocks redeploying the same name until it is purged:

```powershell
az apim deletedservice purge --service-name <apim-name> --location <region>
```

The APIM subnet lives in the landing-zone VNet: delete `snet-apim-<project>-<environment>-<token>` after
the instance is gone.
