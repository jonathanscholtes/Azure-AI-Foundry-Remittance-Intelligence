// Azure AI Search - backs the assurance (terms & deduction-policy) agent's retrieval over a small,
// neutral contract/policy corpus (scripts/load_policy_corpus.py). The Foundry project identity MUST
// hold Search Index Data Contributor or the agent's search tool returns 403.
//
// Entra (RBAC) auth only for apps; public network access stays enabled in this POC (same posture as
// the Foundry endpoint) - a private endpoint needs the landing-zone search DNS zone.

param searchServiceName string
param location string = resourceGroup().location

@allowed([ 'basic', 'standard' ])
param skuName string = 'basic'

@description('Principal id of the Foundry project system-assigned identity.')
param foundryProjectPrincipalId string

@description('Principal id of the shared app/Function managed identity.')
param managedIdentityPrincipalId string

@description('Optional Entra principal id of the deploying user (creates the index and loads the corpus).')
param devPrincipalId string = ''

param tags object = {}

resource search 'Microsoft.Search/searchServices@2024-06-01-preview' = {
  name: searchServiceName
  location: location
  tags: tags
  sku: {
    name: skuName
  }
  identity: {
    type: 'SystemAssigned'
  }
  properties: {
    authOptions: {
      aadOrApiKey: {
        aadAuthFailureMode: 'http401WithBearerChallenge'
      }
    }
    disableLocalAuth: false
    publicNetworkAccess: 'enabled'
    partitionCount: 1
    replicaCount: 1
    semanticSearch: 'free'
  }
}

var indexDataContributorRoleId = subscriptionResourceId('Microsoft.Authorization/roleDefinitions', '8ebe5a00-799e-43f5-93ac-243d3dce84a7')
var indexDataReaderRoleId = subscriptionResourceId('Microsoft.Authorization/roleDefinitions', '1407120a-92aa-4202-b7e9-c0e197c71c8f')
var searchServiceContributorRoleId = subscriptionResourceId('Microsoft.Authorization/roleDefinitions', '7ca78c08-252a-4471-8644-bb5ff32d4ba0')

resource projectIndexData 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(search.id, foundryProjectPrincipalId, indexDataContributorRoleId)
  scope: search
  properties: {
    roleDefinitionId: indexDataContributorRoleId
    principalId: foundryProjectPrincipalId
    principalType: 'ServicePrincipal'
  }
}

// Control-plane access: the agent's AI Search tool reads the index definition/schema, which the
// data-plane Index Data Contributor role does not cover. Without this the tool returns 403
// ("access denied ... managed identity access to the search service").
resource projectService 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(search.id, foundryProjectPrincipalId, searchServiceContributorRoleId)
  scope: search
  properties: {
    roleDefinitionId: searchServiceContributorRoleId
    principalId: foundryProjectPrincipalId
    principalType: 'ServicePrincipal'
  }
}

resource miIndexReader 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(search.id, managedIdentityPrincipalId, indexDataReaderRoleId)
  scope: search
  properties: {
    roleDefinitionId: indexDataReaderRoleId
    principalId: managedIdentityPrincipalId
    principalType: 'ServicePrincipal'
  }
}

resource devIndexData 'Microsoft.Authorization/roleAssignments@2022-04-01' = if (!empty(devPrincipalId)) {
  name: guid(search.id, devPrincipalId, indexDataContributorRoleId)
  scope: search
  properties: {
    roleDefinitionId: indexDataContributorRoleId
    principalId: devPrincipalId
  }
}

resource devService 'Microsoft.Authorization/roleAssignments@2022-04-01' = if (!empty(devPrincipalId)) {
  name: guid(search.id, devPrincipalId, searchServiceContributorRoleId)
  scope: search
  properties: {
    roleDefinitionId: searchServiceContributorRoleId
    principalId: devPrincipalId
  }
}

output searchServiceName string = search.name
output searchServiceId string = search.id
output searchEndpoint string = 'https://${search.name}.search.windows.net'
