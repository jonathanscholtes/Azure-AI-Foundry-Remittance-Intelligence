// ACH Review API on the solution's dedicated API Management (apim.bicep) - the REST surface that APIM's
// "Expose an API as an MCP server" export turns into the review MCP server for the ACH Review Expert
// (Copilot). The MCP export itself is a manual portal step (docs/review-agent.md).
//
// Each operation below becomes an MCP tool: its name is the tool name (= the route's operation_id in
// apps/api/app/main.py and the agent's ALLOWED_TOOLS) and its description is what the agent reads to
// decide when and how to call it. The API's ingress is limited to its Container Apps environment, so
// APIM (in the landing-zone VNet) calls the UI's /api proxy over the environment's private endpoint.
// Callers authenticate to APIM with a subscription key on the 'ach-review' product - the Foundry
// connection made by Deploy-Apim.ps1. approvalRequired keeps developer-portal users from self-issuing
// keys for it.

@description('Name of the API Management instance.')
param apimName string

@description('FQDN of the UI Container App (its /api path proxies to the internal API).')
param uiFqdn string

@description('APIM path of the ACH Review API.')
param apiPath string = 'ach-review'

resource apim 'Microsoft.ApiManagement/service@2023-05-01-preview' existing = {
  name: apimName
}

resource reviewApi 'Microsoft.ApiManagement/service/apis@2023-05-01-preview' = {
  parent: apim
  name: 'ach-review-api'
  properties: {
    displayName: 'ACH Review API'
    description: 'Remittance runs, manual-review cases, original email and document text, and review sign-off.'
    path: apiPath
    protocols: [ 'https' ]
    serviceUrl: 'https://${uiFqdn}/api/review'
    subscriptionRequired: true
  }
}

var processingId = {
  name: 'processing_id'
  type: 'string'
  required: true
  description: 'Processing id of the run, e.g. REM-0123456789ABCDEF0123.'
}

var reviewOperations = [
  {
    name: 'list_runs', display: 'List runs', method: 'GET', url: '/runs', templateParameters: []
    queryParameters: ['review', 'status', 'payer', 'q', 'from', 'to', 'limit']
    description: 'Find remittance runs. Call to list runs waiting for manual review (review=open), signed-off reviews (review=completed), runs still processing (status=InFlight) or failures (status=Failed). Params (all optional): review (open|completed), status (InFlight|Completed|ReviewRequired|Failed|DuplicateSkipped), payer (exact payer name), q (text in payer, file name, subject, processing id or payment reference), from / to (processed date YYYY-MM-DD), limit (1-200, default 25).'
  }
  {
    name: 'get_pipeline_status', display: 'Get pipeline status', method: 'GET', url: '/pipeline', templateParameters: []
    queryParameters: ['window']
    description: 'What is processing right now: runs in flight by stage, throughput, p50/p95 duration, queue latency and intake queue depth. Params: window (optional look-back in minutes, 15-1440, default 60).'
  }
  {
    name: 'get_run', display: 'Get run', method: 'GET', url: '/runs/{processing_id}', templateParameters: [processingId]
    queryParameters: []
    description: 'One run\'s review case: status, payer, date, payment reference, stated total vs extracted total and difference, review reasons, assurance findings, resolution explanation, payment lines, email header, sources (message-body, doc-NNN) and the review sign-off. Call before reviewing or answering about a specific run. Params: processing_id.'
  }
  {
    name: 'get_source_text', display: 'Get source text', method: 'GET', url: '/runs/{processing_id}/sources/{source_id}/text'
    templateParameters: [processingId, {
      name: 'source_id'
      type: 'string'
      required: true
      description: 'Source id from the run\'s sources list: message-body or doc-NNN.'
    }]
    queryParameters: ['offset']
    description: 'Read the original email (source_id message-body, with its header) or the text read from an attached document (doc-NNN), 8000 characters per page; call again with offset = nextOffset to keep reading. Call to check the extracted lines and findings against the evidence. Params: processing_id, source_id, offset (optional, default 0).'
  }
  {
    name: 'complete_review', display: 'Complete review', method: 'POST', url: '/runs/{processing_id}/complete', templateParameters: [processingId]
    queryParameters: []
    description: 'Mark a run\'s manual review as completed. Call ONLY after the analyst explicitly confirms. Params: processing_id; JSON body {reviewer (analyst name or email), outcome (approved = lines correct, releases the CSV | handled_manually = lines wrong, cash applied manually, no CSV | rejected = no cash applied, no CSV), notes (what was decided and why)}. Returns 409 if the run is not in review or was already completed.'
  }
]

var queryParameterDescriptions = {
  review: 'open = awaiting manual review; completed = signed off'
  status: 'InFlight, Completed, ReviewRequired, Failed or DuplicateSkipped'
  payer: 'Exact payer name'
  q: 'Text in payer, file name, subject, processing id or payment reference'
  from: 'Processed on or after (YYYY-MM-DD)'
  to: 'Processed on or before (YYYY-MM-DD)'
  limit: 'Maximum rows, 1-200 (default 25)'
  window: 'Look-back window in minutes, 15-1440 (default 60)'
  offset: 'Character offset; nextOffset from the previous page (default 0)'
}

resource reviewOps 'Microsoft.ApiManagement/service/apis/operations@2023-05-01-preview' = [for op in reviewOperations: {
  parent: reviewApi
  name: op.name
  properties: {
    displayName: op.display
    method: op.method
    urlTemplate: op.url
    description: op.description
    templateParameters: op.templateParameters
    request: {
      queryParameters: [for q in op.queryParameters: {
        name: q
        type: q == 'limit' || q == 'window' || q == 'offset' ? 'integer' : 'string'
        required: false
        description: queryParameterDescriptions[q]
      }]
    }
  }
}]

resource reviewProduct 'Microsoft.ApiManagement/service/products@2023-05-01-preview' = {
  parent: apim
  name: 'ach-review'
  properties: {
    displayName: 'ACH Review'
    description: 'ACH manual-review tools for the ACH Review Expert agent (review MCP server).'
    subscriptionRequired: true
    approvalRequired: true
    state: 'published'
  }
}

resource reviewProductApi 'Microsoft.ApiManagement/service/products/apis@2023-05-01-preview' = {
  parent: reviewProduct
  name: reviewApi.name
}

// The key the Foundry connection holds; scoped to the product, so it opens nothing else on this APIM.
resource agentSubscription 'Microsoft.ApiManagement/service/subscriptions@2023-05-01-preview' = {
  parent: apim
  name: 'ach-review-agent'
  properties: {
    displayName: 'ACH Review Expert (Foundry)'
    scope: reviewProduct.id
    state: 'active'
  }
}

output gatewayUrl string = apim.properties.gatewayUrl
output subscriptionName string = agentSubscription.name
output apiPath string = apiPath
