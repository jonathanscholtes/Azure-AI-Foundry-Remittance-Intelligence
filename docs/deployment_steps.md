# Deployment Guide

> Back to the [README](../README.md)

This guide deploys the ACH Remittance Intelligence solution to an Azure subscription. One PowerShell script provisions the Azure resources with Bicep and configures Content Understanding in Microsoft Foundry, the policy index, the Foundry agents, the Functions app and the container apps.

## Contents

- [Prerequisites](#prerequisites)
- [Step 1 — Clone the Repository](#step-1--clone-the-repository)
- [Step 2 — Login to Azure](#step-2--login-to-azure)
- [Step 3 — Configure for Your Organization](#step-3--configure-for-your-organization)
- [Step 4 — Deploy Everything](#step-4--deploy-everything)
- [Step 5 — Verify the Deployment](#step-5--verify-the-deployment)
- [Private Deployment (Landing Zone)](#private-deployment-landing-zone)
- [Optional: Copilot Agent and Mailbox Intake](#optional-copilot-agent-and-mailbox-intake)
- [Redeploying and Running One Phase](#redeploying-and-running-one-phase)
- [Security Model](#security-model)
- [Troubleshooting](#troubleshooting)
- [Teardown](#teardown)

---

## Prerequisites

### Tools

| Tool | Version | Install |
|---|---|---|
| Azure CLI (with Bicep) | 2.60+ | `winget install Microsoft.AzureCLI`, then `az bicep install` |
| PowerShell | 5.1+ (Windows) or 7+ | Included with Windows; `winget install Microsoft.PowerShell` |
| Python | 3.11+ | `winget install Python.Python.3.11` |
| Azure Functions Core Tools | v4 | `winget install Microsoft.Azure.FunctionsCoreTools` |
| Git | any | `winget install Git.Git` |

> Docker is not needed: the API and UI images are built in Azure Container Registry (`az acr build`). Node.js is only needed to run the UI locally.

### Azure Access Requirements

| Requirement | Why |
|---|---|
| **Owner**, or **Contributor** + **User Access Administrator**, on the subscription (or a pre-created resource group) | The deployment creates role assignments for the managed identity, the Foundry project and the deploying user |
| Resource providers registered: `Microsoft.CognitiveServices`, `Microsoft.Web`, `Microsoft.App`, `Microsoft.ServiceBus`, `Microsoft.Search`, `Microsoft.Storage`, `Microsoft.ContainerRegistry`, `Microsoft.OperationalInsights`, `Microsoft.Insights`, `Microsoft.Dashboard`, and for private deployments `Microsoft.ApiManagement` and `Microsoft.Network` | Resource types the templates create |

### Model Quota and Region

Choose a region where **Content Understanding** is available and where you have quota for these deployments:

| Deployment | Model | Used by |
|---|---|---|
| `cu-gpt-55` | GPT-5.5 (Data Zone Standard) | Content Understanding extraction, the Extraction and Assurance experts |
| `reasoning-gpt-56-terra` | GPT-5.6 Terra | Resolution Expert |
| `intake-decision` | Microsoft-Decision-1 | Source and thread classification |
| `cu-text-embedding-3-large` | text-embedding-3-large | Content Understanding |

> The solution is tested in **West US 3** (`westus3`).

---

## Step 1 — Clone the Repository

```powershell
git clone <repository-url>
cd <repository-folder>
```

## Step 2 — Login to Azure

```powershell
az login
az account set --subscription <subscription-id>
```

## Step 3 — Configure for Your Organization

Settings that differ by organization live in `.env` at the repository root. It is git-ignored, so organization-specific values never enter the repository. Create it before the first deployment and keep a copy somewhere safe: a fresh clone does not have it.

```powershell
Copy-Item .env.example .env
```

> [!IMPORTANT]
> Set the PRO settings when your PRO numbers have a specific length or suffixes. Without them the solution uses neutral rules (6–12 digits, no suffixes): PROs with a suffix are flagged as *not in standard format* instead of being normalized, and runs that would publish a CSV go to review.

Example (fictional values):

```ini
# .env
RECEIVING_ORG_ALIASES=Contoso Freight,Contoso Freight Lines,CONTOSO
PRO_MIN_DIGITS=9
PRO_MAX_DIGITS=10
PRO_STRIP_SUFFIXES=XY,X
PRO_SUFFIX_EXAMPLES=-X1,XY1
PRO_CREDIT_MEMO_SUFFIX=-CR1
```

| Variable | Default | Meaning | Effect |
|---|---|---|---|
| `RECEIVING_ORG_ALIASES` | empty | Comma-separated names of the organization that *receives* the payments (the payee) | The payee is never recorded as the payer |
| `PRO_MIN_DIGITS`, `PRO_MAX_DIGITS` | `6`, `12` | Length of a valid PRO in digits, after suffixes are removed. Leading zeros count and are kept | Lines outside the range are flagged *PRO not in standard format* |
| `PRO_STRIP_SUFFIXES` | none | Comma-separated trailing suffixes removed before validating, matched case-insensitively at the end, longest first. `001234567X` → `001234567` | The CSV carries the base PRO |
| `PRO_SUFFIX_EXAMPLES` | none | Comma-separated suffixes payers write on PROs that are *not* stripped, so the analyst knows what to look for | Quoted in the review guidance for non-standard PROs |
| `PRO_CREDIT_MEMO_SUFFIX` | `-CR1` (generic example) | The suffix your organization uses on credit or debit-memo PROs | Used in the ACH Assurance Expert's instructions |

How the settings are applied:

- `deploy.ps1` loads `.env`. The infrastructure phase writes `RECEIVING_ORG_ALIASES` and `PRO_MIN_DIGITS` … `PRO_SUFFIX_EXAMPLES` to the Function app settings and prints the PRO rules it applied; the agents phase uses `PRO_CREDIT_MEMO_SUFFIX`. The phase scripts also load `.env` when run on their own.
- They are app settings, so publishing new code never resets them. Running the infrastructure phase **without** `.env` does: it sets them empty (neutral rules) and prints a warning.
- After a change, run the infrastructure phase (`./scripts/Deploy-Infrastructure.ps1 -Environment <env> -Location <region>`, with the same `-Private` / `-PrivateUi` switches you deployed with) and, for `PRO_CREDIT_MEMO_SUFFIX`, the agents phase (`./scripts/Deploy-FoundryAgents.ps1 -Environment <env>`).
- A parameter passed to `deploy.ps1`, or a real environment variable, wins over the value in `.env`.
- These settings describe rules, not who the organization is: put nothing else that identifies it in `.env` beyond the receiving organization's names.

## Step 4 — Deploy Everything

```powershell
./deploy.ps1 -Subscription <subscription-id> -Location westus3 -Environment dev
```

Resources are created in the resource group `rg-ach-remit-<environment>`. The script runs six phases, plus a seventh on private deployments:

| Phase | `-Skip` name | Script | What it does |
|---|---|---|---|
| 1 | `infra` | `scripts/Deploy-Infrastructure.ps1` | Deploys `infra/main.bicep`: Foundry account and project, model deployments, AI Search, Storage, Service Bus, Function app, Container Registry, Container Apps environment, Log Analytics, Application Insights and the dashboard |
| 2 | `contentunderstanding` | `scripts/Deploy-ContentUnderstanding.ps1` | Sets the Content Understanding model defaults and creates the `ach.remittance.v1` analyzer |
| 3 | `dataseed` | `scripts/Deploy-DataSeed.ps1` | Creates the `ach-policy` index and loads the deduction-policy corpus |
| 4 | `agents` | `scripts/Deploy-FoundryAgents.ps1` | Creates new versions of the Foundry agents (Extraction, Resolution and Assurance experts, and the ACH Review Expert once its MCP connection exists) |
| 5 | `function` | `scripts/Deploy-FunctionApp.ps1` | Publishes the Functions app (remote build) |
| 6 | `apps` | `scripts/Deploy-Applications.ps1` | Builds the API and UI images and deploys the container apps |
| 7 | `apim` | `scripts/Deploy-Apim.ps1` | `-Private -PrivateUi` only: deploys the dedicated API Management instance in the landing-zone VNet, with the ACH Review API and the Foundry MCP connection, for the [ACH Review Expert](review-agent.md), and the ACH Intake API for [mailbox intake](mailbox-intake.md) (first deployment ~30–45 minutes; later runs a few minutes) |

The script ends with a phase checklist and the service endpoints, including the **UI URL**.

### Optional Parameters

| Parameter | Default | Description |
|---|---|---|
| `-Location` | `eastus2` | Azure region |
| `-Environment` | `dev` | Environment name, part of every resource name |
| `-Skip` | none | Phases to skip, e.g. `-Skip infra,contentunderstanding` |
| `-SkipTests` | `$true` | `$false` runs the unit tests before deploying |
| `-FunctionPlanSku` | `B1` | Function plan size; use `S1` or `P1v3` for load tests |
| `-MaxConcurrentActivities` | `8` | Concurrent Durable activities per instance (throttles model calls) |
| `-DevPrincipalId` | signed-in user | Object ID of the user granted data-plane access |
| `-Private` | off | Deploy into a landing-zone VNet ([below](#private-deployment-landing-zone)) |
| `-PrivateUi` | off | With `-Private`: the UI is reachable only from the VNet |
| `-LandingZoneTag` | `role=private-landing-zone` | Tag that identifies the landing-zone VNet's resource group |

### Resources Created

| Resource | Purpose |
|---|---|
| Microsoft Foundry account and project | Content Understanding, model deployments, agents |
| Azure AI Search (Basic) | Deduction-policy index for the Assurance Expert |
| Storage account | Originals, attachments, analysis, results, run ledger, live events, audit trail; Durable task hub |
| Service Bus (Standard) | `remittance-intake` queue with dead-letter |
| App Service plan + Function app | Durable orchestration and activities |
| Container Registry, Container Apps environment | `ach-remit-api` (internal ingress) and `ach-remit-ui` |
| User-assigned managed identity | Keyless access for the Function and container apps |
| Log Analytics, Application Insights | Traces and run events |
| Azure Monitor dashboard with Grafana | **ACH Remittance Processing** dashboard |
| API Management (Developer), public IP, NSG and `/27` subnet | Private deployments only (`apim` phase): the only public entry point, with the ACH Review API (the Copilot agent's MCP server) and the ACH Intake API (mailbox flow) |
| Foundry project connection `ach-review-mcp` | Private deployments only: the MCP server URL and the APIM subscription key the ACH Review Expert uses |

Created by hand, not by the deployment ([below](#optional-copilot-agent-and-mailbox-intake)): the APIM MCP server (a portal export), the Azure Bot Service and Teams app (created by publishing the agent to Microsoft 365 Copilot from the Foundry portal), and the mailbox flow (Power Automate or a Logic App).

> **Cost:** model usage, Content Understanding and the other services are billable. Each run records its own usage and estimated cost.

---

## Step 5 — Verify the Deployment

1. Open the **UI URL** printed at the end of the deployment.
2. Upload a remittance email (`.msg` or `.eml`) on **File Upload**, or run the smoke test:

   ```powershell
   ./scripts/Test-Remittance.ps1 -MsgPath <path-to-message.msg> -ApiUrl https://<ui-fqdn>
   ```

3. Watch the run on **Live Pipeline**, then open it under **Processing Runs**.

**Checklist:**

- [ ] The deployment summary shows `[OK]` for every phase
- [ ] The infrastructure phase printed `PRO rules: …` with your rules (not the *PRO_\* not set* warning, unless you need none)
- [ ] The UI loads and the Live Pipeline card shows *live*
- [ ] An upload reaches **Complete** (CSV published) or **Review Required**, with a timeline
- [ ] Run Details shows payment lines and a **Cost and usage** card
- [ ] In the Azure portal, the **ACH Remittance Processing** dashboard (Azure Monitor → Dashboards with Grafana) shows the run within a few minutes

To generate traffic, replay messages from a local folder of `.msg` / `.eml` files (see [Load Testing](load-testing.md)):

```powershell
python test/load/load_test.py --api https://<ui-fqdn> --source-dir <folder> --count 25 --interval-seconds 10
```

---

## Private Deployment (Landing Zone)

`-Private` deploys into an existing landing-zone VNet. Storage gets private endpoints and the Function app and the Container Apps environment are VNet-integrated.

```powershell
./deploy.ps1 -Subscription <subscription-id> -Location westus3 -Environment dev -Private -PrivateUi
```

**The landing zone must provide:**

| Item | Detail |
|---|---|
| VNet resource group tag | `role=private-landing-zone` (or pass `-LandingZoneTag`) |
| Region | The VNet's region; pass it as `-Location` |
| Address space | A free `/26` (Function subnet), `/23` (Container Apps subnet) and, with `-PrivateUi`, `/27` (API Management subnet); the scripts create the subnets |
| Private-endpoint subnet | A non-delegated subnet; `snet-pe` is preferred, or pass `-PrivateEndpointSubnetResourceId` |
| Private DNS zones | `privatelink.blob.core.windows.net`, `privatelink.queue.core.windows.net` and `privatelink.table.core.windows.net` in the landing-zone resource group, linked to the VNet. With `-PrivateUi` the script creates `privatelink.<region>.azurecontainerapps.io` if it is missing |
| DNS for clients | A DNS resolver (or forwarder) so VPN clients resolve private-endpoint names to private IPs |
| Network access for the deployment | Run the deployment from a machine on the VNet or its VPN: publishing the Function app needs the storage private endpoint |
| Firewall egress | If outbound traffic is force-tunneled, allow the endpoints listed in [infra/README.md](../infra/README.md#firewall-egress-private-mode) |

**`-PrivateUi`** adds a private endpoint for the Container Apps environment and disables its public access. The UI keeps its URL, which resolves to the private endpoint for clients on the VNet or VPN. Pass `-PrivateUi` on every infrastructure deployment. Without it, the infrastructure phase tries to re-enable public access, and Azure refuses while the private endpoint exists ([Troubleshooting](#troubleshooting)).

With `-Private -PrivateUi`, the `apim` phase also deploys the solution's own API Management instance, injected into the landing-zone VNet in external mode. Its gateway is public, so Microsoft 365 Copilot (through the Foundry agent) and a mailbox flow can reach the solution. It carries only the subscription-keyed review and intake APIs, and its backend calls reach the private UI's `/api` proxy over the Container Apps private endpoint. The first deployment takes 30–45 minutes; later runs take a few minutes. Opt out with `-Skip apim`.

> In this sample the Foundry account, AI Search and Service Bus (Standard) stay publicly reachable, with Microsoft Entra-only authentication. For production, add private endpoints for Foundry and AI Search, and use Service Bus Premium with a private endpoint.

---

## Optional: Copilot Agent and Mailbox Intake

Both build on the `apim` phase of a private deployment and each needs a few manual steps:

| Feature | What the deployment creates | Manual steps | Guide |
|---|---|---|---|
| **ACH Review Copilot**: analysts work exceptions in Microsoft 365 Copilot and Teams | APIM with the ACH Review API, the Foundry connection `ach-review-mcp`, and the ACH Review Expert agent (agents phase, once the connection exists) | 1. Export the ACH Review API as the MCP server `ach-review-mcp` in the APIM portal. 2. Rerun the agents phase. 3. Publish the agent to Microsoft 365 Copilot from the Foundry portal; publishing creates the Azure Bot Service and the Teams app | [ACH Review Expert](review-agent.md) |
| **Mailbox intake**: remittance emails submitted automatically | APIM with the ACH Intake API, the `ACH Intake` product and the `ach-intake-mailbox` subscription | Build the Power Automate flow or Logic App (trigger on the mailbox folder, export the email, POST it with the key) | [Mailbox intake](mailbox-intake.md) |

After the MCP export, redeploy only the agents:

```powershell
./deploy.ps1 -Subscription <subscription-id> -Location westus3 -Environment dev -Private -PrivateUi -Skip infra,contentunderstanding,dataseed,function,apps,apim
```

---

## Redeploying and Running One Phase

The deployment is idempotent. To redeploy only code:

```powershell
./deploy.ps1 -Subscription <subscription-id> -Location westus3 -Environment dev -Skip infra,contentunderstanding,dataseed,agents
```

Each phase script also runs on its own and reads its inputs from the infrastructure deployment's outputs:

```powershell
./scripts/Deploy-FunctionApp.ps1 -Environment dev
./scripts/Deploy-Applications.ps1 -Environment dev
./scripts/Deploy-FoundryAgents.ps1 -Environment dev
./scripts/Deploy-Infrastructure.ps1 -Environment dev -Location westus3 -WhatIf   # preview infrastructure changes
```

Pass the same `-Private` / `-PrivateUi` switches you deployed with whenever you run the infrastructure phase.

---

## Security Model

- **No keys or connection strings.** The Function app and container apps use one user-assigned managed identity. Storage shared-key access and Service Bus local authentication are disabled; Foundry and AI Search are called with Microsoft Entra tokens.
- **The API is internal.** `ach-remit-api` has internal ingress only; the UI's nginx proxies `/api` to it inside the Container Apps environment.
- **The UI** is public by default (no sign-in in this sample). Use `-Private -PrivateUi` to make it reachable only from your network, or put it behind your organization's edge (for example Azure Front Door with WAF and Private Link).
- **API Management is the only public entry point** in a private deployment. It exposes only the review operations (for the Copilot agent) and the email-submit operation (for the mailbox flow). Each API is behind its own product and subscription key, and neither key can call the other API. The products require approval, so developer-portal users can't issue themselves keys. The Copilot agent's only write is the review sign-off, made after the analyst confirms it. The reviewer is recorded as the analyst names themselves in chat, not authenticated.
- **Least-privilege roles.** The managed identity gets only the data-plane roles it needs (Storage Blob Data Owner, Table and Queue Data Contributor, Service Bus Data Owner, Cognitive Services User, Azure AI User, Content Understanding Reader, Search Index Data Reader, AcrPull). The Foundry project identity gets Search Index Data Contributor for the agent's search tool.
- **Data stays in your subscription.** Originals, extracted data and results are stored only in your storage account. Do not commit real remittances to the repository: `samples/` is git-ignored except the invented `samples/synthetic/` set.

---

## Troubleshooting

| Symptom | Cause and fix |
|---|---|
| `Could not resolve your Microsoft Entra object id` | Conditional Access token challenge, common right after connecting a VPN. Run `az login` again (if it persists, `az login --scope https://graph.microsoft.com/.default`), or pass `-DevPrincipalId <object-id>`. |
| Content Understanding phase returns `500 InternalServerError` | A new Foundry account can take minutes before its model deployments accept analyzer writes. The script retries; if it still fails, re-run the phase later (`./scripts/Deploy-ContentUnderstanding.ps1 -Environment dev`). |
| `FlagMustBeSetForRestore` when recreating an environment | A soft-deleted Foundry account with the same name exists. The infrastructure phase purges it automatically for the same resource group; otherwise purge it ([Teardown](#teardown)), or use a new `-Environment` name. |
| Data seed or agents phase fails with `403` | New role assignments can take several minutes to apply. The scripts retry; if they still fail, re-run the phase. |
| Function publish fails or times out (private) | Publishing needs network access to the storage private endpoint. Connect to the VNet or its VPN, check that `<account>.blob.core.windows.net` resolves to a private IP (`Test-NetConnection <account>.blob.core.windows.net -Port 443`), and re-run `./scripts/Deploy-FunctionApp.ps1`. |
| `az acr build` is slow or hangs | The build context includes `node_modules`. Keep the patterns in `apps/*/.dockerignore` without a trailing slash (`node_modules`, not `node_modules/`). |
| Many runs go to review with *PROs not in standard format* | Your PRO length or suffixes aren't set, or the infrastructure phase ran without `.env` (it printed *PRO_\* not set*). Check the `PRO_*` app settings on the Function app, fix `.env` ([Step 3](#step-3--configure-for-your-organization)) and re-run the infrastructure phase. |
| A run stays **Queued** for more than a minute | Look at **Live Pipeline**: messages counted under *Queued (Service Bus)* with nothing *In flight* mean the Function app isn't reading the queue. Check that it is running and that **Always On** is on: the app has no HTTP triggers, so on a Dedicated plan it idles without Always On and nothing wakes it. Fix with `az functionapp config set -g <resource-group> -n <function-app> --always-on true` and restart the app; queued messages then start within about a minute. Scaling the plan down to a Free or Shared tier (for example by a cost-saving schedule) turns Always On off, and scaling back up does not restore it. Messages in *Dead-letter* failed repeatedly; replay them from Live Pipeline. |
| UI shows `RBAC: access denied`, or doesn't resolve, with `-PrivateUi` | You are not reaching it through the VNet. Connect the VPN; a secure-access client that tunnels Azure traffic must be off, or bypass `*.azurecontainerapps.io`. |
| A phase fails with `Could not read 'main' deployment outputs` | The last infrastructure deployment failed, so it recorded no outputs. A common cause is running the infrastructure phase without `-PrivateUi` on a private-UI environment, which fails with `ManagedEnvironmentInvalidPublicNetworkAccessWithPrivateEndpoint`. Re-run the infrastructure phase with the same `-Private -PrivateUi` switches, then the phase that failed. |
| The ACH Review Expert is skipped by the agents phase, or the Copilot agent sees no tools | The Foundry connection or the MCP export is missing. See the troubleshooting table in [ACH Review Expert](review-agent.md#troubleshooting). |
| Mailbox flow gets `401` / `400` / `413` from the intake URL | Wrong key, a file not named `.eml`/`.msg`, or an email over the size limit. See [Mailbox intake](mailbox-intake.md#the-api). |
| Azure portal can't list private storage containers ("cannot access storage endpoint") | The browser blocks a public site from calling a private IP. On the portal tab: site information icon → **Permissions for this site** → **Local network access** → **Allow**, then reload. Or use `az storage ... --auth-mode login` over the VPN. |

---

## Teardown

```powershell
az group delete --name rg-ach-remit-<environment> --yes
```

Then clean up what is outside the resource group:

```powershell
# Foundry accounts are soft-deleted; purge to release the name and quota.
az cognitiveservices account list-deleted --query "[?contains(name,'ach-remit-<environment>')].{name:name,location:location}" -o table
az cognitiveservices account purge -g rg-ach-remit-<environment> -n <name> -l <location>

# API Management (private deployments) is soft-deleted for 48 hours; purge to reuse the name.
az apim deletedservice list --query "[?contains(name,'ach-remit-<environment>')].{name:name,location:location}" -o table
az apim deletedservice purge --service-name <name> --location <location>

# Private deployments: the delegated subnets and the APIM subnet live in the landing-zone VNet.
az network vnet subnet list -g <landing-zone-rg> --vnet-name <vnet> --query "[?contains(name,'ach-remit-<environment>')].name" -o tsv
az network vnet subnet delete -g <landing-zone-rg> --vnet-name <vnet> -n <subnet-name>
```

With `-PrivateUi`, also remove the environment's record from `privatelink.<region>.azurecontainerapps.io` in the landing-zone resource group (or the zone and its VNet link, if nothing else uses it).

If you published the Copilot agent, remove the Teams / Microsoft 365 app, and delete the Azure Bot Service that publishing created if it is outside the resource group. Turn off or delete the mailbox flow, which would otherwise keep calling a URL that no longer exists.
