// Service Bus (Standard) - the remittance intake bus. Every ingestion stream (UI/API upload today,
// email next, the load generator) writes the raw message to Blob `incoming/` and then sends a small
// reference message here; a Service Bus-triggered Durable starter begins one orchestration per message.
// Messages carry identifiers only (claim-check pattern) - never MSG/attachment bytes.
//
// Local/SAS auth is disabled: producers and the Function authenticate with Entra (managed identity).
// Standard tier has no private endpoints, so the namespace stays publicly reachable with Entra-only
// auth in -Private mode (same posture as the Foundry endpoint). Move to Premium for private networking.

@description('Service Bus namespace name (globally unique, 6-50 chars).')
param namespaceName string

@description('Azure region.')
param location string = resourceGroup().location

@description('Intake queue name.')
param queueName string = 'remittance-intake'

@description('Principal id of the shared app/Function managed identity.')
param managedIdentityPrincipalId string

@description('Optional Entra principal id (deploying user) for the load generator and DLQ replay.')
param devPrincipalId string = ''

param tags object = {}

resource namespace 'Microsoft.ServiceBus/namespaces@2022-10-01-preview' = {
  name: namespaceName
  location: location
  tags: tags
  sku: {
    name: 'Standard'
    tier: 'Standard'
  }
  properties: {
    minimumTlsVersion: '1.2'
    disableLocalAuth: true
    publicNetworkAccess: 'Enabled'
  }
}

resource queue 'Microsoft.ServiceBus/namespaces/queues@2022-10-01-preview' = {
  parent: namespace
  name: queueName
  properties: {
    // The starter only schedules an orchestration, so a short lock is plenty; the message is
    // completed as soon as Durable has accepted the work.
    lockDuration: 'PT1M'
    maxDeliveryCount: 5
    deadLetteringOnMessageExpiration: true
    defaultMessageTimeToLive: 'P7D'
    // MessageId = processingId (content hash): an identical file submitted twice within the window
    // is dropped by the broker before it ever reaches the Function.
    requiresDuplicateDetection: true
    duplicateDetectionHistoryTimeWindow: 'PT10M'
    maxSizeInMegabytes: 1024
  }
}

// Azure Service Bus Data Owner: send (API), receive (Function trigger) and read runtime properties
// (queue depth / dead-letter count for the live pipeline view, which needs the Manage claim).
var dataOwnerRoleId = subscriptionResourceId('Microsoft.Authorization/roleDefinitions', '090c5cfd-751d-490a-894a-3ce6f1109419')

resource miDataOwner 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(namespace.id, managedIdentityPrincipalId, dataOwnerRoleId)
  scope: namespace
  properties: {
    roleDefinitionId: dataOwnerRoleId
    principalId: managedIdentityPrincipalId
    principalType: 'ServicePrincipal'
  }
}

resource devDataOwner 'Microsoft.Authorization/roleAssignments@2022-04-01' = if (!empty(devPrincipalId)) {
  name: guid(namespace.id, devPrincipalId, dataOwnerRoleId)
  scope: namespace
  properties: {
    roleDefinitionId: dataOwnerRoleId
    principalId: devPrincipalId
  }
}

output namespaceName string = namespace.name
output namespaceFqdn string = '${namespace.name}.servicebus.windows.net'
output queueName string = queue.name
