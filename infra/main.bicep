targetScope = 'resourceGroup'

@minLength(1)
@maxLength(16)
param projectName string = 'ach-remit'

@minLength(1)
@maxLength(8)
param environmentName string = 'dev'

param location string = resourceGroup().location
param pythonVersion string = '3.11'
param privateNetworking bool = false
param functionSubnetResourceId string = ''
param privateEndpointSubnetResourceId string = ''
param acaSubnetResourceId string = ''
param devPrincipalId string = ''

@description('App Service plan SKU for the Function (Dedicated). Use S1/P1v3 for load tests and email streams.')
param functionPlanSku string = 'B1'

@description('Max concurrent Durable activities per Function instance - the throttle that protects the Content Understanding / model quota.')
param maxConcurrentActivities int = 8

@description('Comma-separated names of the receiving org (payee), for payer-vs-payee resolution. Supplied at deploy time; kept out of the repo.')
param receivingOrgAliases string = ''

@description('The customer\'s PRO rules (Function app settings PRO_*). Supplied at deploy time from .env; empty means neutral rules (6-12 digits, no suffixes).')
param proMinDigits string = ''
param proMaxDigits string = ''
param proStripSuffixes string = ''
param proSuffixExamples string = ''

@description('Deploy the operations and cost dashboard (Azure Monitor dashboards with Grafana).')
param deployDashboard bool = true

@description('Private mode only: make the UI reachable only from the VNet (e.g. over the landing-zone VPN) - a private endpoint for the Container Apps environment and public access disabled. Needs the privatelink.<region>.azurecontainerapps.io zone in the landing zone (created by Deploy-Infrastructure.ps1 -PrivateUi).')
param privateUi bool = false

param tags object = {
  workload: 'ach-remittance-intelligence'
  environment: environmentName
}

var resourceToken = toLower(uniqueString(subscription().id, resourceGroup().id, environmentName))
var baseName = '${projectName}-${environmentName}'
var storageName = take(toLower(replace('st${projectName}${environmentName}${resourceToken}', '-', '')), 24)
// Resource abbreviations follow the Azure naming conventions (aif- Foundry, cr registry, ...).
var foundryResourceName = 'aif-${baseName}-${resourceToken}'
var registryName = take(toLower(replace('cr${projectName}${environmentName}${resourceToken}', '-', '')), 50)
var acaEnvironmentName = 'cae-${baseName}-${resourceToken}'
var serviceBusName = 'sb-${baseName}-${resourceToken}'
var intakeQueueName = 'remittance-intake'
var searchServiceName = take(toLower('srch-${baseName}-${resourceToken}'), 60)
var foundryProjectName = 'proj-${baseName}'
var searchConnectionName = 'ach-policy-search'
var policyIndexName = 'ach-policy'
// Foundry agents, named for their pipeline step (definitions in agents/, deployed by the 'agents' phase).
var extractionAgentName = 'ach-extraction-expert'
var resolutionAgentName = 'ach-resolution-expert'
var assuranceAgentName = 'ach-assurance-expert'
var privateNetworkingEffective = privateNetworking && !empty(functionSubnetResourceId) && !empty(privateEndpointSubnetResourceId)
var privateUiEffective = privateUi && privateNetworkingEffective && !empty(acaSubnetResourceId)
var deploymentContainerName = 'deployments'
var containers = [
  'incoming'
  'attachments'
  'candidates'
  'bodies'
  'analysis'
  'audit'
  'errors'
  'manifests'
  'results'
  deploymentContainerName
]
var ledgerTableName = 'remittances'
var eventsTableName = 'remittanceevents'
var tables = [
  ledgerTableName
  eventsTableName
]

module identity 'core/security/managed-identity.bicep' = {
  name: 'identity'
  params: {
    name: 'id-${baseName}-${resourceToken}'
    location: location
    tags: tags
  }
}

module monitor 'core/monitor/main.bicep' = {
  name: 'monitor'
  params: {
    logAnalyticsName: 'log-${baseName}-${resourceToken}'
    appInsightsName: 'appi-${baseName}-${resourceToken}'
    location: location
    tags: tags
  }
}

module contentUnderstanding 'core/ai/content-understanding.bicep' = {
  name: 'contentUnderstanding'
  params: {
    name: foundryResourceName
    location: location
    functionPrincipalId: identity.outputs.managedIdentityPrincipalId
    deploymentPrincipalId: devPrincipalId
    logAnalyticsWorkspaceId: monitor.outputs.logAnalyticsId
    tags: tags
  }
}

module storage 'core/data/storage/main.bicep' = {
  name: 'storage'
  params: {
    storageAccountName: storageName
    location: location
    containers: containers
    tables: tables
    managedIdentityPrincipalId: identity.outputs.managedIdentityPrincipalId
    devPrincipalId: devPrincipalId
    privateNetworking: privateNetworkingEffective
    privateEndpointSubnetResourceId: privateEndpointSubnetResourceId
    tags: tags
  }
}

module serviceBus 'core/messaging/servicebus.bicep' = {
  name: 'serviceBus'
  params: {
    namespaceName: serviceBusName
    location: location
    queueName: intakeQueueName
    managedIdentityPrincipalId: identity.outputs.managedIdentityPrincipalId
    devPrincipalId: devPrincipalId
    tags: tags
  }
}

module foundryProject 'core/ai/foundry-project.bicep' = {
  name: 'foundryProject'
  params: {
    accountName: contentUnderstanding.outputs.resourceName
    projectName: foundryProjectName
    location: location
    managedIdentityPrincipalId: identity.outputs.managedIdentityPrincipalId
    devPrincipalId: devPrincipalId
    tags: tags
  }
}

module search 'core/ai/search.bicep' = {
  name: 'search'
  params: {
    searchServiceName: searchServiceName
    location: location
    foundryProjectPrincipalId: foundryProject.outputs.projectPrincipalId
    managedIdentityPrincipalId: identity.outputs.managedIdentityPrincipalId
    devPrincipalId: devPrincipalId
    tags: tags
  }
}

module searchConnection 'core/ai/foundry-search-connection.bicep' = {
  name: 'searchConnection'
  params: {
    accountName: contentUnderstanding.outputs.resourceName
    projectName: foundryProject.outputs.projectName
    connectionName: searchConnectionName
    searchServiceId: search.outputs.searchServiceId
    searchEndpoint: search.outputs.searchEndpoint
    location: location
  }
}

module functionApp 'core/host/functionapp.bicep' = {
  name: 'functionApp'
  params: {
    planName: 'plan-${baseName}-${resourceToken}'
    functionAppName: 'func-${baseName}-${resourceToken}'
    location: location
    pythonVersion: pythonVersion
    appServicePlanSku: functionPlanSku
    maxConcurrentActivities: maxConcurrentActivities
    receivingOrgAliases: receivingOrgAliases
    proMinDigits: proMinDigits
    proMaxDigits: proMaxDigits
    proStripSuffixes: proStripSuffixes
    proSuffixExamples: proSuffixExamples
    managedIdentityId: identity.outputs.managedIdentityId
    managedIdentityClientId: identity.outputs.managedIdentityClientId
    storageBlobEndpoint: storage.outputs.blobEndpoint
    storageQueueEndpoint: storage.outputs.queueEndpoint
    storageTableEndpoint: storage.outputs.tableEndpoint
    ledgerTableName: ledgerTableName
    eventsTableName: eventsTableName
    serviceBusFqdn: serviceBus.outputs.namespaceFqdn
    intakeQueueName: serviceBus.outputs.queueName
    appInsightsConnectionString: monitor.outputs.appInsightsConnectionString
    contentUnderstandingEndpoint: contentUnderstanding.outputs.endpoint
    foundryProjectEndpoint: foundryProject.outputs.projectEndpoint
    extractionAgentName: extractionAgentName
    resolutionAgentName: resolutionAgentName
    assuranceAgentName: assuranceAgentName
    intakeDecisionDeployment: contentUnderstanding.outputs.decisionDeploymentName
    functionSubnetResourceId: privateNetworkingEffective ? functionSubnetResourceId : ''
    tags: tags
  }
}

// 'deploymentContainerName' is retained as a blob container for compatibility but is no longer
// wired into the Function App: the Dedicated plan deploys via Kudu zip-deploy, not a Flex
// deployment storage container.

module containerRegistry 'core/app/containerapps/registry.bicep' = {
  name: 'containerRegistry'
  params: {
    registryName: registryName
    location: location
    managedIdentityPrincipalId: identity.outputs.managedIdentityPrincipalId
    tags: tags
  }
}

module containerAppsEnvironment 'core/app/containerapps/environment.bicep' = {
  name: 'containerAppsEnvironment'
  params: {
    environmentName: acaEnvironmentName
    location: location
    logAnalyticsWorkspaceName: monitor.outputs.logAnalyticsName
    privateNetworking: privateNetworkingEffective
    appsSubnetResourceId: privateNetworkingEffective ? acaSubnetResourceId : ''
    publicNetworkAccess: privateUiEffective ? 'Disabled' : 'Enabled'
    tags: tags
  }
}

// Private UI: the environment's ingress (UI) gets a private IP in the landing-zone PE subnet; with
// public access disabled it is reachable only from the VNet / VPN. The API stays internal either way.
module containerAppsPrivateEndpoint 'core/network/private-endpoint.bicep' = if (privateUiEffective) {
  name: 'containerAppsPrivateEndpoint'
  params: {
    name: '${acaEnvironmentName}-pe'
    location: location
    privateEndpointSubnetResourceId: privateEndpointSubnetResourceId
    privateLinkServiceId: containerAppsEnvironment.outputs.environmentId
    groupId: 'managedEnvironments'
    privateDnsZoneName: 'privatelink.${location}.azurecontainerapps.io'
  }
}

// Operations and cost dashboard (Azure Monitor dashboards with Grafana), over the run events in
// Application Insights and the Foundry / Service Bus metrics. The UI app name matches apps.bicep.
module dashboard 'core/monitor/dashboard.bicep' = if (deployDashboard) {
  name: 'dashboard'
  params: {
    name: 'dash-${baseName}'
    location: location
    appInsightsId: monitor.outputs.appInsightsId
    foundryAccountName: contentUnderstanding.outputs.resourceName
    serviceBusNamespaceName: serviceBus.outputs.namespaceName
    uiUrl: 'https://ach-remit-ui.${containerAppsEnvironment.outputs.defaultDomain}'
    tags: tags
  }
}

output functionAppName string = functionApp.outputs.functionAppName
output storageAccountName string = storage.outputs.storageAccountName
output managedIdentityClientId string = identity.outputs.managedIdentityClientId
output managedIdentityResourceId string = identity.outputs.managedIdentityId
output storageBlobEndpoint string = storage.outputs.blobEndpoint
output storageTableEndpoint string = storage.outputs.tableEndpoint
output ledgerTableName string = ledgerTableName
output eventsTableName string = eventsTableName
output serviceBusNamespace string = serviceBus.outputs.namespaceName
output serviceBusFqdn string = serviceBus.outputs.namespaceFqdn
output intakeQueueName string = serviceBus.outputs.queueName
output foundryProjectEndpoint string = foundryProject.outputs.projectEndpoint
output searchServiceName string = search.outputs.searchServiceName
output searchEndpoint string = search.outputs.searchEndpoint
output searchConnectionName string = searchConnection.outputs.connectionName
output policyIndexName string = policyIndexName
output extractionAgentName string = extractionAgentName
output resolutionAgentName string = resolutionAgentName
output reasoningDeploymentName string = contentUnderstanding.outputs.reasoningDeploymentName
output assuranceAgentName string = assuranceAgentName
output containerRegistryName string = containerRegistry.outputs.registryName
output containerRegistryLoginServer string = containerRegistry.outputs.registryLoginServer
output containerAppsEnvironmentId string = containerAppsEnvironment.outputs.environmentId
output containerAppsEnvironmentDefaultDomain string = containerAppsEnvironment.outputs.defaultDomain
output privateNetworkingEffective bool = privateNetworkingEffective
output privateUiEffective bool = privateUiEffective
output logAnalyticsName string = monitor.outputs.logAnalyticsName
output appInsightsName string = monitor.outputs.appInsightsName
output dashboardName string = deployDashboard ? dashboard!.outputs.dashboardName : ''
output contentUnderstandingResourceName string = contentUnderstanding.outputs.resourceName
output contentUnderstandingEndpoint string = contentUnderstanding.outputs.endpoint
output contentUnderstandingCompletionDeploymentName string = contentUnderstanding.outputs.completionDeploymentName
output contentUnderstandingEmbeddingDeploymentName string = contentUnderstanding.outputs.embeddingDeploymentName
output contentUnderstandingAnalyzerId string = 'ach.remittance.v1'
output intakeDecisionDeployment string = contentUnderstanding.outputs.decisionDeploymentName
