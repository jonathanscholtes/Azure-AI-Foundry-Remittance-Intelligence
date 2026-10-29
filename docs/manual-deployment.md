# Manual Deployment Guide — ACH Remittance Intelligence

> Back to the [README](../README.md)

This guide describes every Azure resource in the solution, its settings, its role assignments, and the steps to create it by hand, without running `deploy.ps1`. Use it to review what the deployment creates, to adapt the Bicep to your own landing zone, or to deploy where scripts can't run.

> **Recommended:** use the automated deployment (`deploy.ps1`), which creates all of this from `infra/main.bicep` and `infra/apps.bicep` and is idempotent. See the **[Deployment Guide](deployment_steps.md)**.

The commands below are PowerShell with the Azure CLI. Names follow the same patterns as the Bicep ([Naming Conventions](#21-naming-conventions)), so a later `deploy.ps1` run against the same resource group recognizes them only if you use the same `<token>`; otherwise treat a manual deployment as its own environment.

---

## Required vs Optional Resources

| Resource | Bicep module | Required | Notes |
|---|---|---|---|
| Resource Group | *(deployment scope)* | ✅ Required | |
| User-Assigned Managed Identity | `core/security/managed-identity.bicep` | ✅ Required | Keyless access for the Function app and both container apps |
| Log Analytics + Application Insights | `core/monitor/main.bicep` | ✅ Required | Function telemetry, the run events, Container Apps logs |
| Microsoft Foundry account + model deployments | `core/ai/content-understanding.bicep` | ✅ Required | Content Understanding, GPT-5.5, GPT-5.6 Terra, Microsoft-Decision-1, embeddings |
| Microsoft Foundry project | `core/ai/foundry-project.bicep` | ✅ Required | Hosts the three Foundry agents |
| Azure AI Search + project connection | `core/ai/search.bicep`, `core/ai/foundry-search-connection.bicep` | ✅ Required | Policy index for the ACH Assurance Expert |
| Storage Account | `core/data/storage/main.bicep` | ✅ Required | Originals, artifacts, results, run ledger, live events, Durable task hub |
| Service Bus | `core/messaging/servicebus.bicep` | ✅ Required | `remittance-intake` queue (claim check) |
| App Service plan + Function app | `core/host/functionapp.bicep` | ✅ Required | Durable orchestration |
| Container Registry | `core/app/containerapps/registry.bicep` | ✅ Required | API and UI images |
| Container Apps environment + 2 apps | `core/app/containerapps/environment.bicep`, `apps.bicep` | ✅ Required | API (internal) and UI |
| Azure Monitor dashboard with Grafana | `core/monitor/dashboard.bicep` | ⚠️ Recommended | Outcomes, cost and operations reporting |
| Private endpoints, delegated subnets, private DNS | `core/network/private-endpoint.bicep` | ❌ Optional | Only for a landing-zone (private) deployment |

---

## Contents

1. [Prerequisites](#1-prerequisites)
2. [Set Shell Variables](#2-set-shell-variables)
3. [Resource Group](#3-resource-group)
4. [User-Assigned Managed Identity](#4-user-assigned-managed-identity)
5. [Log Analytics and Application Insights](#5-log-analytics-and-application-insights)
6. [Microsoft Foundry — Account](#6-microsoft-foundry--account)
7. [Microsoft Foundry — Model Deployments](#7-microsoft-foundry--model-deployments)
8. [Microsoft Foundry — Project](#8-microsoft-foundry--project)
9. [Azure AI Search and the Project Connection](#9-azure-ai-search-and-the-project-connection)
10. [Storage Account](#10-storage-account)
11. [Service Bus](#11-service-bus)
12. [Function App](#12-function-app)
13. [Container Registry](#13-container-registry)
14. [Container Apps Environment](#14-container-apps-environment)
15. [Content Understanding Analyzer](#15-content-understanding-analyzer)
16. [Policy Index](#16-policy-index)
17. [Foundry Agents](#17-foundry-agents)
18. [Publish the Function App](#18-publish-the-function-app)
19. [Build Images and Deploy the Container Apps](#19-build-images-and-deploy-the-container-apps)
20. [Dashboard](#20-dashboard)
21. [Naming Conventions](#21-naming-conventions)
22. [Role Assignments Summary](#22-role-assignments-summary)
23. [Private Deployment (Optional)](#23-private-deployment-optional)
24. [Verification Checklist](#24-verification-checklist)

---

## 1. Prerequisites

| Requirement | Detail |
|---|---|
| Azure subscription | **Owner**, or **Contributor** + **User Access Administrator** (the steps create role assignments) |
| Model quota | GPT-5.5 and GPT-5.6 Terra (Data Zone Standard), Microsoft-Decision-1, text-embedding-3-large; Content Understanding available in the region |
| Region | Tested in **West US 3** (`westus3`) |
| Tools | Azure CLI with Bicep, PowerShell, Python 3.11+, Azure Functions Core Tools v4. No Docker: images build in Azure Container Registry |

```powershell
az login
az account set --subscription "<subscription-id>"
```

---

## 2. Set Shell Variables

Set these once; every step uses them.

```powershell
$LOCATION = "westus3"
$PROJECT  = "ach-remit"
$ENV_NAME = "dev"
$BASE     = "$PROJECT-$ENV_NAME"                                       # ach-remit-dev
$TOKEN    = -join ((48..57) + (97..122) | Get-Random -Count 13 | ForEach-Object { [char]$_ })   # lowercase suffix for globally unique names
$RG       = "rg-$BASE"
$SUB_ID   = az account show --query id -o tsv
$ME       = az ad signed-in-user show --query id -o tsv              # deploying user (data-plane access)
$TAGS     = "workload=ach-remittance-intelligence", "environment=$ENV_NAME"

# Organization settings (see the Deployment Guide, Step 3). Leave empty for neutral rules.
$RECEIVING_ORG_ALIASES = ""
$PRO_MIN_DIGITS = ""; $PRO_MAX_DIGITS = ""; $PRO_STRIP_SUFFIXES = ""; $PRO_SUFFIX_EXAMPLES = ""
```

> The automated deployment derives the token with Bicep's `uniqueString(subscription, resource group, environment)`. Any lowercase alphanumeric suffix works for a manual deployment; keep it for the life of the environment.

---

## 3. Resource Group

**Portal path:** Resource groups → Create

| Setting | Value |
|---|---|
| Name | `rg-<project>-<env>` (e.g. `rg-ach-remit-dev`) |
| Region | West US 3 |

```powershell
az group create --name $RG --location $LOCATION --tags $TAGS
```

---

## 4. User-Assigned Managed Identity

**Portal path:** Managed Identities → Create

One identity is shared by the Function app and both container apps, so no keys or connection strings are stored anywhere.

| Setting | Value |
|---|---|
| Name | `id-<project>-<env>-<token>` |
| Region | West US 3 |

```powershell
$MI_NAME = "id-$BASE-$TOKEN"
az identity create --resource-group $RG --name $MI_NAME --location $LOCATION --tags $TAGS

$MI_ID           = az identity show -g $RG -n $MI_NAME --query id -o tsv
$MI_CLIENT_ID    = az identity show -g $RG -n $MI_NAME --query clientId -o tsv
$MI_PRINCIPAL_ID = az identity show -g $RG -n $MI_NAME --query principalId -o tsv
```

---

## 5. Log Analytics and Application Insights

**Portal path:** Log Analytics workspaces → Create, then Application Insights → Create

| Resource | Setting | Value |
|---|---|---|
| Log Analytics | Name | `log-<project>-<env>-<token>` |
| | Pricing tier | Pay-as-you-go (`PerGB2018`) |
| | Retention | 30 days |
| | Access control mode | Use resource or workspace permissions (resource-only) |
| Application Insights | Name | `appi-<project>-<env>-<token>` |
| | Application type | Web |
| | Workspace | the Log Analytics workspace above |

```powershell
$LAW_NAME  = "log-$BASE-$TOKEN"
$APPI_NAME = "appi-$BASE-$TOKEN"

az monitor log-analytics workspace create -g $RG -n $LAW_NAME -l $LOCATION --retention-time 30 --tags $TAGS
$LAW_ID          = az monitor log-analytics workspace show -g $RG -n $LAW_NAME --query id -o tsv
$LAW_CUSTOMER_ID = az monitor log-analytics workspace show -g $RG -n $LAW_NAME --query customerId -o tsv
$LAW_KEY         = az monitor log-analytics workspace get-shared-keys -g $RG -n $LAW_NAME --query primarySharedKey -o tsv

az monitor app-insights component create -g $RG --app $APPI_NAME -l $LOCATION --kind web `
  --application-type web --workspace $LAW_ID --tags $TAGS
$APPI_ID   = az monitor app-insights component show -g $RG --app $APPI_NAME --query id -o tsv
$APPI_CONN = az monitor app-insights component show -g $RG --app $APPI_NAME --query connectionString -o tsv
```

---

## 6. Microsoft Foundry — Account

**Portal path:** Microsoft Foundry → Create a resource (AI Services)

One Foundry (AI Services) account hosts Content Understanding, the model deployments and the project.

| Setting | Value |
|---|---|
| Name | `aif-<project>-<env>-<token>` |
| Kind / SKU | `AIServices` / `S0` |
| Custom subdomain | the name without hyphens |
| Project management | **Enabled** (`allowProjectManagement`) |
| Local (key) authentication | **Disabled** — Microsoft Entra only |
| Public network access | Enabled (Entra-only; see [Private Deployment](#23-private-deployment-optional)) |
| Diagnostic setting | `content-understanding-audit`: Audit logs + AllMetrics → the Log Analytics workspace |

```powershell
$FOUNDRY_NAME = "aif-$BASE-$TOKEN"
$FOUNDRY_SUBDOMAIN = $FOUNDRY_NAME.Replace("-", "")
$FOUNDRY_URL = "https://management.azure.com/subscriptions/$SUB_ID/resourceGroups/$RG/providers/Microsoft.CognitiveServices/accounts/$FOUNDRY_NAME`?api-version=2025-06-01"

@{
  location = $LOCATION; kind = "AIServices"; sku = @{ name = "S0" }
  tags = @{ workload = "ach-remittance-intelligence"; environment = $ENV_NAME }
  properties = @{ customSubDomainName = $FOUNDRY_SUBDOMAIN; allowProjectManagement = $true
                  disableLocalAuth = $true; publicNetworkAccess = "Enabled" }
} | ConvertTo-Json -Depth 5 | Set-Content foundry.json
az rest --method put --url $FOUNDRY_URL --body "@foundry.json"

$FOUNDRY_ID       = az cognitiveservices account show -g $RG -n $FOUNDRY_NAME --query id -o tsv
$FOUNDRY_ENDPOINT = az cognitiveservices account show -g $RG -n $FOUNDRY_NAME --query properties.endpoint -o tsv

az monitor diagnostic-settings create --name content-understanding-audit --resource $FOUNDRY_ID `
  --workspace $LAW_ID --logs '[{"category":"Audit","enabled":true}]' --metrics '[{"category":"AllMetrics","enabled":true}]'
```

> A deleted Foundry account stays soft-deleted and blocks reusing its name. Purge it first: `az cognitiveservices account list-deleted`, then `az cognitiveservices account purge`.

### Role Assignments — Foundry Account

| Principal | Role | Purpose |
|---|---|---|
| Managed identity | Cognitive Services Content Understanding Reader (`379c52cb-64de-498c-8b5b-c6170d6c49d4`) | Run analyses |
| Managed identity | Cognitive Services User | Model inference (Decision-1, agents) |
| Managed identity | Azure AI User | Invoke the Foundry agents |
| Deploying user | Cognitive Services Content Understanding Contributor (`59a2dba3-6303-4fd8-9a2e-8cbb4bdda972`) | Create the analyzer |
| Deploying user | Azure AI User, Azure AI Developer | Create the agents |

```powershell
az role assignment create --assignee-object-id $MI_PRINCIPAL_ID --assignee-principal-type ServicePrincipal --scope $FOUNDRY_ID --role "379c52cb-64de-498c-8b5b-c6170d6c49d4"
az role assignment create --assignee-object-id $MI_PRINCIPAL_ID --assignee-principal-type ServicePrincipal --scope $FOUNDRY_ID --role "Cognitive Services User"
az role assignment create --assignee-object-id $MI_PRINCIPAL_ID --assignee-principal-type ServicePrincipal --scope $FOUNDRY_ID --role "Azure AI User"
az role assignment create --assignee $ME --scope $FOUNDRY_ID --role "59a2dba3-6303-4fd8-9a2e-8cbb4bdda972"
az role assignment create --assignee $ME --scope $FOUNDRY_ID --role "Azure AI User"
az role assignment create --assignee $ME --scope $FOUNDRY_ID --role "Azure AI Developer"
```

---

## 7. Microsoft Foundry — Model Deployments

**Portal path:** Foundry portal → your account → Models + endpoints → Deploy model

| Deployment name | Model | Version | Deployment type | Capacity | Used by |
|---|---|---|---|---|---|
| `cu-gpt-55` | `gpt-5.5` (OpenAI) | `2026-04-24` | Data Zone Standard | 150K TPM | Content Understanding; ACH Extraction and Assurance Experts |
| `cu-text-embedding-3-large` | `text-embedding-3-large` (OpenAI) | `1` | Standard | 50K TPM | Content Understanding |
| `reasoning-gpt-56-terra` | `gpt-5.6-terra` (OpenAI) | `2026-07-09` | Data Zone Standard | 50K TPM | ACH Resolution Expert (failing or disputed runs only) |
| `intake-decision` | `Microsoft-Decision-1` (Microsoft) | `1` | Data Zone Standard | 60 (requests/min) | Source and thread classification |

```powershell
az cognitiveservices account deployment create -g $RG -n $FOUNDRY_NAME --deployment-name cu-gpt-55 `
  --model-format OpenAI --model-name gpt-5.5 --model-version 2026-04-24 --sku-name DataZoneStandard --sku-capacity 150
az cognitiveservices account deployment create -g $RG -n $FOUNDRY_NAME --deployment-name cu-text-embedding-3-large `
  --model-format OpenAI --model-name text-embedding-3-large --model-version 1 --sku-name Standard --sku-capacity 50
az cognitiveservices account deployment create -g $RG -n $FOUNDRY_NAME --deployment-name reasoning-gpt-56-terra `
  --model-format OpenAI --model-name gpt-5.6-terra --model-version 2026-07-09 --sku-name DataZoneStandard --sku-capacity 50
az cognitiveservices account deployment create -g $RG -n $FOUNDRY_NAME --deployment-name intake-decision `
  --model-format Microsoft --model-name Microsoft-Decision-1 --model-version 1 --sku-name DataZoneStandard --sku-capacity 60
```

> `cu-gpt-55` is shared by Content Understanding and two agents. Raise its capacity before a full-volume load test to avoid throttling.

---

## 8. Microsoft Foundry — Project

**Portal path:** Foundry portal → your account → Projects → New project

| Setting | Value |
|---|---|
| Name | `proj-<project>-<env>` |
| Display name | ACH remittance assurance |
| Identity | System-assigned (used by the agents' AI Search tool) |

```powershell
$PROJECT_NAME = "proj-$BASE"
@{
  location = $LOCATION; identity = @{ type = "SystemAssigned" }
  properties = @{ displayName = "ACH remittance assurance"
                  description = "Foundry assurance agents that assess remittance exceptions with cited policy evidence." }
} | ConvertTo-Json -Depth 5 | Set-Content project.json
az rest --method put --body "@project.json" `
  --url "https://management.azure.com$FOUNDRY_ID/projects/$PROJECT_NAME`?api-version=2025-06-01"

$PROJECT_PRINCIPAL_ID = az rest --method get --url "https://management.azure.com$FOUNDRY_ID/projects/$PROJECT_NAME`?api-version=2025-06-01" --query identity.principalId -o tsv
$PROJECT_ENDPOINT = "https://$FOUNDRY_SUBDOMAIN.services.ai.azure.com/api/projects/$PROJECT_NAME"
```

---

## 9. Azure AI Search and the Project Connection

**Portal path:** AI Search → Create

| Setting | Value |
|---|---|
| Name | `srch-<project>-<env>-<token>` |
| Pricing tier | Basic (1 partition, 1 replica) |
| Semantic ranker | Free |
| API access control | Role-based access control and API keys (apps use Entra) |
| Identity | System-assigned |

```powershell
$SEARCH_NAME = "srch-$BASE-$TOKEN"
az search service create -g $RG -n $SEARCH_NAME -l $LOCATION --sku basic --partition-count 1 --replica-count 1 `
  --semantic-search free --identity-type SystemAssigned --auth-options aadOrApiKey --aad-auth-failure-mode http401WithBearerChallenge
$SEARCH_ID = az search service show -g $RG -n $SEARCH_NAME --query id -o tsv
$SEARCH_ENDPOINT = "https://$SEARCH_NAME.search.windows.net"
```

### Role Assignments — AI Search

| Principal | Role | Purpose |
|---|---|---|
| Foundry project identity | Search Index Data Contributor | The agent's search tool queries the index |
| Foundry project identity | Search Service Contributor | The search tool reads the index definition (without it: 403) |
| Managed identity | Search Index Data Reader | Read the policy index |
| Deploying user | Search Index Data Contributor, Search Service Contributor | Create the index and load the corpus |

```powershell
az role assignment create --assignee-object-id $PROJECT_PRINCIPAL_ID --assignee-principal-type ServicePrincipal --scope $SEARCH_ID --role "Search Index Data Contributor"
az role assignment create --assignee-object-id $PROJECT_PRINCIPAL_ID --assignee-principal-type ServicePrincipal --scope $SEARCH_ID --role "Search Service Contributor"
az role assignment create --assignee-object-id $MI_PRINCIPAL_ID --assignee-principal-type ServicePrincipal --scope $SEARCH_ID --role "Search Index Data Reader"
az role assignment create --assignee $ME --scope $SEARCH_ID --role "Search Index Data Contributor"
az role assignment create --assignee $ME --scope $SEARCH_ID --role "Search Service Contributor"
```

### Project Connection to AI Search

**Portal path:** Foundry portal → project → Management center → Connected resources → New connection → Azure AI Search

| Setting | Value |
|---|---|
| Connection name | `ach-policy-search` |
| Authentication | Microsoft Entra ID |
| Shared to all project users | Yes |

```powershell
@{
  properties = @{ category = "CognitiveSearch"; target = $SEARCH_ENDPOINT; authType = "AAD"; isSharedToAll = $true
                  metadata = @{ ApiType = "Azure"; ResourceId = $SEARCH_ID; location = $LOCATION } }
} | ConvertTo-Json -Depth 5 | Set-Content connection.json
az rest --method put --body "@connection.json" `
  --url "https://management.azure.com$FOUNDRY_ID/projects/$PROJECT_NAME/connections/ach-policy-search`?api-version=2025-06-01"
```

---

## 10. Storage Account

**Portal path:** Storage accounts → Create

| Setting | Value |
|---|---|
| Name | `st<project><env><token>` — no hyphens, max 24 characters |
| Performance / redundancy | Standard / LRS |
| Kind / access tier | StorageV2 / Hot |
| Minimum TLS | 1.2 |
| Blob anonymous access | **Disabled** |
| Storage account key access | **Disabled** (Entra only) |

| Type | Names | Purpose |
|---|---|---|
| Blob containers | `incoming`, `attachments`, `candidates`, `bodies`, `analysis`, `audit`, `errors`, `manifests`, `results`, `deployments` | Originals, extracted parts, analysis, audit trail, CSV and final results |
| Tables | `remittances`, `remittanceevents` | Run ledger, live timeline |

The Durable Functions task hub (`achremittance`) uses the same account's queues and tables through the Function's identity.

```powershell
$ST_NAME = ("st$PROJECT$ENV_NAME$TOKEN").Replace("-", "").ToLower()
if ($ST_NAME.Length -gt 24) { $ST_NAME = $ST_NAME.Substring(0, 24) }
az storage account create -g $RG -n $ST_NAME -l $LOCATION --sku Standard_LRS --kind StorageV2 --access-tier Hot `
  --min-tls-version TLS1_2 --allow-blob-public-access false --allow-shared-key-access false --https-only true --tags $TAGS
$ST_ID = az storage account show -g $RG -n $ST_NAME --query id -o tsv

# The deploying user needs data access to create containers and tables (key access is disabled).
az role assignment create --assignee $ME --scope $ST_ID --role "Storage Blob Data Contributor"
az role assignment create --assignee $ME --scope $ST_ID --role "Storage Table Data Contributor"
# Wait a minute or two for the roles to apply, then:
foreach ($c in "incoming","attachments","candidates","bodies","analysis","audit","errors","manifests","results","deployments") {
  az storage container create --account-name $ST_NAME --name $c --auth-mode login --output none
}
foreach ($t in "remittances","remittanceevents") {
  az storage table create --account-name $ST_NAME --name $t --auth-mode login --output none
}
```

> With key access disabled, every data-plane CLI command needs `--auth-mode login`.

### Role Assignments — Managed Identity on Storage

| Role | Purpose |
|---|---|
| Storage Blob Data Owner | Blobs, and the Functions host's own containers |
| Storage Queue Data Contributor | Durable Functions queues |
| Storage Table Data Contributor | Run ledger, live events, Durable history |

```powershell
foreach ($role in "Storage Blob Data Owner","Storage Queue Data Contributor","Storage Table Data Contributor") {
  az role assignment create --assignee-object-id $MI_PRINCIPAL_ID --assignee-principal-type ServicePrincipal --scope $ST_ID --role $role
}
```

---

## 11. Service Bus

**Portal path:** Service Bus → Create namespace, then Queues → + Queue

| Setting | Value |
|---|---|
| Namespace | `sb-<project>-<env>-<token>`, **Standard** tier, TLS 1.2 |
| Local (SAS) authentication | **Disabled** |
| Queue | `remittance-intake` |
| Lock duration / max delivery count | 1 minute / 5 (then dead-letter) |
| Message time to live | 7 days, dead-letter on expiration |
| Duplicate detection | **Enabled**, 10-minute window (message ID = processing ID, the content hash) |
| Max queue size | 1 GB |

```powershell
$SB_NAME = "sb-$BASE-$TOKEN"
az servicebus namespace create -g $RG -n $SB_NAME -l $LOCATION --sku Standard --minimum-tls-version 1.2 --disable-local-auth true --tags $TAGS
az servicebus queue create -g $RG --namespace-name $SB_NAME -n remittance-intake `
  --lock-duration PT1M --max-delivery-count 5 --default-message-time-to-live P7D --enable-dead-lettering-on-message-expiration true `
  --enable-duplicate-detection true --duplicate-detection-history-time-window PT10M --max-size 1024
$SB_ID = az servicebus namespace show -g $RG -n $SB_NAME --query id -o tsv
$SB_FQDN = "$SB_NAME.servicebus.windows.net"

# Data Owner: send (API), receive (Function trigger) and queue metrics (Live Pipeline page, dead-letter replay).
az role assignment create --assignee-object-id $MI_PRINCIPAL_ID --assignee-principal-type ServicePrincipal --scope $SB_ID --role "Azure Service Bus Data Owner"
az role assignment create --assignee $ME --scope $SB_ID --role "Azure Service Bus Data Owner"
```

---

## 12. Function App

**Portal path:** Function App → Create → App Service (Dedicated) plan

The Function runs on a **Dedicated (App Service) plan**, not Consumption or Flex Consumption: storage key access is disabled, and those plans need a key-based content share.

| Setting | Value |
|---|---|
| Plan | `plan-<project>-<env>-<token>`, Linux, **B1** (S1 or P1v3 for load tests) |
| Function app | `func-<project>-<env>-<token>` |
| Runtime | Python 3.11, Functions v4 |
| Identity | User-assigned: the managed identity from step 4 |
| Always On | **On** (keeps the Service Bus listener and Durable dispatchers warm) |
| HTTPS only / minimum TLS / FTPS | On / 1.2 / Disabled |

```powershell
$PLAN_NAME = "plan-$BASE-$TOKEN"
$FUNC_NAME = "func-$BASE-$TOKEN"
az appservice plan create -g $RG -n $PLAN_NAME -l $LOCATION --is-linux --sku B1 --tags $TAGS
$PLAN_ID = az appservice plan show -g $RG -n $PLAN_NAME --query id -o tsv

@{
  location = $LOCATION; kind = "functionapp,linux"
  identity = @{ type = "UserAssigned"; userAssignedIdentities = @{ $MI_ID = @{} } }
  properties = @{ serverFarmId = $PLAN_ID; httpsOnly = $true
                  siteConfig = @{ linuxFxVersion = "Python|3.11"; alwaysOn = $true; minTlsVersion = "1.2"; ftpsState = "Disabled" } }
} | ConvertTo-Json -Depth 6 | Set-Content function.json
az rest --method put --body "@function.json" `
  --url "https://management.azure.com/subscriptions/$SUB_ID/resourceGroups/$RG/providers/Microsoft.Web/sites/$FUNC_NAME`?api-version=2023-12-01"
```

### App Settings

| Setting | Value |
|---|---|
| `AzureWebJobsStorage__blobServiceUri` / `__queueServiceUri` / `__tableServiceUri` | `https://<storage>.blob/queue/table.core.windows.net/` |
| `AzureWebJobsStorage__credential` / `__clientId` | `managedidentity` / managed identity client ID |
| `FUNCTIONS_EXTENSION_VERSION` / `FUNCTIONS_WORKER_RUNTIME` | `~4` / `python` |
| `STORAGE_BLOB_ENDPOINT`, `STORAGE_TABLE_ENDPOINT` | the storage blob and table endpoints |
| `LEDGER_TABLE_NAME` / `EVENTS_TABLE_NAME` | `remittances` / `remittanceevents` |
| `AZURE_CLIENT_ID` | managed identity client ID |
| `ServiceBusConnection__fullyQualifiedNamespace` | `<namespace>.servicebus.windows.net` |
| `ServiceBusConnection__credential` / `__clientId` | `managedidentity` / managed identity client ID |
| `INTAKE_QUEUE_NAME` | `remittance-intake` |
| `DURABLE_TASK_HUB` | `achremittance` |
| `AzureFunctionsJobHost__extensions__durableTask__maxConcurrentActivityFunctions` | `8` (the throttle protecting model quota) |
| `PYTHON_THREADPOOL_THREAD_COUNT` | `12` (max concurrent activities + 4) |
| `FOUNDRY_PROJECT_ENDPOINT` | the project endpoint from step 8 |
| `EXTRACTION_AGENT_NAME` / `RESOLUTION_AGENT_NAME` / `ASSURANCE_AGENT_NAME` | `ach-extraction-expert` / `ach-resolution-expert` / `ach-assurance-expert` |
| `INTAKE_DECISION_DEPLOYMENT` | `intake-decision` |
| `CONTENT_UNDERSTANDING_ENDPOINT` / `CONTENT_UNDERSTANDING_ANALYZER_ID` | the Foundry endpoint / `ach.remittance.v1` |
| `APPLICATIONINSIGHTS_CONNECTION_STRING` | from step 5 |
| `MAX_MSG_SIZE_MB` / `RECONCILIATION_TOLERANCE` | `25` / `0.01` |
| `RECEIVING_ORG_ALIASES` | the receiving organization's names (organization setting) |
| `PRO_MIN_DIGITS`, `PRO_MAX_DIGITS`, `PRO_STRIP_SUFFIXES`, `PRO_SUFFIX_EXAMPLES` | your PRO rules; empty = neutral (6–12 digits, no suffixes) |

```powershell
$BLOB  = "https://$ST_NAME.blob.core.windows.net/"
$QUEUE = "https://$ST_NAME.queue.core.windows.net/"
$TABLE = "https://$ST_NAME.table.core.windows.net/"
az functionapp config appsettings set -g $RG -n $FUNC_NAME --output none --settings `
  "AzureWebJobsStorage__blobServiceUri=$BLOB" "AzureWebJobsStorage__queueServiceUri=$QUEUE" "AzureWebJobsStorage__tableServiceUri=$TABLE" `
  "AzureWebJobsStorage__credential=managedidentity" "AzureWebJobsStorage__clientId=$MI_CLIENT_ID" `
  "FUNCTIONS_EXTENSION_VERSION=~4" "FUNCTIONS_WORKER_RUNTIME=python" `
  "STORAGE_BLOB_ENDPOINT=$BLOB" "STORAGE_TABLE_ENDPOINT=$TABLE" "LEDGER_TABLE_NAME=remittances" "EVENTS_TABLE_NAME=remittanceevents" `
  "AZURE_CLIENT_ID=$MI_CLIENT_ID" `
  "ServiceBusConnection__fullyQualifiedNamespace=$SB_FQDN" "ServiceBusConnection__credential=managedidentity" "ServiceBusConnection__clientId=$MI_CLIENT_ID" `
  "INTAKE_QUEUE_NAME=remittance-intake" "DURABLE_TASK_HUB=achremittance" `
  "AzureFunctionsJobHost__extensions__durableTask__maxConcurrentActivityFunctions=8" "PYTHON_THREADPOOL_THREAD_COUNT=12" `
  "FOUNDRY_PROJECT_ENDPOINT=$PROJECT_ENDPOINT" `
  "EXTRACTION_AGENT_NAME=ach-extraction-expert" "RESOLUTION_AGENT_NAME=ach-resolution-expert" "ASSURANCE_AGENT_NAME=ach-assurance-expert" `
  "INTAKE_DECISION_DEPLOYMENT=intake-decision" `
  "CONTENT_UNDERSTANDING_ENDPOINT=$FOUNDRY_ENDPOINT" "CONTENT_UNDERSTANDING_ANALYZER_ID=ach.remittance.v1" `
  "APPLICATIONINSIGHTS_CONNECTION_STRING=$APPI_CONN" "MAX_MSG_SIZE_MB=25" "RECONCILIATION_TOLERANCE=0.01" `
  "RECEIVING_ORG_ALIASES=$RECEIVING_ORG_ALIASES" `
  "PRO_MIN_DIGITS=$PRO_MIN_DIGITS" "PRO_MAX_DIGITS=$PRO_MAX_DIGITS" "PRO_STRIP_SUFFIXES=$PRO_STRIP_SUFFIXES" "PRO_SUFFIX_EXAMPLES=$PRO_SUFFIX_EXAMPLES"
```

> The code is published in [step 18](#18-publish-the-function-app), after the analyzer and agents exist.

---

## 13. Container Registry

**Portal path:** Container registries → Create

| Setting | Value |
|---|---|
| Name | `cr<project><env><token>` — no hyphens |
| SKU | Basic |
| Admin user | **Disabled** |
| Public network access | Enabled (ACR Tasks builds the images; access is RBAC-only) |

```powershell
$ACR_NAME = ("cr$PROJECT$ENV_NAME$TOKEN").Replace("-", "").ToLower()
az acr create -g $RG -n $ACR_NAME -l $LOCATION --sku Basic --admin-enabled false --tags $TAGS
$ACR_ID = az acr show -g $RG -n $ACR_NAME --query id -o tsv
$ACR_SERVER = az acr show -g $RG -n $ACR_NAME --query loginServer -o tsv
az role assignment create --assignee-object-id $MI_PRINCIPAL_ID --assignee-principal-type ServicePrincipal --scope $ACR_ID --role AcrPull
```

---

## 14. Container Apps Environment

**Portal path:** Container Apps Environments → Create

| Setting | Value |
|---|---|
| Name | `cae-<project>-<env>-<token>` |
| Plan | Workload profiles, with the **Consumption** profile |
| Logs | Log Analytics workspace from step 5 |
| Public network access | Enabled (Disabled for a VPN-only UI, see [step 23](#23-private-deployment-optional)) |

```powershell
$CAE_NAME = "cae-$BASE-$TOKEN"
az containerapp env create -g $RG -n $CAE_NAME -l $LOCATION --enable-workload-profiles `
  --logs-destination log-analytics --logs-workspace-id $LAW_CUSTOMER_ID --logs-workspace-key $LAW_KEY --tags $TAGS
$CAE_ID = az containerapp env show -g $RG -n $CAE_NAME --query id -o tsv
$CAE_DOMAIN = az containerapp env show -g $RG -n $CAE_NAME --query properties.defaultDomain -o tsv
```

---

## 15. Content Understanding Analyzer

Content Understanding needs its default model deployments set once per account, then the `ach.remittance.v1` analyzer is created from [schemas/ach.remittance-analyzer-v1.json](../schemas/ach.remittance-analyzer-v1.json) (GA API `2025-11-01`, Entra auth; needs the Content Understanding Contributor role from step 6).

The simplest way is the phase script, which reads everything from the variables you pass and retries through a new account's warm-up:

```powershell
./scripts/Deploy-ContentUnderstanding.ps1 -ResourceGroup $RG -Endpoint $FOUNDRY_ENDPOINT `
  -CompletionDeploymentName cu-gpt-55 -EmbeddingDeploymentName cu-text-embedding-3-large -AnalyzerId ach.remittance.v1
```

By hand, the two calls are:

```powershell
$token = az account get-access-token --resource https://cognitiveservices.azure.com/ --query accessToken -o tsv
$h = @{ Authorization = "Bearer $token" }
$base = $FOUNDRY_ENDPOINT.TrimEnd('/')

# 1) Default model deployments
$defaults = @{ modelDeployments = @{
  'gpt-5.5' = 'cu-gpt-55'; 'text-embedding-3-large' = 'cu-text-embedding-3-large'
  'prebuilt-analyzer-completion' = 'cu-gpt-55'; 'prebuilt-analyzer-completion-mini' = 'cu-gpt-55'
  'prebuilt-analyzer-embedding' = 'cu-text-embedding-3-large' } } | ConvertTo-Json -Depth 5
Invoke-RestMethod -Method Patch -Uri "$base/contentunderstanding/defaults?api-version=2025-11-01" -Headers $h -ContentType application/json -Body $defaults

# 2) The analyzer (create-only; an existing analyzer returns 409)
Invoke-WebRequest -Method Put -Uri "$base/contentunderstanding/analyzers/ach.remittance.v1?api-version=2025-11-01" -Headers $h `
  -ContentType application/json -Body (Get-Content schemas/ach.remittance-analyzer-v1.json -Raw) -UseBasicParsing
```

> On a new account the write path can return `500` for several minutes after the model deployments report *Succeeded*. Wait and retry.

---

## 16. Policy Index

The ACH Assurance Expert cites the deduction and cash-application policy from the `ach-policy` index. The loader creates the index (keyword + semantic ranking, no vectorizer) and loads `agents/corpus/*.json`. Replace the neutral corpus with your organization's approved policy before real use (keep it outside the repository).

```powershell
python -m pip install -r agents/requirements.txt
python scripts/load_policy_corpus.py --search-endpoint $SEARCH_ENDPOINT --index-name ach-policy
```

> A `403` right after step 9 means the Search roles haven't applied yet; retry after a few minutes.

---

## 17. Foundry Agents

Three prompt agents are created in the project. The Function references them by name and always uses the latest version.

| Agent | Model deployment | Tools | Role |
|---|---|---|---|
| `ach-extraction-expert` (ACH Extraction Expert) | `cu-gpt-55` | — | Maps payment-table columns |
| `ach-resolution-expert` (ACH Resolution Expert) | `reasoning-gpt-56-terra` | Code-backed function tools | Second attempt for runs that fail verification or have disputed details |
| `ach-assurance-expert` (ACH Assurance Expert) | `cu-gpt-55` | AI Search (`ach-policy-search` → `ach-policy`) | Cited policy findings for payer issues |

```powershell
$env:PRO_CREDIT_MEMO_SUFFIX = ""   # optional organization setting: the credit-memo PRO suffix used in the Assurance Expert's instructions
python agents/deploy.py --project-endpoint $PROJECT_ENDPOINT --model-deployment cu-gpt-55 `
  --reasoning-deployment reasoning-gpt-56-terra --search-connection-name ach-policy-search --index-name ach-policy
```

Each run creates a new immutable version and prunes old ones. Verify in the Foundry portal under **Agents**.

---

## 18. Publish the Function App

Publish from `apps/remittance-processor` with a remote build (no local packaging of dependencies):

```powershell
Push-Location apps/remittance-processor
func azure functionapp publish $FUNC_NAME --python --build remote
Pop-Location
```

> In a private deployment the publish needs network access to the storage private endpoint (run it from the VNet or its VPN).

---

## 19. Build Images and Deploy the Container Apps

Build both images in ACR (no local Docker):

```powershell
$TAG = Get-Date -Format "yyyyMMddHHmmss"
az acr build -g $RG --registry $ACR_NAME --image "ach-remit-api:$TAG" apps/api
az acr build -g $RG --registry $ACR_NAME --image "ach-remit-ui:$TAG" apps/ui
```

| App | Image | Ingress | Port | CPU / memory | Replicas |
|---|---|---|---|---|---|
| `ach-remit-api` | `ach-remit-api:<tag>` | **Internal** | 8000 | 0.5 / 1Gi | 1–3 |
| `ach-remit-ui` | `ach-remit-ui:<tag>` | External | 8080 | 0.5 / 1Gi | 1–3 |

Both use the managed identity for the image pull and for data access, with a TCP liveness probe on the target port.

| App | Environment variables |
|---|---|
| API | `AZURE_CLIENT_ID`, `STORAGE_BLOB_ENDPOINT`, `STORAGE_TABLE_ENDPOINT`, `LEDGER_TABLE_NAME=remittances`, `EVENTS_TABLE_NAME=remittanceevents`, `INCOMING_CONTAINER=incoming`, `SERVICE_BUS_FQDN`, `INTAKE_QUEUE_NAME=remittance-intake`, `ALLOWED_ORIGINS=*` |
| UI | `AZURE_CLIENT_ID`, `API_URL=https://<api internal FQDN>` (nginx proxies `/api` to it server-side) |

```powershell
az containerapp create -g $RG -n ach-remit-api --environment $CAE_ID --image "$ACR_SERVER/ach-remit-api:$TAG" `
  --registry-server $ACR_SERVER --registry-identity $MI_ID --user-assigned $MI_ID `
  --ingress internal --target-port 8000 --cpu 0.5 --memory 1Gi --min-replicas 1 --max-replicas 3 --tags $TAGS `
  --env-vars "AZURE_CLIENT_ID=$MI_CLIENT_ID" "STORAGE_BLOB_ENDPOINT=$BLOB" "STORAGE_TABLE_ENDPOINT=$TABLE" `
    "LEDGER_TABLE_NAME=remittances" "EVENTS_TABLE_NAME=remittanceevents" "INCOMING_CONTAINER=incoming" `
    "SERVICE_BUS_FQDN=$SB_FQDN" "INTAKE_QUEUE_NAME=remittance-intake" "ALLOWED_ORIGINS=*"
$API_FQDN = az containerapp show -g $RG -n ach-remit-api --query properties.configuration.ingress.fqdn -o tsv

az containerapp create -g $RG -n ach-remit-ui --environment $CAE_ID --image "$ACR_SERVER/ach-remit-ui:$TAG" `
  --registry-server $ACR_SERVER --registry-identity $MI_ID --user-assigned $MI_ID `
  --ingress external --target-port 8080 --cpu 0.5 --memory 1Gi --min-replicas 1 --max-replicas 3 --tags $TAGS `
  --env-vars "AZURE_CLIENT_ID=$MI_CLIENT_ID" "API_URL=https://$API_FQDN"
$UI_FQDN = az containerapp show -g $RG -n ach-remit-ui --query properties.configuration.ingress.fqdn -o tsv
"UI: https://$UI_FQDN"
```

> Alternatively, deploy [infra/apps.bicep](../infra/apps.bicep) with `az deployment group create`; it defines the same two apps.

---

## 20. Dashboard

The **ACH Remittance Processing** dashboard (Azure Monitor dashboards with Grafana) is generated by `observability/dashboard.py` into `observability/dashboards/ach-remittance.json`. Its placeholders are filled from your resources, so deploy it with its module rather than by hand:

```powershell
az deployment group create -g $RG --name dashboard --template-file infra/core/monitor/dashboard.bicep --parameters `
  name="dash-$BASE" location=$LOCATION appInsightsId=$APPI_ID foundryAccountName=$FOUNDRY_NAME `
  serviceBusNamespaceName=$SB_NAME uiUrl="https://ach-remit-ui.$CAE_DOMAIN"
```

Open it in the portal under **Monitor → Dashboards with Grafana**. Viewers need **Reader** on the dashboard and **Monitoring Reader** on the data it queries. See [observability/README.md](../observability/README.md).

---

## 21. Naming Conventions

`<abbreviation>-<project>-<env>-<token>`; names that don't allow hyphens drop them.

| Resource | Pattern | Example |
|---|---|---|
| Resource group | `rg-<project>-<env>` | `rg-ach-remit-dev` |
| Managed identity | `id-<project>-<env>-<token>` | `id-ach-remit-dev-a1b2c3d4e5f6g` |
| Log Analytics / App Insights | `log-…` / `appi-<project>-<env>-<token>` | `appi-ach-remit-dev-a1b2c3d4e5f6g` |
| Foundry account | `aif-<project>-<env>-<token>` | `aif-ach-remit-dev-a1b2c3d4e5f6g` |
| Foundry project | `proj-<project>-<env>` | `proj-ach-remit-dev` |
| AI Search | `srch-<project>-<env>-<token>` | `srch-ach-remit-dev-a1b2c3d4e5f6g` |
| Storage account | `st<project><env><token>` (max 24) | `stachremitdeva1b2c3d4e5f` |
| Service Bus | `sb-<project>-<env>-<token>` | `sb-ach-remit-dev-a1b2c3d4e5f6g` |
| App Service plan / Function app | `plan-…` / `func-<project>-<env>-<token>` | `func-ach-remit-dev-a1b2c3d4e5f6g` |
| Container registry | `cr<project><env><token>` | `crachremitdeva1b2c3d4e5f6g` |
| Container Apps environment | `cae-<project>-<env>-<token>` | `cae-ach-remit-dev-a1b2c3d4e5f6g` |
| Container apps | fixed names | `ach-remit-api`, `ach-remit-ui` |
| Dashboard | `dash-<project>-<env>` | `dash-ach-remit-dev` |
| Landing-zone subnets (private) | `snet-func-…` (/26), `snet-aca-…` (/23) | |

---

## 22. Role Assignments Summary

### Managed Identity (Function app and both container apps)

| Resource | Role | Purpose |
|---|---|---|
| Storage account | Storage Blob Data Owner | Blobs and the Functions host containers |
| Storage account | Storage Queue Data Contributor | Durable Functions queues |
| Storage account | Storage Table Data Contributor | Ledger, live events, Durable history |
| Service Bus | Azure Service Bus Data Owner | Send, receive, queue metrics |
| Foundry account | Cognitive Services Content Understanding Reader | Run analyses |
| Foundry account | Cognitive Services User | Model inference |
| Foundry account | Azure AI User | Invoke the agents |
| AI Search | Search Index Data Reader | Read the policy index |
| Container registry | AcrPull | Pull images |

### Foundry Project Identity

| Resource | Role | Purpose |
|---|---|---|
| AI Search | Search Index Data Contributor | The agent's search tool |
| AI Search | Search Service Contributor | Read the index definition |

### Deploying User

| Resource | Role | Purpose |
|---|---|---|
| Foundry account | Cognitive Services Content Understanding Contributor | Create the analyzer |
| Foundry account | Azure AI User, Azure AI Developer | Create the agents |
| AI Search | Search Index Data Contributor, Search Service Contributor | Create the index, load the corpus |
| Storage account | Storage Blob Data Contributor, Storage Table Data Contributor | Containers, tables, diagnostics |
| Service Bus | Azure Service Bus Data Owner | Running the Function or API locally against the queue, dead-letter replay |

---

## 23. Private Deployment (Optional)

`deploy.ps1 -Private [-PrivateUi]` places the solution in an existing landing-zone VNet. To do it by hand:

1. **Subnets** in the landing-zone VNet: a `/26` delegated to `Microsoft.Web/serverFarms` (Function VNet integration) and a `/23` delegated to `Microsoft.App/environments` (Container Apps). Private endpoints go in a non-delegated subnet (`snet-pe` by convention).
2. **Storage private endpoints** for `blob`, `queue` and `table`, registered in `privatelink.blob|queue|table.core.windows.net`. The zones must exist in the landing-zone resource group (the private-endpoint subnet's resource group) and be linked to the VNet. Then set the storage account's public network access to **Disabled** (default action Deny, bypass AzureServices).
3. **Function app**: regional VNet integration on the `/26` subnet with **Route All** enabled. If outbound traffic is force-tunneled, allow the endpoints in [infra/README.md](../infra/README.md#firewall-egress-private-mode).
4. **Container Apps environment**: create it on the `/23` subnet (`--infrastructure-subnet-resource-id`); this can't be added later.
5. **VPN-only UI (`-PrivateUi`)**: create `privatelink.<region>.azurecontainerapps.io` in the landing-zone resource group and link it to the VNet; add a private endpoint for the environment (group `managedEnvironments`); set the environment's public network access to **Disabled** (API version `2025-07-01` or later).
6. Publish the Function app and run the analyzer, index and agent steps from a machine on the VNet or its VPN.

> Foundry, AI Search and Service Bus (Standard) stay publicly reachable with Microsoft Entra authentication in this sample. For production, add private endpoints for Foundry and AI Search, and use Service Bus Premium with a private endpoint.

---

## 24. Verification Checklist

- [ ] The Function app is running and lists its functions:
  ```powershell
  az functionapp function list -g $RG -n $FUNC_NAME --query "[].name" -o tsv
  ```
- [ ] Both container apps are running:
  ```powershell
  az containerapp list -g $RG --query "[].{name:name, state:properties.runningStatus}" -o table
  ```
- [ ] The UI loads at `https://<ui-fqdn>` and the Live Pipeline card shows *live*
- [ ] The analyzer exists: `GET {endpoint}/contentunderstanding/analyzers/ach.remittance.v1?api-version=2025-11-01` returns status `ready`
- [ ] The three agents are listed in the Foundry portal under **Agents**
- [ ] The `ach-policy` index has documents (AI Search → Indexes)
- [ ] A smoke test reaches **Complete** (CSV in `results/`) or **Review Required**:
  ```powershell
  ./scripts/Test-Remittance.ps1 -MsgPath <path-to-message.msg> -ApiUrl https://<ui-fqdn>
  ```
- [ ] Run Details shows the timeline, payment lines and the **Cost and usage** card
- [ ] Application Insights has a `remittance_run_completed` trace for the run, and the dashboard shows it within a few minutes

---

## Clean-Up

```powershell
az group delete --name $RG --yes --no-wait
```

The Foundry account stays soft-deleted; purge it to release the name and quota:

```powershell
az cognitiveservices account purge -g $RG -n $FOUNDRY_NAME -l $LOCATION
```

For a private deployment, also delete the delegated subnets and the private DNS records (or the `azurecontainerapps.io` zone and its VNet link) in the landing-zone resource group.
