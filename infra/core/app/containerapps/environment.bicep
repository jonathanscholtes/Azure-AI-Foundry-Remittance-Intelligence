// Container Apps managed environment wired to the existing Log Analytics workspace.
// In private mode it VNet-integrates into the delegated landing-zone ACA subnet for
// outbound access to the private storage data plane. Ingress is public by default (the UI
// is browser-reachable); with publicNetworkAccess 'Disabled' and a private endpoint
// (main.bicep, privateUi) the UI is reachable only from the VNet - e.g. over the landing-zone
// VPN - without recreating the environment (workload-profiles environments support this).

@description('Name of the Container Apps environment.')
param environmentName string

@description('Azure region.')
param location string = resourceGroup().location

@description('Name of the existing Log Analytics workspace.')
param logAnalyticsWorkspaceName string

@description('When true, VNet-integrate the environment into the delegated ACA subnet.')
param privateNetworking bool = false

@description('Delegated landing-zone ACA subnet resource id (private mode).')
param appsSubnetResourceId string = ''

@description('Tags to apply.')
param tags object = {}

@description('Public network access to the environment\'s ingress. Disabled = reachable only through its private endpoint.')
@allowed(['Enabled', 'Disabled'])
param publicNetworkAccess string = 'Enabled'

var usePrivate = privateNetworking && !empty(appsSubnetResourceId)

resource logAnalytics 'Microsoft.OperationalInsights/workspaces@2023-09-01' existing = {
  name: logAnalyticsWorkspaceName
}

// 2025-07-01: the first GA version that accepts publicNetworkAccess (2025-01-01 rejects it; checked
// against the environment). The environment is a workload-profiles environment (Consumption profile),
// stated explicitly so the newer API keeps it as is.
resource environment 'Microsoft.App/managedEnvironments@2025-07-01' = {
  name: environmentName
  location: location
  tags: tags
  properties: union({
    publicNetworkAccess: publicNetworkAccess
    workloadProfiles: [
      {
        name: 'Consumption'
        workloadProfileType: 'Consumption'
      }
    ]
    appLogsConfiguration: {
      destination: 'log-analytics'
      logAnalyticsConfiguration: {
        customerId: logAnalytics.properties.customerId
        sharedKey: logAnalytics.listKeys().primarySharedKey
      }
    }
  }, usePrivate ? {
    vnetConfiguration: {
      infrastructureSubnetId: appsSubnetResourceId
    }
  } : {})
}

output environmentId string = environment.id
output environmentResourceName string = environment.name
output environmentName string = environment.name
output defaultDomain string = environment.properties.defaultDomain
