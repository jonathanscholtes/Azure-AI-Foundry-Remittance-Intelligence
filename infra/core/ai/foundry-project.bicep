// Foundry project for the agentic assurance layer, created under the existing Foundry (AI Services)
// account that also hosts Content Understanding: a system-assigned project identity (used by the AI Search tool
// connection) plus the data-plane roles the agent runtime gates on.

@description('Name of the existing Foundry (AI Services) account.')
param accountName string

@description('Name of the Foundry project.')
param projectName string

param location string = resourceGroup().location

@description('Principal id of the shared app/Function managed identity (invokes agents at runtime).')
param managedIdentityPrincipalId string

@description('Optional Entra principal id of the deploying user (authors agents via agents/deploy.py).')
param devPrincipalId string = ''

param tags object = {}

resource account 'Microsoft.CognitiveServices/accounts@2025-06-01' existing = {
  name: accountName
}

resource project 'Microsoft.CognitiveServices/accounts/projects@2025-06-01' = {
  parent: account
  name: projectName
  location: location
  tags: tags
  identity: {
    type: 'SystemAssigned'
  }
  properties: {
    displayName: 'ACH remittance assurance'
    description: 'Foundry assurance agents that assess remittance exceptions with cited policy evidence.'
  }
}

// Azure AI User gates agent CRUD/invoke and connection reads (the agent data plane); Azure AI
// Developer alone is not enough to create agents. Cognitive Services User covers model inference.
var cognitiveServicesUserRoleId = subscriptionResourceId('Microsoft.Authorization/roleDefinitions', 'a97b65f3-24c7-4388-baec-2e87135dc908')
var azureAiDeveloperRoleId = subscriptionResourceId('Microsoft.Authorization/roleDefinitions', '64702f94-c441-49e6-a78b-ef80e0188fee')
var azureAiUserRoleId = subscriptionResourceId('Microsoft.Authorization/roleDefinitions', '53ca6127-db72-4b80-b1b0-d745d6d5456d')

resource miCsUser 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(account.id, managedIdentityPrincipalId, cognitiveServicesUserRoleId)
  scope: account
  properties: {
    roleDefinitionId: cognitiveServicesUserRoleId
    principalId: managedIdentityPrincipalId
    principalType: 'ServicePrincipal'
  }
}

resource miAiUser 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(account.id, managedIdentityPrincipalId, azureAiUserRoleId)
  scope: account
  properties: {
    roleDefinitionId: azureAiUserRoleId
    principalId: managedIdentityPrincipalId
    principalType: 'ServicePrincipal'
  }
}

resource devAiUser 'Microsoft.Authorization/roleAssignments@2022-04-01' = if (!empty(devPrincipalId)) {
  name: guid(account.id, devPrincipalId, azureAiUserRoleId)
  scope: account
  properties: {
    roleDefinitionId: azureAiUserRoleId
    principalId: devPrincipalId
  }
}

resource devAiDeveloper 'Microsoft.Authorization/roleAssignments@2022-04-01' = if (!empty(devPrincipalId)) {
  name: guid(account.id, devPrincipalId, azureAiDeveloperRoleId)
  scope: account
  properties: {
    roleDefinitionId: azureAiDeveloperRoleId
    principalId: devPrincipalId
  }
}

output projectName string = project.name
output projectPrincipalId string = project.identity.principalId
output projectEndpoint string = project.properties.endpoints['AI Foundry API']
