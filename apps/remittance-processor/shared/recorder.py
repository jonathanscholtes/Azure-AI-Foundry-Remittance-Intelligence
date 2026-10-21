"""Recorder: the single writer of governed run state.

Every state change in an orchestration is recorded here and nowhere else:
  * one event row per transition in the events table (the live timeline the UI polls),
  * a merge-upsert of the run-summary row in the ledger table,
  * a ``workflow_event`` trace (Application Insights / Log Analytics),
  * on ``finalize``: the full audit JSON blob (and an error blob for failed runs), and the
    ``remittance_run_completed`` run event (shared/run_metrics.py) for the dashboards.

Unlike the old best-effort ledger writes, the Recorder raises on a storage failure so the Durable
retry policy re-runs it; writes are idempotent (deterministic RowKeys + merge upserts).
"""

from __future__ import annotations

import json
import logging

from shared.ledger import build_entity, build_event_entity, event_row_key
from shared.run_metrics import RUN_EVENT
from shared.storage import upload_json

logger = logging.getLogger("remittance_intake")

TERMINAL_STATES = {"Completed", "ReviewRequired", "Failed", "DuplicateSkipped"}


def _log_event(processing_id: str, run_id: str, correlation_id: str | None, event: dict) -> None:
    payload = {
        "processingId": processing_id,
        "runId": run_id,
        "correlationId": correlation_id,
        "event": event.get("event") or "state_transition",
        "eventTimeUtc": event.get("timeUtc"),
        "previousState": event.get("previousState"),
        "newState": event.get("newState"),
        "stage": event.get("stage"),
        **(event.get("details") or {}),
    }
    level = logging.ERROR if event.get("newState") == "Failed" else logging.INFO
    logger.log(level, "workflow_event %s", json.dumps(payload, sort_keys=True, default=str))


def _run_events(events_client, processing_id: str, run_id: str) -> list[dict]:
    if events_client is None:
        return []
    low = event_row_key(run_id, 0)
    high = event_row_key(run_id, 9999)
    rows = events_client.query_entities(
        query_filter="PartitionKey eq @pk and RowKey ge @low and RowKey le @high",
        parameters={"pk": processing_id, "low": low, "high": high},
    )
    events = []
    for row in sorted(rows, key=lambda r: r.get("RowKey", "")):
        try:
            details = json.loads(row.get("detailsJson") or "{}")
        except ValueError:
            details = {}
        events.append(
            {
                "seq": row.get("seq"),
                "event": row.get("event"),
                "eventTimeUtc": row.get("eventTimeUtc"),
                "previousState": row.get("previousState"),
                "newState": row.get("newState"),
                "stage": row.get("stage"),
                **details,
            }
        )
    return events


def record(payload: dict, *, ledger_client, events_client, blob_service) -> dict:
    """Persist one batch of events + ledger fields for a run. Returns the recorded state."""
    processing_id = payload["processingId"]
    run_id = payload["runId"]
    correlation_id = payload.get("correlationId")
    events = payload.get("events") or []

    last_state = None
    last_time = None
    for event in events:
        if events_client is not None:
            entity = build_event_entity(
                processing_id,
                run_id,
                event["seq"],
                {**event, "correlationId": correlation_id},
            )
            events_client.upsert_entity(entity)
        _log_event(processing_id, run_id, correlation_id, event)
        if event.get("newState"):
            last_state = event["newState"]
        last_time = event.get("timeUtc") or last_time

    fields = dict(payload.get("ledger") or {})
    if last_state:
        fields.setdefault("currentState", last_state)
        fields.setdefault("status", last_state)
    if last_time:
        fields.setdefault("updatedAtUtc", last_time)
    fields.setdefault("correlationId", correlation_id)
    if ledger_client is not None and fields:
        ledger_client.upsert_entity(build_entity(processing_id, run_id, fields))

    audit_blob_uri = None
    if payload.get("finalize") and blob_service is not None:
        audit_blob_uri = upload_json(
            blob_service,
            "audit",
            f"{processing_id}/{run_id}.json",
            {
                "processingId": processing_id,
                "runId": run_id,
                "correlationId": correlation_id,
                "events": _run_events(events_client, processing_id, run_id),
            },
        )
        error = payload.get("error")
        if error:
            upload_json(
                blob_service,
                "errors",
                f"{processing_id}/{run_id}.json",
                {
                    "processingId": processing_id,
                    "runId": run_id,
                    "correlationId": correlation_id,
                    "status": "Failed",
                    **error,
                },
            )

    # One run event per finished run (after the ledger write, so a storage failure retries first). A
    # Durable retry of this activity can log it twice; the dashboards keep one per processingId + runId.
    if payload.get("finalize") and payload.get("runEvent"):
        logger.info("%s %s", RUN_EVENT, json.dumps(payload["runEvent"], sort_keys=True, default=str))

    return {"state": last_state, "auditBlobUri": audit_blob_uri}
