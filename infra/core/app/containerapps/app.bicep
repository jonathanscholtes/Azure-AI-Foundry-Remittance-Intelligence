// A single Container App (one apps/<component> image) using the shared managed
// identity for both ACR pull and runtime data-plane auth (Blob/Table via AZURE_CLIENT_ID).

@description('Name of the Container App.')
param appName string

@description('Container Apps environment resource id.')
param environmentId string

@description('Container image URI, e.g. myregistry.azurecr.io/ach-remit-api:latest.')
param image string

@description('Container registry login server (identity-based pull).')
param registryLoginServer string

@description('Resource id of the managed identity used for ACR pull + runtime auth.')
param managedIdentityId string

@description('Client id of the managed identity (exposed to the app as AZURE_CLIENT_ID).')
param managedIdentityClientId string

@description('Target container port (ingress).')
param targetPort int = 8080

@description('Expose public ingress (true for UI, false for internal API).')
param externalIngress bool = false

@description('Additional environment variables.')
param environmentVariables array = []

@description('CPU cores.')
param cpu string = '0.5'

@description('Memory.')
param memory string = '1Gi'

@description('Minimum replicas.')
param minReplicas int = 1

@description('Maximum replicas.')
param maxReplicas int = 3

@description('Tags to apply.')
param tags object = {}

resource containerApp 'Microsoft.App/containerApps@2024-03-01' = {
  name: appName
  location: resourceGroup().location
  tags: tags
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: {
      '${managedIdentityId}': {}
    }
  }
  properties: {
    managedEnvironmentId: environmentId
    configuration: {
      activeRevisionsMode: 'Single'
      registries: [
        {
          server: registryLoginServer
          identity: managedIdentityId
        }
      ]
      ingress: {
        external: externalIngress
        targetPort: targetPort
        transport: 'auto'
        allowInsecure: false
      }
    }
    template: {
      containers: [
        {
          image: image
          name: appName
          env: union([
            {
              name: 'AZURE_CLIENT_ID'
              value: managedIdentityClientId
            }
          ], environmentVariables)
          resources: {
            cpu: json(cpu)
            memory: memory
          }
          probes: [
            {
              type: 'Liveness'
              tcpSocket: {
                port: targetPort
              }
              initialDelaySeconds: 10
              periodSeconds: 10
            }
          ]
        }
      ]
      scale: {
        minReplicas: minReplicas
        maxReplicas: maxReplicas
      }
    }
  }
}

output appName string = containerApp.name
output appFqdn string = containerApp.properties.configuration.ingress.fqdn
