// Dedicated API Management for the ACH Review Expert (Copilot) - private deployments only.
//
// Developer tier, injected into the landing-zone VNet in EXTERNAL mode: the gateway keeps a public
// endpoint (Foundry Agent Service calls the MCP server from Microsoft's network), while calls to the
// backend leave through the VNet, so APIM reaches the private Container Apps environment over its
// private endpoint - the same path VPN users take to the UI. The ACH Review API (review-mcp.bicep) is
// the only API on it; every call needs a subscription key on the 'ACH Review' product.
//
// Deployed by scripts/Deploy-Apim.ps1 into the solution's resource group (the subnet goes into the
// landing-zone resource group). First deployment takes ~30-45 minutes. Developer has no SLA - use
// Premium for production.

@description('Project name (same as infra/main.bicep), used in resource names.')
param projectName string = 'ach-remit'

@description('Environment name (same as infra/main.bicep).')
param environmentName string = 'dev'

param location string = resourceGroup().location

@description('Landing-zone resource group holding the VNet.')
param landingZoneResourceGroup string

@description('Landing-zone VNet name.')
param vnetName string

@description('Name of APIM\'s dedicated subnet (snet-apim-...).')
param subnetName string

@description('Address prefix of APIM\'s subnet (/27 or larger), allocated by Deploy-Apim.ps1.')
param subnetPrefix string

@description('FQDN of the UI Container App; its /api path proxies to the internal API.')
param uiFqdn string

@description('Publisher email (APIM notifications).')
param publisherEmail string = 'apim-admin@example.com'

param publisherName string = 'ACH Remittance Processing'

param tags object = {}

// Same token as infra/main.bicep, so the name is stable per resource group + environment.
var resourceToken = toLower(uniqueString(subscription().id, resourceGroup().id, environmentName))
var apimName = 'apim-${projectName}-${environmentName}-${resourceToken}'

// Inbound and outbound rules VNet-injected API Management requires (stv2, external mode).
var nsgRules = [
  { name: 'AllowClientHttps', priority: 100, direction: 'Inbound', source: 'Internet', destination: 'VirtualNetwork', ports: [ '443' ] }
  { name: 'AllowApimManagement', priority: 110, direction: 'Inbound', source: 'ApiManagement', destination: 'VirtualNetwork', ports: [ '3443' ] }
  { name: 'AllowAzureLoadBalancer', priority: 120, direction: 'Inbound', source: 'AzureLoadBalancer', destination: 'VirtualNetwork', ports: [ '6390' ] }
  { name: 'AllowTrafficManager', priority: 130, direction: 'Inbound', source: 'AzureTrafficManager', destination: 'VirtualNetwork', ports: [ '443' ] }
  { name: 'AllowStorage', priority: 100, direction: 'Outbound', source: 'VirtualNetwork', destination: 'Storage', ports: [ '443' ] }
  { name: 'AllowSql', priority: 110, direction: 'Outbound', source: 'VirtualNetwork', destination: 'Sql', ports: [ '1433' ] }
  { name: 'AllowKeyVault', priority: 120, direction: 'Outbound', source: 'VirtualNetwork', destination: 'AzureKeyVault', ports: [ '443' ] }
  { name: 'AllowMonitor', priority: 130, direction: 'Outbound', source: 'VirtualNetwork', destination: 'AzureMonitor', ports: [ '1886', '443' ] }
]

resource nsg 'Microsoft.Network/networkSecurityGroups@2023-11-01' = {
  name: 'nsg-${apimName}'
  location: location
  tags: tags
  properties: {
    securityRules: [for rule in nsgRules: {
      name: rule.name
      properties: {
        priority: rule.priority
        direction: rule.direction
        access: 'Allow'
        protocol: 'Tcp'
        sourceAddressPrefix: rule.source
        sourcePortRange: '*'
        destinationAddressPrefix: rule.destination
        destinationPortRanges: rule.ports
      }
    }]
  }
}

module subnet '../core/network/apim-subnet.bicep' = {
  name: 'apim-subnet'
  scope: resourceGroup(landingZoneResourceGroup)
  params: {
    vnetName: vnetName
    subnetName: subnetName
    addressPrefix: subnetPrefix
    networkSecurityGroupId: nsg.id
  }
}

// VNet-injected APIM (stv2) needs its own Standard static public IP with a DNS label.
resource publicIp 'Microsoft.Network/publicIPAddresses@2023-11-01' = {
  name: 'pip-${apimName}'
  location: location
  tags: tags
  sku: {
    name: 'Standard'
  }
  properties: {
    publicIPAllocationMethod: 'Static'
    dnsSettings: {
      domainNameLabel: apimName
    }
  }
}

resource apim 'Microsoft.ApiManagement/service@2023-05-01-preview' = {
  name: apimName
  location: location
  tags: tags
  sku: {
    name: 'Developer'
    capacity: 1
  }
  identity: {
    type: 'SystemAssigned'
  }
  properties: {
    publisherEmail: publisherEmail
    publisherName: publisherName
    virtualNetworkType: 'External'
    virtualNetworkConfiguration: {
      subnetResourceId: subnet.outputs.subnetId
    }
    publicIpAddressId: publicIp.id
  }
}

module reviewApi 'review-mcp.bicep' = {
  name: 'ach-review-api'
  params: {
    apimName: apim.name
    uiFqdn: uiFqdn
  }
}

output apimName string = apim.name
output gatewayUrl string = apim.properties.gatewayUrl
output subscriptionName string = reviewApi.outputs.subscriptionName
output apiPath string = reviewApi.outputs.apiPath
