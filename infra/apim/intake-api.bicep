// ACH Intake API on the solution's dedicated API Management (apim.bicep): one operation that submits an
// email (.eml / .msg) for processing, for a mailbox flow (Logic App or Power Automate) outside the VNet.
// Like the review API, APIM calls the UI's /api proxy over the Container Apps private endpoint. Callers
// use a subscription key on the 'ach-intake' product, a separate key from the review agent's, so the
// intake key can only submit mail and the review key can't.

@description('Name of the API Management instance.')
param apimName string

@description('FQDN of the UI Container App (its /api path proxies to the internal API).')
param uiFqdn string

@description('APIM path of the ACH Intake API.')
param apiPath string = 'ach-intake'

resource apim 'Microsoft.ApiManagement/service@2023-05-01-preview' existing = {
  name: apimName
}

resource intakeApi 'Microsoft.ApiManagement/service/apis@2023-05-01-preview' = {
  parent: apim
  name: 'ach-intake-api'
  properties: {
    displayName: 'ACH Intake API'
    description: 'Submit a remittance email (.eml or .msg) for processing.'
    path: apiPath
    protocols: [ 'https' ]
    serviceUrl: 'https://${uiFqdn}/api'
    subscriptionRequired: true
  }
}

resource submitOp 'Microsoft.ApiManagement/service/apis/operations@2023-05-01-preview' = {
  parent: intakeApi
  name: 'submit_remittance'
  properties: {
    displayName: 'Submit remittance'
    method: 'POST'
    urlTemplate: '/remittances'
    description: 'multipart/form-data with one part named "file" (the whole email as .eml or .msg). Returns 202 with the processingId; the same message submitted again gets the same processingId and is not processed twice.'
    request: {
      queryParameters: [
        {
          name: 'stream'
          type: 'string'
          required: false
          description: 'Intake stream shown in the portal: email (mailbox flow) or upload'
        }
      ]
    }
  }
}

resource intakeProduct 'Microsoft.ApiManagement/service/products@2023-05-01-preview' = {
  parent: apim
  name: 'ach-intake'
  properties: {
    displayName: 'ACH Intake'
    description: 'Submit remittance emails for processing (mailbox flow).'
    subscriptionRequired: true
    approvalRequired: true
    state: 'published'
  }
}

resource intakeProductApi 'Microsoft.ApiManagement/service/products/apis@2023-05-01-preview' = {
  parent: intakeProduct
  name: intakeApi.name
}

// The key the mailbox flow holds; scoped to the product, so it opens nothing else on this APIM.
resource mailboxSubscription 'Microsoft.ApiManagement/service/subscriptions@2023-05-01-preview' = {
  parent: apim
  name: 'ach-intake-mailbox'
  properties: {
    displayName: 'ACH mailbox intake flow'
    scope: intakeProduct.id
    state: 'active'
  }
}

output submitUrl string = '${apim.properties.gatewayUrl}/${apiPath}/remittances?stream=email'
output subscriptionName string = mailboxSubscription.name
