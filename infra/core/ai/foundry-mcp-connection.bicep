// Project connection from the Foundry project to the APIM review MCP server: target = the MCP endpoint,
// credential = the APIM subscription key, sent as the Ocp-Apim-Subscription-Key header on every MCP call.
// agents/deploy.py reads the URL and id from this connection to bind the ACH Review Expert's MCP tool.
// Deployed by scripts/Deploy-Apim.ps1 (the key comes from APIM, so it is not part of main.bicep).

param accountName string
param projectName string
param connectionName string = 'ach-review-mcp'

@description('MCP endpoint, e.g. https://<apim>.azure-api.net/ach-review-mcp/mcp.')
param target string

@secure()
@description('APIM subscription key for the ach-review product.')
param subscriptionKey string

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
    category: 'CustomKeys'
    target: target
    authType: 'CustomKeys'
    isSharedToAll: true
    credentials: {
      keys: {
        'Ocp-Apim-Subscription-Key': subscriptionKey
      }
    }
    metadata: {
      type: 'mcp'
    }
  }
}

output connectionName string = connection.name
