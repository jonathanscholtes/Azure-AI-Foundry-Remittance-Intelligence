param planName string
param functionAppName string
param location string = resourceGroup().location
param pythonVersion string = '3.11'

@description('App Service plan SKU for the Function App (Dedicated plan). Bump to S1/P1v3 for heavier scale or private (VNet) workloads.')
param appServicePlanSku string = 'B1'

param managedIdentityId string
param managedIdentityClientId string
param storageBlobEndpoint string
param storageQueueEndpoint string
param storageTableEndpoint string
param ledgerTableName string = 'remittances'
param eventsTableName string = 'remittanceevents'
param serviceBusFqdn string
param intakeQueueName string = 'remittance-intake'

@description('Max concurrent Durable activities per instance (throttles Content Understanding / agent calls).')
param maxConcurrentActivities int = 8

param appInsightsConnectionString string
param contentUnderstandingEndpoint string
param foundryProjectEndpoint string = ''
param extractionAgentName string = ''
param resolutionAgentName string = ''
param intakeDecisionDeployment string = ''
param assuranceAgentName string = ''
@description('Comma-separated names of the receiving org (payee) used to flag/resolve payer-vs-payee extraction. Supplied at deploy time so the value is not committed to the repo.')
param receivingOrgAliases string = ''
@description('Organization-specific PRO rules; empty means neutral rules (6-12 digits, no suffixes). Supplied at deploy time.')
param proMinDigits string = ''
param proMaxDigits string = ''
param proStripSuffixes string = ''
param proSuffixExamples string = ''
param functionSubnetResourceId string = ''
param tags object = {}

var vnetIntegrated = !empty(functionSubnetResourceId)

// Dedicated (App Service) plan rather than Flex Consumption / Elastic Premium. The storage
// account is keyless (allowSharedKeyAccess = false), and the Consumption / Elastic Premium plans
// require a WEBSITE_CONTENTAZUREFILECONNECTIONSTRING content share that only works with a storage
// account key. A Dedicated plan runs from its own filesystem (no content share), so it keeps the
// identity-only model while supporting the Service Bus trigger, Durable Functions, Always On,
// and regional VNet integration.
resource plan 'Microsoft.Web/serverfarms@2023-12-01' = {
  name: planName
  location: location
  tags: tags
  kind: 'linux'
  sku: {
    name: appServicePlanSku
  }
  properties: {
    reserved: true
  }
}

resource functionApp 'Microsoft.Web/sites@2023-12-01' = {
  name: functionAppName
  location: location
  tags: tags
  kind: 'functionapp,linux'
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: {
      '${managedIdentityId}': {}
    }
  }
  properties: {
    serverFarmId: plan.id
    httpsOnly: true
    virtualNetworkSubnetId: vnetIntegrated ? functionSubnetResourceId : null
    vnetRouteAllEnabled: vnetIntegrated
    siteConfig: {
      linuxFxVersion: 'Python|${pythonVersion}'
      // Keep the Functions host resident so the Service Bus listener and Durable dispatchers stay warm.
      alwaysOn: true
      minTlsVersion: '1.2'
      ftpsState: 'Disabled'
      appSettings: [
        // Identity-based AzureWebJobsStorage (no account key): the Functions host reaches its
        // blob/queue/table system containers with the user-assigned managed identity.
        {
          name: 'AzureWebJobsStorage__blobServiceUri'
          value: storageBlobEndpoint
        }
        {
          name: 'AzureWebJobsStorage__queueServiceUri'
          value: storageQueueEndpoint
        }
        {
          name: 'AzureWebJobsStorage__tableServiceUri'
          value: storageTableEndpoint
        }
        {
          name: 'AzureWebJobsStorage__credential'
          value: 'managedidentity'
        }
        {
          name: 'AzureWebJobsStorage__clientId'
          value: managedIdentityClientId
        }
        {
          name: 'FUNCTIONS_EXTENSION_VERSION'
          value: '~4'
        }
        {
          name: 'FUNCTIONS_WORKER_RUNTIME'
          value: 'python'
        }
        {
          name: 'STORAGE_BLOB_ENDPOINT'
          value: storageBlobEndpoint
        }
        {
          name: 'STORAGE_TABLE_ENDPOINT'
          value: storageTableEndpoint
        }
        {
          name: 'LEDGER_TABLE_NAME'
          value: ledgerTableName
        }
        {
          name: 'EVENTS_TABLE_NAME'
          value: eventsTableName
        }
        {
          name: 'AZURE_CLIENT_ID'
          value: managedIdentityClientId
        }
        // Identity-based Service Bus connection for the intake-queue trigger (no SAS keys).
        {
          name: 'ServiceBusConnection__fullyQualifiedNamespace'
          value: serviceBusFqdn
        }
        {
          name: 'ServiceBusConnection__credential'
          value: 'managedidentity'
        }
        {
          name: 'ServiceBusConnection__clientId'
          value: managedIdentityClientId
        }
        {
          name: 'INTAKE_QUEUE_NAME'
          value: intakeQueueName
        }
        // Durable task hub lives in AzureWebJobsStorage (identity-based) - no extra storage account.
        {
          name: 'DURABLE_TASK_HUB'
          value: 'achremittance'
        }
        // Overrides host.json durableTask.maxConcurrentActivityFunctions without a redeploy of code.
        {
          name: 'AzureFunctionsJobHost__extensions__durableTask__maxConcurrentActivityFunctions'
          value: string(maxConcurrentActivities)
        }
        {
          name: 'PYTHON_THREADPOOL_THREAD_COUNT'
          value: string(maxConcurrentActivities + 4)
        }
        {
          name: 'FOUNDRY_PROJECT_ENDPOINT'
          value: foundryProjectEndpoint
        }
        // Foundry agents referenced by name (latest version): ACH Extraction Expert and ACH Assurance Expert.
        {
          name: 'EXTRACTION_AGENT_NAME'
          value: extractionAgentName
        }
        {
          name: 'RESOLUTION_AGENT_NAME'
          value: resolutionAgentName
        }
        {
          name: 'ASSURANCE_AGENT_NAME'
          value: assuranceAgentName
        }
        // Microsoft-Decision-1 deployment for source / thread classification (same Foundry account as CU).
        {
          name: 'INTAKE_DECISION_DEPLOYMENT'
          value: intakeDecisionDeployment
        }
        {
          name: 'RECEIVING_ORG_ALIASES'
          value: receivingOrgAliases
        }
        {
          name: 'PRO_MIN_DIGITS'
          value: proMinDigits
        }
        {
          name: 'PRO_MAX_DIGITS'
          value: proMaxDigits
        }
        {
          name: 'PRO_STRIP_SUFFIXES'
          value: proStripSuffixes
        }
        {
          name: 'PRO_SUFFIX_EXAMPLES'
          value: proSuffixExamples
        }
        {
          name: 'MAX_MSG_SIZE_MB'
          value: '25'
        }
        {
          name: 'APPLICATIONINSIGHTS_CONNECTION_STRING'
          value: appInsightsConnectionString
        }
        {
          name: 'CONTENT_UNDERSTANDING_ENDPOINT'
          value: contentUnderstandingEndpoint
        }
        {
          name: 'CONTENT_UNDERSTANDING_ANALYZER_ID'
          value: 'ach.remittance.v1'
        }
        {
          name: 'RECONCILIATION_TOLERANCE'
          value: '0.01'
        }
      ]
    }
  }
}

output functionAppName string = functionApp.name
