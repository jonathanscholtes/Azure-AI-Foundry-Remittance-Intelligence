// Azure Container Registry for the api/ui images. The shared managed identity is
// granted AcrPull so Container Apps pull without admin credentials. Public network
// access stays enabled so `az acr build` (ACR Tasks) can push images; access is
// gated by RBAC + no admin user. 

@description('Container registry name (5-50 alphanumerics).')
param registryName string

@description('Azure region.')
param location string = resourceGroup().location

@description('Principal id of the managed identity granted AcrPull.')
param managedIdentityPrincipalId string

@description('Tags to apply.')
param tags object = {}

resource registry 'Microsoft.ContainerRegistry/registries@2023-11-01-preview' = {
  name: registryName
  location: location
  tags: tags
  sku: {
    name: 'Basic'
  }
  properties: {
    adminUserEnabled: false
    publicNetworkAccess: 'Enabled'
  }
}

var acrPullRoleId = subscriptionResourceId('Microsoft.Authorization/roleDefinitions', '7f951dda-4ed3-4680-a7ca-43fe172d538d')
resource acrPullAssignment 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(registry.id, managedIdentityPrincipalId, acrPullRoleId)
  scope: registry
  properties: {
    roleDefinitionId: acrPullRoleId
    principalId: managedIdentityPrincipalId
    principalType: 'ServicePrincipal'
  }
}

output registryName string = registry.name
output registryLoginServer string = registry.properties.loginServer
