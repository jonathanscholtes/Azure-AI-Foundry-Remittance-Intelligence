// API Management's dedicated subnet in the landing-zone VNet (deployed into the landing-zone resource
// group). Not delegated: classic VNet injection (Developer / Premium) needs an empty subnet with an NSG.

param vnetName string
param subnetName string

@description('Address prefix, e.g. 10.0.4.0/27 (allocated by scripts/Deploy-Apim.ps1).')
param addressPrefix string

param networkSecurityGroupId string

resource vnet 'Microsoft.Network/virtualNetworks@2023-11-01' existing = {
  name: vnetName
}

resource subnet 'Microsoft.Network/virtualNetworks/subnets@2023-11-01' = {
  parent: vnet
  name: subnetName
  properties: {
    addressPrefix: addressPrefix
    networkSecurityGroup: {
      id: networkSecurityGroupId
    }
  }
}

output subnetId string = subnet.id
