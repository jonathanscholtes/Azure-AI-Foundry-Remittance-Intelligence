// Container Apps for the demo: the FastAPI backend (internal ingress) and the React
// UI (public ingress). Deployed by deploy.ps1's 'apps' phase after the images are built
// into ACR. The UI reaches the API server-side via the API's internal FQDN, and the API
// reaches private storage and the Service Bus intake queue via the shared managed identity.

targetScope = 'resourceGroup'

@description('Container Apps environment resource id.')
param environmentId string

@description('Container registry login server.')
param registryLoginServer string

@description('Resource id of the shared managed identity.')
param managedIdentityResourceId string

@description('Client id of the shared managed identity.')
param managedIdentityClientId string

@description('Image tag to deploy (e.g. a timestamp).')
param imageTag string

param storageBlobEndpoint string
param storageTableEndpoint string
param ledgerTableName string = 'remittances'
param eventsTableName string = 'remittanceevents'
param incomingContainer string = 'incoming'

@description('Fully qualified Service Bus namespace (<name>.servicebus.windows.net).')
param serviceBusFqdn string
param intakeQueueName string = 'remittance-intake'

// Container app names are plain, descriptive names that match the image names (unique within the resource group / Container Apps environment).
var apiAppName = 'ach-remit-api'
var uiAppName = 'ach-remit-ui'

param tags object = {}

module api 'core/app/containerapps/app.bicep' = {
  name: 'app-api'
  params: {
    appName: apiAppName
    environmentId: environmentId
    image: '${registryLoginServer}/ach-remit-api:${imageTag}'
    registryLoginServer: registryLoginServer
    managedIdentityId: managedIdentityResourceId
    managedIdentityClientId: managedIdentityClientId
    targetPort: 8000
    externalIngress: false
    environmentVariables: [
      { name: 'STORAGE_BLOB_ENDPOINT', value: storageBlobEndpoint }
      { name: 'STORAGE_TABLE_ENDPOINT', value: storageTableEndpoint }
      { name: 'LEDGER_TABLE_NAME', value: ledgerTableName }
      { name: 'EVENTS_TABLE_NAME', value: eventsTableName }
      { name: 'INCOMING_CONTAINER', value: incomingContainer }
      { name: 'SERVICE_BUS_FQDN', value: serviceBusFqdn }
      { name: 'INTAKE_QUEUE_NAME', value: intakeQueueName }
      { name: 'ALLOWED_ORIGINS', value: '*' }
    ]
    tags: tags
  }
}

module ui 'core/app/containerapps/app.bicep' = {
  name: 'app-ui'
  params: {
    appName: uiAppName
    environmentId: environmentId
    image: '${registryLoginServer}/ach-remit-ui:${imageTag}'
    registryLoginServer: registryLoginServer
    managedIdentityId: managedIdentityResourceId
    managedIdentityClientId: managedIdentityClientId
    targetPort: 8080
    externalIngress: true
    environmentVariables: [
      // Internal FQDN of the API app; nginx proxies /api to it server-side.
      { name: 'API_URL', value: 'https://${api.outputs.appFqdn}' }
    ]
    tags: tags
  }
}

output apiFqdn string = api.outputs.appFqdn
output uiFqdn string = ui.outputs.appFqdn
