param storageAccountName string
param location string = resourceGroup().location
param containers array
param tables array = []
param managedIdentityPrincipalId string
param devPrincipalId string = ''
param privateNetworking bool = false
param privateEndpointSubnetResourceId string = ''
param tags object = {}

resource storageAccount 'Microsoft.Storage/storageAccounts@2023-05-01' = {
  name: storageAccountName
  location: location
  tags: tags
  sku: {
    name: 'Standard_LRS'
  }
  kind: 'StorageV2'
  properties: {
    minimumTlsVersion: 'TLS1_2'
    allowBlobPublicAccess: false
    allowSharedKeyAccess: false
    supportsHttpsTrafficOnly: true
    accessTier: 'Hot'
    publicNetworkAccess: privateNetworking ? 'Disabled' : 'Enabled'
    networkAcls: {
      defaultAction: privateNetworking ? 'Deny' : 'Allow'
      bypass: 'AzureServices'
    }
  }
}

resource blobService 'Microsoft.Storage/storageAccounts/blobServices@2023-05-01' = {
  parent: storageAccount
  name: 'default'
}

resource blobContainers 'Microsoft.Storage/storageAccounts/blobServices/containers@2023-05-01' = [for containerName in containers: {
  parent: blobService
  name: containerName
  properties: {
    publicAccess: 'None'
  }
}]

resource tableService 'Microsoft.Storage/storageAccounts/tableServices@2023-05-01' = if (!empty(tables)) {
  parent: storageAccount
  name: 'default'
}

resource storageTables 'Microsoft.Storage/storageAccounts/tableServices/tables@2023-05-01' = [for tableName in tables: {
  parent: tableService
  name: tableName
}]

module blobPrivateEndpoint '../../network/private-endpoint.bicep' = if (privateNetworking && !empty(privateEndpointSubnetResourceId)) {
  name: 'blobPrivateEndpoint'
  params: {
    name: '${storageAccountName}-blob-pe'
    location: location
    privateEndpointSubnetResourceId: privateEndpointSubnetResourceId
    privateLinkServiceId: storageAccount.id
    groupId: 'blob'
    privateDnsZoneName: 'privatelink.blob.${environment().suffixes.storage}'
  }
}

module queuePrivateEndpoint '../../network/private-endpoint.bicep' = if (privateNetworking && !empty(privateEndpointSubnetResourceId)) {
  name: 'queuePrivateEndpoint'
  params: {
    name: '${storageAccountName}-queue-pe'
    location: location
    privateEndpointSubnetResourceId: privateEndpointSubnetResourceId
    privateLinkServiceId: storageAccount.id
    groupId: 'queue'
    privateDnsZoneName: 'privatelink.queue.${environment().suffixes.storage}'
  }
}

module tablePrivateEndpoint '../../network/private-endpoint.bicep' = if (privateNetworking && !empty(privateEndpointSubnetResourceId) && !empty(tables)) {
  name: 'tablePrivateEndpoint'
  params: {
    name: '${storageAccountName}-table-pe'
    location: location
    privateEndpointSubnetResourceId: privateEndpointSubnetResourceId
    privateLinkServiceId: storageAccount.id
    groupId: 'table'
    privateDnsZoneName: 'privatelink.table.${environment().suffixes.storage}'
  }
}

var blobOwnerRoleId = subscriptionResourceId('Microsoft.Authorization/roleDefinitions', 'b7e6dc6d-f1e8-4753-8033-0f276bb0955b')
resource blobOwnerRoleAssignment 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(storageAccount.id, managedIdentityPrincipalId, blobOwnerRoleId)
  scope: storageAccount
  properties: {
    roleDefinitionId: blobOwnerRoleId
    principalId: managedIdentityPrincipalId
    principalType: 'ServicePrincipal'
  }
}

var queueContributorRoleId = subscriptionResourceId('Microsoft.Authorization/roleDefinitions', '974c5e8b-45b9-4653-ba55-5f855dd0fb88')
resource queueRoleAssignment 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(storageAccount.id, managedIdentityPrincipalId, queueContributorRoleId)
  scope: storageAccount
  properties: {
    roleDefinitionId: queueContributorRoleId
    principalId: managedIdentityPrincipalId
    principalType: 'ServicePrincipal'
  }
}

var tableContributorRoleId = subscriptionResourceId('Microsoft.Authorization/roleDefinitions', '0a9a7e1f-b9d0-4cc4-a60d-0319b160aaa3')
resource tableRoleAssignment 'Microsoft.Authorization/roleAssignments@2022-04-01' = if (!empty(tables)) {
  name: guid(storageAccount.id, managedIdentityPrincipalId, tableContributorRoleId)
  scope: storageAccount
  properties: {
    roleDefinitionId: tableContributorRoleId
    principalId: managedIdentityPrincipalId
    principalType: 'ServicePrincipal'
  }
}

var blobContributorRoleId = subscriptionResourceId('Microsoft.Authorization/roleDefinitions', 'ba92f5b4-2d11-453d-a403-e96b0029c9fe')
resource devBlobRoleAssignment 'Microsoft.Authorization/roleAssignments@2022-04-01' = if (!empty(devPrincipalId)) {
  name: guid(storageAccount.id, devPrincipalId, blobContributorRoleId)
  scope: storageAccount
  properties: {
    roleDefinitionId: blobContributorRoleId
    principalId: devPrincipalId
  }
}

resource devTableRoleAssignment 'Microsoft.Authorization/roleAssignments@2022-04-01' = if (!empty(devPrincipalId) && !empty(tables)) {
  name: guid(storageAccount.id, devPrincipalId, tableContributorRoleId)
  scope: storageAccount
  properties: {
    roleDefinitionId: tableContributorRoleId
    principalId: devPrincipalId
  }
}

output storageAccountName string = storageAccount.name
output storageAccountId string = storageAccount.id
output blobEndpoint string = storageAccount.properties.primaryEndpoints.blob
output queueEndpoint string = storageAccount.properties.primaryEndpoints.queue
output tableEndpoint string = storageAccount.properties.primaryEndpoints.table
