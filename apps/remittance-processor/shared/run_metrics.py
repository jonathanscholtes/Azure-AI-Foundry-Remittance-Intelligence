"""Run classification and the run event - pure, shared by the orchestrator.

* **Message class**, from what produced the payment lines:
  A body table (the table readers), B spreadsheet (the spreadsheet parser), C small document
  (Content Understanding on a few pages / lines), D large document (CU on many lines or pages, or
  chunked text). The most expensive source wins; ``none`` when no source produced lines. Review is a
  separate flag (the "+R" add-on), not a class.
* **Outcome** - every run lands in exactly one: handled_by_rules (completed on the first pass - which
  can include Content Understanding and the ACH Extraction Expert; "rules" means the code checks
  verified it, not that no AI ran), handled_by_agents (completed only after a second pass by the ACH
  Resolution Expert recovered lines or a date / reference), flagged_for_review, failed, duplicate.
  Dashboards show them as "Straight-through, first pass" / "Straight-through, AI-resolved" / "Needs
  review" / "Failed".
* **Run event** - one ``remittance_run_completed`` record per finished run, written by the Recorder
  as a structured trace (like ``workflow_event``), for the Azure Monitor dashboards and alerts.
  Payment lines stay in the ledger and results; the processingId links to them.
"""

from __future__ import annotations

RUN_EVENT = "remittance_run_completed"
RUN_EVENT_VERSION = "1.0"

LARGE_LINES = 100   # D above this many lines from one CU source
LARGE_PAGES = 5     # ... or this many pages analyzed for it
_TABLE_STRATEGIES = ("html_table+content_understanding", "text_table+content_understanding")
_CLASS_ORDER = {"none": 0, "A": 1, "B": 2, "C": 3, "D": 4}


def _pages(item: dict) -> int:
    pages = ((item.get("usage") or {}).get("contentUnderstanding") or {}).get("pages") or {}
    return sum(pages.values())


def source_class(item: dict) -> str:
    """The class one extracted source puts the message in ("none" when it produced no lines)."""
    lines = item.get("lineCount")
    if lines is None:
        lines = len((item.get("normalized") or {}).get("paymentLines") or [])
    if not lines:
        return "none"
    strategy = item.get("strategy") or ""
    if strategy in _TABLE_STRATEGIES:
        return "A"
    if strategy == "deterministic_parser":
        return "B"
    chunked = ((item.get("normalized") or {}).get("chunkCount") or 1) > 1
    if chunked or lines > LARGE_LINES or _pages(item) > LARGE_PAGES:
        return "D"
    return "C"


def message_class(extractions: list[dict]) -> str:
    classes = [source_class(item) for item in extractions or []]
    return max(classes, key=_CLASS_ORDER.get, default="none")


def outcome(status: str, resolution: dict | None = None) -> str:
    if status == "Failed":
        return "failed"
    if status == "DuplicateSkipped":
        return "duplicate"
    if status == "ReviewRequired":
        return "flagged_for_review"
    resolution = resolution or {}
    applied = (resolution.get("details") or {}).get("applied")
    if resolution.get("status") == "accepted" or applied:
        return "handled_by_agents"
    return "handled_by_rules"


# Ledger fields copied into the event as they are.
_EVENT_FIELDS = (
    "status", "stream", "submittedAtUtc", "processedAtUtc", "processedDate", "durationMs", "queueLatencyMs",
    "payerName", "remittanceDate", "paymentReference", "currency", "paymentTotal", "calculatedPaymentTotal",
    "lineCount", "isReconciled", "requiresReview", "reviewReasons", "errorCode", "stage",
    "sourceSizeBytes", "attachmentCount", "candidateCount", "throttleRetries",
    "messageClass", "outcome", "assuranceDecision", "amountAtRisk",
    "cuCalls", "cuPages", "agentCalls", "decisionCalls", "aiTokens", "estimatedCost", "estimatedCostComplete",
)


def run_event(processing_id: str, run_id: str, correlation_id: str | None, ledger: dict, *,
              steps: dict | None = None, cost: dict | None = None) -> dict:
    """The run event: the terminal ledger fields plus step durations and cost per component."""
    event = {"event": RUN_EVENT, "schemaVersion": RUN_EVENT_VERSION,
             "processingId": processing_id, "runId": run_id, "correlationId": correlation_id}
    event.update({key: ledger[key] for key in _EVENT_FIELDS if ledger.get(key) is not None})
    if steps:
        event["stepDurationsMs"] = {name: value for name, value in steps.items() if value is not None}
    if cost:
        event["costByComponent"] = cost.get("byComponent")
        event["pricingVersion"] = cost.get("pricingVersion")
        event["pricingBasis"] = cost.get("basis")
    return event
