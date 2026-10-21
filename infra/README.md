# Infrastructure

`main.bicep` provisions:

- **Function App** on a Dedicated (App Service) plan (`functionPlanSku`, default `B1`; use `S1`/`P1v3` for load tests), with identity-only `AzureWebJobsStorage` (also hosts the Durable task hub), an identity-based Service Bus connection, and `maxConcurrentActivities` (default 8) as the Content Understanding / agent throttle.
- **Service Bus (Standard)** namespace + `remittance-intake` queue: local/SAS auth disabled, duplicate detection on `MessageId = processingId` (10 min), `maxDeliveryCount` 5 then dead-letter.
- **Storage**: Blob containers (content plane), the `remittances` ledger table and the `remittanceevents` timeline table. Shared-key and anonymous access are disabled.
- **Microsoft Foundry** account (Content Understanding; GPT-5.5 `DataZoneStandard` + `text-embedding-3-large`), a **Foundry project** for agents, **Azure AI Search** (`basic`, semantic ranker) and the project → Search connection (`ach-policy-search`, Entra auth).
- **Monitoring**: Log Analytics + Application Insights.
- **Demo platform**: Azure Container Registry + Container Apps environment; the `api`/`ui` apps are deployed by `apps.bicep`.

Identity and roles (one shared user-assigned identity for the Function and both container apps): Storage Blob Data Owner, Queue Data Contributor, Table Data Contributor, AcrPull, Content Understanding Reader, Cognitive Services User + Azure AI User (agent invoke), Search Index Data Reader, Azure Service Bus Data Owner (send/receive + queue metrics). The Foundry project identity holds Search Index Data Contributor (required by the agent's search tool). The deploying user gets Content Understanding Contributor, Storage Blob/Table Data Contributor, Azure AI User + Developer, Search Index Data Contributor + Search Service Contributor, and Service Bus Data Owner. Dataverse remains a later phase.

## Networking

By default Storage is public-network reachable but Entra-only. With `deploy.ps1 -Private`, the script discovers the landing-zone resource group by `-LandingZoneTag` (default `role=private-landing-zone`) and resolves the network inputs: it prefers a non-delegated `snet-pe` subnet for private endpoints, reuses or creates a per-deployment `/26` Function subnet delegated to **`Microsoft.Web/serverFarms`** (an older subnet delegated to `Microsoft.App/environments` from the Flex pattern is re-delegated if unused), and a `/23` Container Apps subnet delegated to `Microsoft.App/environments`. Storage gets blob/queue/table private endpoints; the central `privatelink.{blob,queue,table}.core.windows.net` zones must exist in the landing-zone resource group and be linked to the VNet. The deployment location must match the VNet region.

The Foundry endpoint, Service Bus (Standard has no private endpoints) and AI Search remain publicly reachable with Entra-only access in this POC. Production hardening: Service Bus Premium + private endpoint, Search and Foundry private endpoints.

The Container Apps environment is VNet-integrated (outbound to private storage) and keeps public ingress for the `ui` app unless `-PrivateUi` is set (below); the `api` app uses internal ingress. Function publishing in private mode needs line-of-sight to the blob private endpoint (VPN).

### Private UI (VNet / VPN only)

`./deploy.ps1 ... -Private -PrivateUi` makes the UI reachable only from the landing-zone VNet, e.g.
over the point-to-site VPN, without recreating anything:

- the infra script creates `privatelink.<region>.azurecontainerapps.io` in the landing-zone resource
  group and links it to the VNet (if missing);
- `main.bicep` adds a private endpoint for the Container Apps environment in the private-endpoint
  subnet (group `managedEnvironments`) and sets the environment's `publicNetworkAccess` to `Disabled`.

The UI keeps its URL; on the VPN it resolves (through the landing zone's DNS resolver) to the private
endpoint. Off the VPN it doesn't resolve to anything reachable. Pass `-PrivateUi` on every infra deploy,
or the next one re-enables public access. A secure-access client that tunnels Azure traffic (e.g.
Global Secure Access) can capture the UI's name before the VPN does; turn it off, or bypass
`*.azurecontainerapps.io`, while using the UI. Tools that call the UI URL (the golden runner's
`--mode api`, the load test) then also need the VPN.

### Browsing private storage in the Azure portal

In private mode, storage is reachable only through its private endpoints, so the portal's container
browser works only over the VPN, and the browser has to allow it. The portal (a public site) calls the
storage account's private IP directly from your browser, and current Edge and Chrome block that by
default ("Network request failed - cannot access storage endpoint"; in the console, *Permission was
denied for this request to access the `local` address space*). Fix: on the portal tab, open the site
information icon in the address bar > **Permissions for this site** > **Local network access** >
**Allow**, then reload. If the setting is locked by policy, use `az storage ... --auth-mode login` or
Storage Explorer over the VPN. Also check that the browser resolves the account to its private IP
(`edge://net-internals/#dns`); Secure DNS or a cached public answer bypasses the VPN's resolver. The
private UI isn't affected: its page and its API calls come from the same private address.

### Firewall egress (private mode)

The Function routes all outbound traffic into the VNet (`vnetRouteAllEnabled`), so a landing zone that force-tunnels through a hub firewall must allow the endpoints below. Storage stays on its private endpoints, and any service moved behind a private endpoint drops off the list.

| Service | Endpoint |
|---|---|
| Foundry (Content Understanding, models, agents) | `*.services.ai.azure.com`, `*.cognitiveservices.azure.com`, `*.openai.azure.com` |
| Service Bus | `*.servicebus.windows.net` (AMQP 5671/5672, HTTPS 443) |
| AI Search | `*.search.windows.net` |
| Monitoring | `*.applicationinsights.azure.com`, `*.monitor.azure.com`, `*.ods.opinsights.azure.com` |
| Container Registry | `*.azurecr.io` |
| Microsoft Entra ID (managed identity tokens) | `login.microsoftonline.com` |

Confirm against the landing-zone firewall policy: service tags such as `CognitiveServicesManagement`, `ServiceBus` and `AzureMonitor` may already cover parts of it.

## Deploy

```powershell
./scripts/Deploy-Infrastructure.ps1 -Environment <env> -Location westus3 -Private -PrivateUi -WhatIf   # preview
./deploy.ps1 -Subscription <subscription-id> -Location westus3 -Environment <env> -Private -PrivateUi  # all phases
```

`deploy.ps1` phases (each a standalone `scripts/Deploy-*.ps1`): `infra` (`Deploy-Infrastructure`) → `contentunderstanding` (`Deploy-ContentUnderstanding`: model defaults + `ach.remittance.v1` analyzer, GA API `2025-11-01`, definition in [schemas/ach.remittance-analyzer-v1.json](../schemas/ach.remittance-analyzer-v1.json)) → `dataseed` (`Deploy-DataSeed`: `agents/corpus` into the `ach-policy` index) → `agents` (`Deploy-FoundryAgents`: new versions of `ach-extraction-expert` and `ach-assurance-expert`) → `function` (`Deploy-FunctionApp`) → `apps` (`Deploy-Applications`: `az acr build` api/ui, `apps.bicep`, demo UI URL).
