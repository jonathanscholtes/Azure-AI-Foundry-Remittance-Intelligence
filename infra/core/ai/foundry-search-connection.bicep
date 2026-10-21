// Project connection from the Foundry project to Azure AI Search (Entra / project identity auth).
// Separate module because the Search role assignments need the project's principal id first.

param accountName string
param projectName string
param connectionName string
param searchServiceId string
param searchEndpoint string
param location string = resourceGroup().location

resource account 'Microsoft.CognitiveServices/accounts@2025-06-01' existing = {
  name: accountName
}

resource project 'Microsoft.CognitiveServices/accounts/projects@2025-06-01' existing = {
  parent: account
  name: projectName
}

resource connection 'Microsoft.CognitiveServices/accounts/projects/connections@2025-06-01' = {
  parent: project
  name: connectionName
  properties: {
    category: 'CognitiveSearch'
    target: searchEndpoint
    authType: 'AAD'
    isSharedToAll: true
    metadata: {
      ApiType: 'Azure'
      ResourceId: searchServiceId
      location: location
    }
  }
}

output connectionName string = connection.name
