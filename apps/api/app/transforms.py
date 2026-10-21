"""Pure data transforms for the ledger API (no Azure SDK imports, so unit-testable)."""

from __future__ import annotations

import json
import re
from collections import Counter, defaultdict
from datetime import datetime, timedelta

PROCESSING_ID_RE = re.compile(r"^REM-[0-9A-F]{20}$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
WEEK_RE = re.compile(r"^\d{4}-W\d{2}$")
TERMINAL_STATES = {"Completed", "ReviewRequired", "Failed", "DuplicateSkipped"}
KNOWN_STATES = TERMINAL_STATES | {
    "Queued", "Received", "Archived", "Parsed", "CandidatesIdentified", "Extracting",
    "Extracted", "Validating", "Validated", "Reconciled", "AssuranceReview", "Assessed",
}
# Containers + per-processing prefixes the UI may download artifacts from.
ARTIFACT_CONTAINERS = ("incoming", "attachments", "bodies", "candidates", "analysis", "results", "manifests", "audit", "errors")


def to_float(value):
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(str(value).replace("$", "").replace(",", "").strip())
    except (TypeError, ValueError):
        return None


def to_bool(value):
    if value is None:
        return None
    return bool(value)


SUMMARY_FIELDS = (
    "currentState", "payerName", "remittanceDate", "paymentReference", "currency", "lineCount",
    "sourceName", "sourceSha256", "sourceSizeBytes", "attachmentCount", "candidateCount", "subject",
    "senderEmail", "stream", "csvFileName", "csvBlobUri", "finalResultBlobUri", "runFinalResultBlobUri",
    "validationBlobUri", "assuranceBlobUri", "auditBlobUri", "manifestBlobUri", "processedAtUtc",
    "processedDate", "processedWeek", "createdAtUtc", "submittedAtUtc", "startedAtUtc", "updatedAtUtc",
    "durationMs", "queueLatencyMs", "throttleRetries", "errorCode", "stage", "correlationId",
    "durableInstanceId", "resultStatus", "assuranceDecision", "assuranceCategory", "assuranceHeadline",
    "assuranceIssueCount",
    # AI usage totals per run (cost per message); absent on runs processed before usage was recorded.
    "cuCalls", "cuPages", "agentCalls", "decisionCalls", "aiTokens", "estimatedCost", "estimatedCostComplete",
    # Run classification (message class A-D / none, outcome bucket).
    "messageClass", "outcome",
)


def entity_to_summary(entity: dict) -> dict:
    warnings = entity.get("warnings")
    review_reasons = entity.get("reviewReasons")
    summary = {
        "processingId": entity.get("PartitionKey"),
        "runId": entity.get("RowKey"),
        "status": entity.get("status"),
        "requiresReview": to_bool(entity.get("requiresReview")),
        "paymentTotal": to_float(entity.get("paymentTotal")),
        "calculatedPaymentTotal": to_float(entity.get("calculatedPaymentTotal")),
        "totalDifference": to_float(entity.get("totalDifference")),
        "isReconciled": to_bool(entity.get("isReconciled")),
        "amountAtRisk": to_float(entity.get("amountAtRisk")),
        "assuranceConfidence": to_float(entity.get("assuranceConfidence")),
        "warnings": [w.strip() for w in warnings.split(",")] if warnings else [],
        # Blocking reasons only (runs recorded before this field: None -> fall back to warnings).
        "reviewReasons": [w.strip() for w in review_reasons.split(",") if w.strip()] if review_reasons is not None else None,
    }
    for field in SUMMARY_FIELDS:
        summary[field] = entity.get(field)
    return summary


def event_entity_to_event(entity: dict) -> dict:
    try:
        details = json.loads(entity.get("detailsJson") or "{}")
    except ValueError:
        details = {}
    return {
        "seq": entity.get("seq"),
        "runId": entity.get("runId"),
        "event": entity.get("event"),
        "eventTimeUtc": entity.get("eventTimeUtc"),
        "previousState": entity.get("previousState"),
        "newState": entity.get("newState"),
        "stage": entity.get("stage"),
        "durationMs": entity.get("durationMs"),
        "details": details,
    }


def latest_per_processing(entities):
    latest: dict[str, dict] = {}
    for entity in entities:
        pk = entity.get("PartitionKey")
        stamp = entity.get("updatedAtUtc") or ""
        if pk not in latest or stamp > (latest[pk].get("updatedAtUtc") or ""):
            latest[pk] = entity
    return list(latest.values())


def _percentile(values, pct):
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, round(pct / 100 * (len(ordered) - 1))))
    return int(ordered[index])


def by_payer(runs):
    """Payment-processing roll-up: one row per payer for the review team."""
    groups: dict[str, dict] = defaultdict(
        lambda: {"runs": 0, "lines": 0, "statedAmount": 0.0, "calculatedAmount": 0.0, "completed": 0,
                 "reviewRequired": 0, "failed": 0, "unreconciledAmount": 0.0, "amountAtRisk": 0.0}
    )
    for run in runs:
        payer = run.get("payerName") or "(unknown payer)"
        g = groups[payer]
        g["runs"] += 1
        g["lines"] += run.get("lineCount") or 0
        g["statedAmount"] += run.get("paymentTotal") or 0
        g["calculatedAmount"] += run.get("calculatedPaymentTotal") or 0
        g["unreconciledAmount"] += abs(run.get("totalDifference") or 0) if run.get("isReconciled") is False else 0
        g["amountAtRisk"] += run.get("amountAtRisk") or 0
        if run.get("status") == "Completed":
            g["completed"] += 1
        elif run.get("status") == "ReviewRequired":
            g["reviewRequired"] += 1
        elif run.get("status") == "Failed":
            g["failed"] += 1
        g["lastRemittanceDate"] = max(filter(None, [g.get("lastRemittanceDate"), run.get("remittanceDate")]), default=None)
    rows = []
    for payer, g in groups.items():
        rows.append({"payerName": payer, **{k: round(v, 2) if isinstance(v, float) else v for k, v in g.items()}})
    rows.sort(key=lambda r: r["calculatedAmount"], reverse=True)
    return rows


def aggregate(runs, *, scope):
    count = len(runs)
    statuses = Counter(r.get("status") for r in runs if r.get("status"))
    review = sum(1 for r in runs if r.get("requiresReview"))
    durations = [r["durationMs"] for r in runs if isinstance(r.get("durationMs"), (int, float))]
    # "Top review reasons": only what put runs in review, not informational notes.
    warnings = Counter(
        w for r in runs
        for w in (r.get("reviewReasons") if r.get("reviewReasons") is not None else (r.get("warnings") or []))
    )
    decisions = Counter(r.get("assuranceDecision") for r in runs if r.get("assuranceDecision"))
    total_processed = sum(r.get("calculatedPaymentTotal") or 0 for r in runs)
    total_stated = sum(r.get("paymentTotal") or 0 for r in runs)
    total_lines = sum(r.get("lineCount") or 0 for r in runs)
    return {
        "scope": scope,
        "count": count,
        "byStatus": dict(statuses),
        "completed": statuses.get("Completed", 0),
        "reviewRequired": review,
        "failed": statuses.get("Failed", 0),
        "reviewRate": round(review / count, 4) if count else 0.0,
        "totalProcessedAmount": round(total_processed, 2),
        "totalStatedAmount": round(total_stated, 2),
        "totalPaymentLines": total_lines,
        "payerCount": len({r.get("payerName") for r in runs if r.get("payerName")}),
        "amountAtRisk": round(sum(r.get("amountAtRisk") or 0 for r in runs), 2),
        "avgDurationMs": round(sum(durations) / len(durations)) if durations else None,
        "p95DurationMs": _percentile(durations, 95),
        "topWarnings": [{"warning": w, "count": c} for w, c in warnings.most_common(5)],
        "assuranceDecisions": dict(decisions),
        "byPayer": by_payer(runs),
    }


def _parse_time(value):
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed


def pipeline_snapshot(runs, *, now: datetime, window_minutes: int = 60, bucket_minutes: int = 5):
    """Live pipeline view: work in flight by stage + recent throughput and latency."""
    window_start = now - timedelta(minutes=window_minutes)
    in_flight = Counter()
    recent = []
    for run in runs:
        if run.get("status") not in TERMINAL_STATES:
            in_flight[run.get("currentState") or run.get("status") or "Queued"] += 1
            continue
        processed = _parse_time(run.get("processedAtUtc") or run.get("updatedAtUtc"))
        if processed and processed >= window_start:
            recent.append((processed, run))

    buckets = []
    bucket_count = window_minutes // bucket_minutes
    for index in range(bucket_count):
        start = window_start + timedelta(minutes=index * bucket_minutes)
        end = start + timedelta(minutes=bucket_minutes)
        in_bucket = [run for processed, run in recent if start <= processed < end]
        buckets.append(
            {
                "start": start.isoformat(),
                "completed": sum(1 for r in in_bucket if r.get("status") == "Completed"),
                "reviewRequired": sum(1 for r in in_bucket if r.get("status") == "ReviewRequired"),
                "failed": sum(1 for r in in_bucket if r.get("status") == "Failed"),
                "duplicate": sum(1 for r in in_bucket if r.get("status") == "DuplicateSkipped"),
            }
        )
    durations = [r["durationMs"] for _, r in recent if isinstance(r.get("durationMs"), (int, float))]
    latencies = [r["queueLatencyMs"] for _, r in recent if isinstance(r.get("queueLatencyMs"), (int, float))]
    return {
        "asOfUtc": now.isoformat(),
        "windowMinutes": window_minutes,
        "inFlight": sum(in_flight.values()),
        "inFlightByState": dict(in_flight),
        "processedInWindow": len(recent),
        "throughputPerMinute": round(len(recent) / window_minutes, 2),
        "p50DurationMs": _percentile(durations, 50),
        "p95DurationMs": _percentile(durations, 95),
        "avgQueueLatencyMs": round(sum(latencies) / len(latencies)) if latencies else None,
        "throttleRetries": sum(r.get("throttleRetries") or 0 for _, r in recent),
        "buckets": buckets,
    }


def artifact_allowed(container: str, path: str, processing_id: str) -> bool:
    """Only artifacts that belong to this processingId can be downloaded through the API."""
    if container not in ARTIFACT_CONTAINERS or not PROCESSING_ID_RE.match(processing_id):
        return False
    if ".." in path.split("/") or path.startswith("/"):
        return False
    return path == f"{processing_id}.json" or path.startswith(f"{processing_id}/") or path.startswith(f"{processing_id}.")
