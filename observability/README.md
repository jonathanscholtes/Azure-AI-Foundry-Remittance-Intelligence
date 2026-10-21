# Observability

**ACH Remittance Processing** is an Azure Monitor dashboard with Grafana (Azure portal: Monitor >
Dashboards with Grafana). It reads the `remittance_run_completed` run event that every finished run
writes to Application Insights, plus the platform metrics of the Foundry account and Service Bus. No
Grafana instance, Prometheus or sidecar is involved.

| Section | What it shows |
|---|---|
| Outcomes and ROI | Messages processed; straight-through (the AI processed the remittance end to end and produced the payment file: on the first pass, or AI-resolved by the ACH Resolution Expert) vs needs review vs failed; payment lines and dollars processed; estimated AI cost and cost per message; median processing time; estimated time saved; messages per day by outcome; by payer |
| Cost and usage | Estimated AI cost per day by component; calls and usage; by message class, remittance date and calendar date (business time zone) |
| Runs | One row per email, with a link to Run Details |
| Operations | Processing time p50 / p95; time per step; review reasons; failures by stage; throttle retries; duplicates |
| Capacity | Model tokens and requests (incl. 429s) per deployment; intake queue active and dead-lettered messages |

Filters: time range, payer, message class, business time zone, and *manual minutes per message* (the
assumption behind the time-saved estimate). Costs are estimates from recorded usage and the configured
rates; Cost Management is the billing record.

## Files

- `dashboard.py` generates the dashboard; **edit it, not the JSON**, then regenerate:
  `python observability/dashboard.py`. Each panel's query is defined once on a shared base (`runs`: one
  row per run; `messages`: the latest run per email).
- `dashboards/ach-remittance.json` is the generated Grafana definition, with placeholders for the
  environment's resources.
- `infra/core/monitor/dashboard.bicep` deploys it (`Microsoft.Dashboard/dashboards` +
  `dashboardDefinitions`, API `2026-09-01`) and fills the placeholders. `main.bicep` includes it unless
  `deployDashboard=false`, so the infra phase deploys it.

## Testing the queries

`python observability/dashboard.py --emit-tests <folder>` also writes every log query with test values
(last 30 days, all payers and classes) so it can be run in Application Insights > Logs before deploying.

## Querying the events directly

The Recorder emits a `workflow_event` trace for every state transition and one `remittance_run_completed` event per finished run. In Application Insights > Logs:

```kusto
// One row per run: outcome, message class, payer, lines and estimated cost
traces
| where message startswith "remittance_run_completed "
| extend e = parse_json(substring(message, strlen("remittance_run_completed ")))
| summarize arg_max(timestamp, e) by processingId = tostring(e.processingId), runId = tostring(e.runId)
| project timestamp, processingId, outcome = tostring(e.outcome), messageClass = tostring(e.messageClass),
          payer = tostring(e.payerName), lines = toint(e.lineCount), estimatedCost = todouble(e.estimatedCost)
```

```kusto
// Every state transition (filter by processingId to follow one message)
AppTraces
| where Message startswith "workflow_event "
| extend data = parse_json(substring(Message, 15))
| project TimeGenerated,
          newState = tostring(data.newState),
          stage = tostring(data.stage),
          processingId = tostring(data.processingId),
          runId = tostring(data.runId),
          correlationId = tostring(data.correlationId),
          durationMs = toint(data.durationMs),
          details = data
| order by TimeGenerated desc
```

The `processingId` is also the Durable instance ID, so one message can be followed across retries and reprocessing. The same events are in the `remittanceevents` table, which drives the UI's live timeline, and in `audit/{processingId}/{runId}.json`.

## Access

Viewers need **Reader** on the dashboard and **Monitoring Reader** on the Application Insights
component, the Foundry account and the Service Bus namespace (or the resource group).
