param name string
param location string = resourceGroup().location
param functionPrincipalId string
param deploymentPrincipalId string = ''
param logAnalyticsWorkspaceId string
param tags object = {}

@description('TPM capacity (thousands of tokens/min) for the gpt-5.5 completion deployment, shared by Content Understanding and the assurance agent. Raise it to avoid 429s under concurrent extraction + agent load.')
param completionCapacity int = 150

@description('TPM capacity (thousands of tokens/min) for the embedding deployment used by the policy corpus.')
param embeddingCapacity int = 50

@description('Capacity for the Microsoft-Decision-1 deployment (source and thread classification): one unit = one request per minute (60 is the portal maximum for a new deployment). The Function makes at most one call per email, and only for multi-source emails.')
param decisionCapacity int = 60

@description('TPM capacity (thousands of tokens/min) for the GPT-5.6 Terra reasoning deployment used by the ACH Resolution Expert (hard cases only, ~10-20% of runs).')
param reasoningCapacity int = 50

var completionDeploymentName = 'cu-gpt-55'
// GPT-5.6 Terra: balanced, adjustable-depth reasoning for operational agents. Used by the ACH
// Resolution Expert, which only runs on runs that fail verification.
var reasoningDeploymentName = 'reasoning-gpt-56-terra'
var embeddingDeploymentName = 'cu-text-embedding-3-large'
// Microsoft-Decision-1: typed classification (choice / yes-no / score) with probabilities, no prose.
// Used after extraction to tell payment detail from payment proofs, statements and correspondence,
// and to classify the thread (one remittance / several payments / dispute) - only when rules can't.
var decisionDeploymentName = 'intake-decision'
// The AVM cognitive-services module (0.19.1) has no built-in name lookup for the Content
// Understanding roles, so reference them by role-definition GUID instead of friendly name.
// Reader:      Cognitive Services Content Understanding Reader
// Contributor: Cognitive Services Content Understanding Contributor
var contentUnderstandingReaderRoleId = subscriptionResourceId('Microsoft.Authorization/roleDefinitions', '379c52cb-64de-498c-8b5b-c6170d6c49d4')
var contentUnderstandingContributorRoleId = subscriptionResourceId('Microsoft.Authorization/roleDefinitions', '59a2dba3-6303-4fd8-9a2e-8cbb4bdda972')
var roleAssignments = concat(
  [
    {
      principalId: functionPrincipalId
      principalType: 'ServicePrincipal'
      roleDefinitionIdOrName: contentUnderstandingReaderRoleId
    }
  ],
  empty(deploymentPrincipalId) ? [] : [
    {
      principalId: deploymentPrincipalId
      roleDefinitionIdOrName: contentUnderstandingContributorRoleId
    }
  ]
)

module foundry 'br/public:avm/res/cognitive-services/account:0.19.1' = {
  name: 'foundry'
  params: {
    kind: 'AIServices'
    name: name
    location: location
    customSubDomainName: take(replace(name, '-', ''), 64)
    sku: 'S0'
    allowProjectManagement: true
    disableLocalAuth: true
    publicNetworkAccess: 'Enabled'
    deployments: [
      {
        name: completionDeploymentName
        model: {
          format: 'OpenAI'
          name: 'gpt-5.5'
          version: '2026-04-24'
        }
        sku: {
          name: 'DataZoneStandard'
          capacity: completionCapacity
        }
      }
      {
        name: embeddingDeploymentName
        model: {
          format: 'OpenAI'
          name: 'text-embedding-3-large'
          version: '1'
        }
        sku: {
          name: 'Standard'
          capacity: embeddingCapacity
        }
      }
      {
        name: reasoningDeploymentName
        model: {
          format: 'OpenAI'
          name: 'gpt-5.6-terra'
          version: '2026-07-09'
        }
        sku: {
          name: 'DataZoneStandard'
          capacity: reasoningCapacity
        }
      }
      {
        name: decisionDeploymentName
        model: {
          format: 'Microsoft'
          name: 'Microsoft-Decision-1'
          version: '1'
        }
        sku: {
          name: 'DataZoneStandard'
          capacity: decisionCapacity
        }
      }
    ]
    roleAssignments: roleAssignments
    diagnosticSettings: [
      {
        name: 'content-understanding-audit'
        workspaceResourceId: logAnalyticsWorkspaceId
        logCategoriesAndGroups: [
          {
            category: 'Audit'
          }
        ]
        metricCategories: [
          {
            category: 'AllMetrics'
          }
        ]
      }
    ]
    tags: tags
  }
}

output resourceName string = foundry.outputs.name
output endpoint string = foundry.outputs.endpoint
output completionDeploymentName string = completionDeploymentName
output embeddingDeploymentName string = embeddingDeploymentName
output decisionDeploymentName string = decisionDeploymentName
output reasoningDeploymentName string = reasoningDeploymentName
