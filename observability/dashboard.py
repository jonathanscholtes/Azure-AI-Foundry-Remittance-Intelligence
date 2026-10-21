"""Generate the Azure Monitor dashboard with Grafana for ACH remittance processing.

    python observability/dashboard.py                 # writes observability/dashboards/ach-remittance.json
    python observability/dashboard.py --emit-tests D  # also writes each panel's KQL, with test values, to D/

The dashboard's own text (title, descriptions, notes) stays free of internal project references, so it
reads the same wherever it's shared.

The dashboard reads the ``remittance_run_completed`` run event (``shared/run_metrics.py``) from
Application Insights and the platform metrics of the Foundry account and Service Bus. Environment
values are placeholders that ``infra/core/monitor/dashboard.bicep`` fills at deploy time:
__APPI_ID__, __SUBSCRIPTION_ID__, __RESOURCE_GROUP__, __FOUNDRY_NAME__, __SERVICEBUS_NAME__,
__REGION__, __UI_URL__. Edit this file (not the JSON) and regenerate.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

OUT = Path(__file__).resolve().parent / "dashboards" / "ach-remittance.json"
DS = {"type": "grafana-azure-monitor-datasource", "uid": "${ds}"}
HANDLED = '("handled_by_rules", "handled_by_agents")'
# The filters' "All" value. A selected value arrives quoted by ${var:singlequote}; "All" arrives exactly
# as written here, so it is already a quoted KQL string and both compare against '__all'.
ALL_VALUE = "'__all'"
# Message classes: how the payment lines were read, which drives the cost of an email.
CLASS_LABELS = {
    "A": "A - Body table",
    "B": "B - Spreadsheet",
    "C": "C - Small document",
    "D": "D - Large document",
    "none": "none - No lines",
}
CLASS_NAME_KQL = "case(" + ", ".join(f'messageClass == "{value}", "{label}"' for value, label in CLASS_LABELS.items()) + ", messageClass)"
# Outcomes in business terms ("straight-through" = the AI processed the remittance end to end; the UI
# says "Needs review"). Describe what the AI did, not what people didn't do.
# Both straight-through outcomes use AI (Content Understanding, the ACH Extraction Expert): the split is
# whether the first pass verified, or a second pass by the ACH Resolution Expert was needed.
OUTCOME_LABELS = {
    "handled_by_rules": ("Straight-through, first pass", "green"),
    "handled_by_agents": ("Straight-through, AI-resolved", "blue"),
    "flagged_for_review": ("Needs review", "orange"),
    "failed": ("Failed", "red"),
    "duplicate": ("Duplicate", "gray"),
}
OUTCOME_NAME_KQL = "case(" + ", ".join(f'outcome == "{code}", "{label}"' for code, (label, _) in OUTCOME_LABELS.items()) + ", outcome)"
OUTCOME_COLORS = [{"matcher": {"id": "byName", "options": label},
                   "properties": [{"id": "color", "value": {"mode": "fixed", "fixedColor": color}}]}
                  for label, color in OUTCOME_LABELS.values()]

# Every log panel starts from the same two tables: `runs` (one row per run - each run was billed) and
# `messages` (the latest run per email - counts and outcomes). A retried Recorder call can log an event
# twice, so runs keep one per processingId + runId.
BASE = """let runs = traces
| where $__timeFilter(timestamp)
| where message startswith "remittance_run_completed "
| extend e = parse_json(substring(message, 25))
| summarize arg_max(timestamp, e) by processingId = tostring(e.processingId), runId = tostring(e.runId)
| extend payer = tostring(e.payerName), messageClass = tostring(e.messageClass), outcome = tostring(e.outcome),
         lines = toint(e.lineCount), paymentTotal = todouble(e.paymentTotal), cost = todouble(e.estimatedCost),
         durationMs = toint(e.durationMs), remittanceDate = tostring(e.remittanceDate),
         day = startofday(datetime_utc_to_local(timestamp, '$tz'))
| where ${payer:singlequote} == '__all' or payer == ${payer:singlequote}
| where ${class:singlequote} == '__all' or messageClass == ${class:singlequote};
let messages = runs | summarize arg_max(timestamp, *) by processingId | where outcome != "duplicate";
"""

TEST_VALUES = {
    "$__timeFilter(timestamp)": "timestamp > ago(30d)",
    "${payer:singlequote}": "'__all'",
    "${class:singlequote}": "'__all'",
    "$tz": "America/New_York",
    "$minutes": "10",
}

DISCLAIMER = (
    "**Straight-through:** the AI processed the remittance end to end - read the email and its "
    "attachments, extracted every payment line, verified each line against its source, reconciled to the "
    "payment and produced the payment file, ready for cash application. *First pass*: verified on the first "
    "extraction. *AI-resolved*: the ACH Resolution Expert reasoned across the sources to recover the lines or "
    "details, and its answer was verified before it was applied.\n\n"
    "**Time saved (estimate):** straight-through messages x the *Manual minutes per message* setting above, "
    "the business's estimate of the manual effort per remittance.\n\n"
    "**Costs (estimate):** from each run's recorded AI usage and the configured rates (today Azure list "
    "prices, before negotiated discounts, credits or taxes). Azure Cost Management is the billing record."
)


class Layout:
    """Places panels left to right on Grafana's 24-column grid."""

    def __init__(self):
        self.panels: list[dict] = []
        self.y = 0
        self.x = 0
        self.row_height = 0
        self.next_id = 1

    def _place(self, panel: dict, w: int, h: int) -> dict:
        if self.x + w > 24:
            self.y += self.row_height
            self.x, self.row_height = 0, 0
        panel.update({"id": self.next_id, "gridPos": {"x": self.x, "y": self.y, "w": w, "h": h}})
        self.next_id += 1
        self.x += w
        self.row_height = max(self.row_height, h)
        self.panels.append(panel)
        return panel

    def row(self, title: str) -> None:
        if self.x:
            self.y += self.row_height
        self.x, self.row_height = 0, 0
        self._place({"type": "row", "title": title, "collapsed": False, "panels": []}, 24, 1)
        self.x, self.row_height, self.y = 0, 0, self.y + 1

    def add(self, panel: dict, w: int, h: int) -> dict:
        return self._place(panel, w, h)


def logs_target(query: str, fmt: str = "table", ref: str = "A") -> dict:
    return {
        "refId": ref,
        "datasource": DS,
        "queryType": "Azure Log Analytics",
        "azureLogAnalytics": {"query": BASE + query.strip(), "resources": ["__APPI_ID__"], "resultFormat": fmt},
    }


def metric_target(namespace: str, resource: str, metric: str, aggregation: str, split: str | None = None, ref: str = "A") -> dict:
    target = {
        "refId": ref,
        "datasource": DS,
        "queryType": "Azure Monitor",
        # Grafana builds the metrics URL from this top-level field; without it the request goes to
        # /subscriptions//resourceGroups/... and fails with InvalidSubscriptionId.
        "subscription": "__SUBSCRIPTION_ID__",
        "azureMonitor": {
            "resources": [{"subscription": "__SUBSCRIPTION_ID__", "resourceGroup": "__RESOURCE_GROUP__",
                           "metricNamespace": namespace, "resourceName": resource, "region": "__REGION__"}],
            "metricNamespace": namespace,
            "metricName": metric,
            "aggregation": aggregation,
            "timeGrain": "auto",
            "dimensionFilters": [],
        },
    }
    if split:
        target["azureMonitor"]["dimensionFilters"] = [{"dimension": split, "operator": "eq", "filters": []}]
    return target


def stat(title: str, query: str, unit: str = "short", description: str = "", decimals: int | None = None) -> dict:
    defaults = {"unit": unit, "color": {"mode": "thresholds"}, "thresholds": {"mode": "absolute", "steps": [{"color": "blue", "value": None}]}}
    if decimals is not None:
        defaults["decimals"] = decimals
    return {"type": "stat", "title": title, "description": description, "datasource": DS,
            "targets": [logs_target(query)], "fieldConfig": {"defaults": defaults, "overrides": []},
            "options": {"reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False},
                        "colorMode": "value", "graphMode": "none", "textMode": "auto", "justifyMode": "center"}}


def table(title: str, query: str, description: str = "", overrides: list | None = None) -> dict:
    return {"type": "table", "title": title, "description": description, "datasource": DS,
            "targets": [logs_target(query)],
            "fieldConfig": {"defaults": {"custom": {"align": "auto", "filterable": True}}, "overrides": overrides or []},
            "options": {"showHeader": True, "cellHeight": "sm", "footer": {"show": False, "reducer": ["sum"], "fields": ""}}}


def timeseries(title: str, targets: list[dict], unit: str = "short", bars: bool = False, stacked: bool = False, description: str = "") -> dict:
    custom = {"drawStyle": "bars" if bars else "line", "fillOpacity": 80 if bars else 10, "lineWidth": 1,
              "stacking": {"mode": "normal" if stacked else "none", "group": "A"}, "showPoints": "never"}
    return {"type": "timeseries", "title": title, "description": description, "datasource": DS, "targets": targets,
            "fieldConfig": {"defaults": {"unit": unit, "custom": custom}, "overrides": []},
            "options": {"legend": {"displayMode": "list", "placement": "bottom", "showLegend": True},
                        "tooltip": {"mode": "multi", "sort": "desc"}}}


def build() -> dict:
    g = Layout()

    g.row("Outcomes and ROI")
    g.add(stat("Messages processed", "messages | count", description="Emails processed (latest run per email; duplicates excluded)."), 4, 4)
    g.add(stat("Straight-through rate", f"messages | summarize v = round(100.0 * countif(outcome in {HANDLED}) / count(), 1)",
               unit="percent", description="Share of messages the AI processed end to end - extracted, verified, reconciled and the payment file produced (first pass or AI-resolved)."), 4, 4)
    g.add(stat("Needs review", 'messages | summarize v = countif(outcome == "flagged_for_review")',
               description="Messages the AI extracted and assessed, held for an analyst's decision with the reason and the ACH Assurance Expert's recommendation."), 4, 4)
    g.add(stat("Failed", 'messages | summarize v = countif(outcome == "failed")'), 4, 4)
    g.add(stat("AI cost (est.)", "runs | summarize v = sum(cost)", unit="currencyUSD", decimals=2,
               description="Estimated AI cost of every run in the range (reprocessed emails count each run)."), 4, 4)
    g.add(stat("Cost per message (est.)", "runs | summarize v = sum(cost) / toscalar(messages | count)", unit="currencyUSD", decimals=4), 4, 4)
    g.add(stat("Straight-through, first pass", 'messages | summarize v = countif(outcome == "handled_by_rules")',
               description="Processed end to end on the first pass: extracted (Content Understanding, the ACH Extraction Expert and the table readers as needed), verified against the source, reconciled, payment file produced."), 4, 4)
    g.add(stat("Straight-through, AI-resolved", 'messages | summarize v = countif(outcome == "handled_by_agents")',
               description="Processed end to end after the ACH Resolution Expert reasoned across the sources to recover the lines or a date / reference; its answer was verified against the source before it was applied."), 4, 4)
    g.add(stat("Payment lines processed", "messages | summarize v = sum(lines)"), 4, 4)
    g.add(stat("Dollars processed", "messages | summarize v = sum(paymentTotal)", unit="currencyUSD", decimals=0,
               description="Sum of the stated payment totals."), 4, 4)
    g.add(stat("Time saved (est.)", f"messages | summarize v = countif(outcome in {HANDLED}) * todouble('$minutes') / 60.0",
               unit="h", decimals=1, description="Straight-through messages (first pass and AI-resolved) x Manual minutes per message (a setting)."), 4, 4)
    g.add(stat("Median processing time", "messages | summarize v = percentile(durationMs, 50) / 1000.0", unit="s", decimals=1), 4, 4)
    per_day = timeseries("Messages per day by outcome", [logs_target(
        f"messages | summarize Messages = count() by day, Outcome = {OUTCOME_NAME_KQL} | order by day asc", fmt="time_series")],
        bars=True, stacked=True)
    per_day["fieldConfig"]["defaults"]["displayName"] = "${__field.labels.Outcome}"  # legend: the outcome, not "Messages ..."
    per_day["fieldConfig"]["overrides"] = OUTCOME_COLORS
    g.add(per_day, 16, 8)
    g.add({"type": "text", "title": "About these numbers", "options": {"mode": "markdown", "content": DISCLAIMER}}, 8, 8)

    g.add(table("By payer", f"""
let totals = runs | summarize totalCost = sum(cost) by processingId;
messages
| join kind=leftouter totals on processingId
| summarize Messages = count(), ['Straight-through %'] = round(100.0 * countif(outcome in {HANDLED}) / count(), 1),
            ['Needs review'] = countif(outcome == "flagged_for_review"), Lines = sum(lines), Dollars = round(sum(paymentTotal), 2),
            ['AI cost'] = round(sum(totalCost), 4) by Payer = iff(isempty(payer), "(unknown)", payer)
| extend ['Cost per message'] = round(['AI cost'] / Messages, 4)
| order by Messages desc"""), 24, 8)

    g.row("Cost and usage")
    g.add(timeseries("Estimated AI cost per day by component", [logs_target("""
runs
| mv-expand bagexpansion=array c = e.costByComponent
| summarize Cost = sum(todouble(c[1])) by day, Component = tostring(c[0])
| order by day asc""", fmt="time_series")], unit="currencyUSD", bars=True, stacked=True), 12, 8)
    g.add(table("Calls and usage", """
runs
| summarize Runs = count(), ['CU calls'] = sum(toint(e.cuCalls)), ['CU pages'] = sum(toint(e.cuPages)),
            ['Agent calls'] = sum(toint(e.agentCalls)), ['Decision-1 calls'] = sum(toint(e.decisionCalls)),
            ['AI tokens'] = sum(tolong(e.aiTokens)), ['Est. cost'] = round(sum(cost), 4)"""), 12, 4)
    g.add(table("By message class", f"""
let totals = runs | summarize totalCost = sum(cost) by processingId;
messages
| join kind=leftouter totals on processingId
| summarize Messages = count(), ['Straight-through %'] = round(100.0 * countif(outcome in {HANDLED}) / count(), 1),
            Lines = sum(lines), ['AI cost'] = round(sum(totalCost), 4) by Class = {CLASS_NAME_KQL}
| extend ['Cost per message'] = round(['AI cost'] / Messages, 4)
| order by Class asc""", description="How the payment lines were read: A body table and B spreadsheet (code readers, lowest cost), "
                                     "C small document and D large document (Content Understanding; cost grows with lines), none = no lines."), 12, 4)
    g.add(table("By remittance date", f"""
let totals = runs | summarize totalCost = sum(cost) by processingId;
messages
| join kind=leftouter totals on processingId
| summarize Messages = count(), ['Straight-through %'] = round(100.0 * countif(outcome in {HANDLED}) / count(), 1),
            Lines = sum(lines), Dollars = round(sum(paymentTotal), 2), ['AI cost'] = round(sum(totalCost), 4)
            by ['Remittance date'] = iff(isempty(remittanceDate), "(none)", remittanceDate)
| order by ['Remittance date'] desc"""), 12, 8)
    g.add(table("By calendar date (business time zone)", f"""
let totals = runs | summarize totalCost = sum(cost) by processingId;
messages
| join kind=leftouter totals on processingId
| summarize Messages = count(), ['Straight-through %'] = round(100.0 * countif(outcome in {HANDLED}) / count(), 1),
            Lines = sum(lines), ['AI cost'] = round(sum(totalCost), 4) by Day = format_datetime(day, 'yyyy-MM-dd')
| extend ['Cost per message'] = round(['AI cost'] / Messages, 4)
| order by Day desc"""), 12, 8)

    g.row("Runs")
    g.add(table("Runs (one row per email)", f"""
let totals = runs | summarize totalCost = sum(cost), Runs = count() by processingId;
messages
| join kind=leftouter totals on processingId
| project Processed = timestamp, ['Processing ID'] = processingId, Payer = payer, ['Remittance date'] = remittanceDate,
          Class = {CLASS_NAME_KQL}, Outcome = {OUTCOME_NAME_KQL}, Lines = lines, Payment = paymentTotal,
          ['CU pages'] = toint(e.cuPages), ['Agent calls'] = toint(e.agentCalls), Tokens = tolong(e.aiTokens),
          ['Est. cost'] = round(totalCost, 4), Runs, ['Duration (s)'] = round(durationMs / 1000.0, 1)
| order by Processed desc""", description="Latest run per email; Est. cost sums every run of the email. Select a Processing ID to open Run Details.",
        overrides=[{"matcher": {"id": "byName", "options": "Processing ID"},
                    "properties": [{"id": "links", "value": [{"title": "Open Run Details", "url": "__UI_URL__/runs/${__value.raw}", "targetBlank": True}]}]}]),
        24, 10)

    g.row("Operations")
    g.add(timeseries("Processing time per day, p50 / p95", [logs_target("""
messages
| summarize p50 = percentile(durationMs, 50) / 1000.0, p95 = percentile(durationMs, 95) / 1000.0 by day
| order by day asc""", fmt="time_series")], unit="s"), 12, 8)
    g.add(table("Average time per step", """
messages
| mv-expand bagexpansion=array s = e.stepDurationsMs
| summarize ['Average (s)'] = round(avg(todouble(s[1])) / 1000.0, 1), ['p95 (s)'] = round(percentile(todouble(s[1]), 95) / 1000.0, 1)
            by Step = tostring(s[0])
| order by ['Average (s)'] desc"""), 12, 8)
    g.add(table("Review reasons", """
messages
| where outcome == "flagged_for_review"
| mv-expand reason = e.reviewReasons
| summarize Messages = count() by Reason = tostring(reason)
| order by Messages desc"""), 8, 8)
    g.add(table("Failures by stage", """
messages
| where outcome == "failed"
| summarize Messages = count() by Stage = tostring(e.stage), Error = tostring(e.errorCode)
| order by Messages desc"""), 8, 8)
    g.add(stat("Throttle retries (CU 429 / 5xx)", "runs | summarize v = sum(toint(e.throttleRetries))"), 8, 4)
    g.add(stat("Duplicates skipped", 'runs | summarize v = countif(outcome == "duplicate")'), 8, 4)

    g.row("Capacity (platform metrics)")
    foundry = ("Microsoft.CognitiveServices/accounts", "__FOUNDRY_NAME__")
    g.add(timeseries("Model tokens by deployment", [
        metric_target(*foundry, "ProcessedPromptTokens", "Total", split="ModelDeploymentName", ref="A"),
        metric_target(*foundry, "GeneratedTokens", "Total", split="ModelDeploymentName", ref="B"),
    ], description="Input (prompt) and output (generated) tokens per model deployment."), 12, 8)
    g.add(timeseries("Model requests by status code", [
        metric_target(*foundry, "AzureOpenAIRequests", "Total", split="StatusCode"),
    ], description="429 = throttled (raise TPM or lower concurrency)."), 12, 8)
    servicebus = ("Microsoft.ServiceBus/namespaces", "__SERVICEBUS_NAME__")
    g.add(timeseries("Intake queue: active and dead-lettered", [
        metric_target(*servicebus, "ActiveMessages", "Maximum", ref="A"),
        metric_target(*servicebus, "DeadletteredMessages", "Maximum", ref="B"),
    ]), 24, 8)

    return {
        "title": "ACH Remittance Processing",
        "uid": "ach-remittance",
        "description": "Outcomes, cost per message, runs, operations and capacity for ACH remittance processing.",
        "editable": True,
        "graphTooltip": 1,
        "schemaVersion": 39,
        "time": {"from": "now-7d", "to": "now"},
        "refresh": "",
        "tags": ["ach-remittance"],
        "templating": {"list": [
            {"name": "ds", "label": "Data source", "type": "datasource", "query": "grafana-azure-monitor-datasource", "hide": 2},
            # Grafana inserts a custom "All" value as-is (no :singlequote format), so it carries its own quotes.
            {"name": "payer", "label": "Payer", "type": "query", "datasource": DS, "includeAll": True, "allValue": ALL_VALUE,
             "multi": False, "refresh": 2, "sort": 1, "current": {"text": "All", "value": "$__all"},
             "query": {"refId": "payer", "queryType": "Azure Log Analytics", "azureLogAnalytics": {
                 "resources": ["__APPI_ID__"], "resultFormat": "table",
                 "query": 'traces | where $__timeFilter(timestamp) | where message startswith "remittance_run_completed " '
                          '| extend p = tostring(parse_json(substring(message, 25)).payerName) | where isnotempty(p) '
                          '| distinct p | order by p asc'}}},
            {"name": "class", "label": "Message class", "type": "custom", "includeAll": True, "allValue": ALL_VALUE, "multi": False,
             # "label : value" - the dropdown shows the label, queries get the class letter.
             "query": ", ".join(f"{label} : {value}" for value, label in CLASS_LABELS.items()),
             "current": {"text": "All", "value": "$__all"}},
            {"name": "tz", "label": "Business time zone", "type": "custom", "multi": False,
             "query": "America/New_York,America/Chicago,America/Denver,America/Los_Angeles,UTC",
             "current": {"text": "America/New_York", "value": "America/New_York"}},
            {"name": "minutes", "label": "Manual minutes per message", "type": "textbox", "query": "10",
             "current": {"text": "10", "value": "10"}},
        ]},
        "panels": g.panels,
    }


def log_queries(dashboard: dict):
    for panel in dashboard["panels"]:
        for target in panel.get("targets", []):
            if "azureLogAnalytics" in target:
                yield panel["title"], target["azureLogAnalytics"]["query"]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--emit-tests", type=Path, help="also write each log query, with test values, to this folder")
    args = parser.parse_args()
    dashboard = build()
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(dashboard, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {OUT} ({len(dashboard['panels'])} panels)")
    if args.emit_tests:
        args.emit_tests.mkdir(parents=True, exist_ok=True)
        for index, (title, query) in enumerate(log_queries(dashboard), 1):
            for placeholder, value in TEST_VALUES.items():
                query = query.replace(placeholder, value)
            name = "".join(ch if ch.isalnum() else "-" for ch in title.lower()).strip("-")
            (args.emit_tests / f"{index:02d}-{name}.kql").write_text(query, encoding="utf-8")
        print(f"wrote test queries to {args.emit_tests}")


if __name__ == "__main__":
    main()
