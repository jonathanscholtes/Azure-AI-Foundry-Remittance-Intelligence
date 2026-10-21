"""Run-summary ledger in Azure Table Storage.

The ledger is a queryable index the demo UI reads: one row per run, keyed by
(processingId, runId), holding status + summary metadata + blob pointers. A
second table holds the per-run event timeline (PartitionKey=processingId,
RowKey=runId_seq) so the UI can show progress while a run is in flight. Blob
Storage remains the content plane; neither table holds document bytes.

Only the Recorder activity (shared/recorder.py) writes these tables.
"""

from __future__ import annotations

import json
import logging
import os

logger = logging.getLogger("remittance_intake")


def ledger_table_name() -> str:
    return os.environ.get("LEDGER_TABLE_NAME", "remittances")


def events_table_name() -> str:
    return os.environ.get("EVENTS_TABLE_NAME", "remittanceevents")


def _table_client(table_name: str):
    """Return a Table client, or None if unconfigured/unavailable. Never raises."""
    connection_string = os.environ.get("STORAGE_TABLE_CONNECTION_STRING")
    endpoint = os.environ.get("STORAGE_TABLE_ENDPOINT")
    if not connection_string and not endpoint:
        return None
    try:
        from azure.data.tables import TableServiceClient

        if connection_string:
            service = TableServiceClient.from_connection_string(connection_string)
        else:
            from azure.identity import DefaultAzureCredential

            service = TableServiceClient(endpoint=endpoint, credential=DefaultAzureCredential())
        client = service.get_table_client(table_name)
        try:
            client.create_table()
        except Exception:
            pass  # table already exists
        return client
    except Exception:
        logger.warning("table client unavailable table=%s", table_name)
        return None


def ledger_table_client():
    """Return a Table client for the ledger, or None if unconfigured/unavailable."""
    return _table_client(ledger_table_name())


def events_table_client():
    """Return a Table client for the per-run event timeline, or None if unconfigured."""
    return _table_client(events_table_name())


def event_row_key(run_id: str, seq: int) -> str:
    """Events sort by run then sequence, so one partition query returns an ordered timeline."""
    return f"{run_id}_{int(seq):04d}"


def build_event_entity(processing_id: str, run_id: str, seq: int, event: dict) -> dict:
    details = event.get("details") or {}
    entity = {
        "PartitionKey": processing_id,
        "RowKey": event_row_key(run_id, seq),
        "runId": run_id,
        "seq": int(seq),
        "event": event.get("event") or "state_transition",
        "eventTimeUtc": event.get("timeUtc"),
        "detailsJson": json.dumps(details, sort_keys=True, default=str)[:30000],
    }
    for key in ("newState", "previousState", "stage", "correlationId"):
        if event.get(key) is not None:
            entity[key] = str(event[key])
    if isinstance(details.get("durationMs"), (int, float)):
        entity["durationMs"] = int(details["durationMs"])
    return entity


def _flatten(value):
    """Coerce a value to a Table-storable scalar, or None to omit it."""
    if value is None:
        return None
    if isinstance(value, bool) or isinstance(value, (int, float, str)):
        return value
    if isinstance(value, (list, tuple, set)):
        return ", ".join(str(item) for item in value)
    return str(value)


def build_entity(processing_id: str, run_id: str, fields: dict) -> dict:
    """Build a flat Table entity keyed by (processingId, runId), omitting empties."""
    entity = {"PartitionKey": processing_id, "RowKey": run_id}
    for key, value in fields.items():
        flat = _flatten(value)
        if flat is not None:
            entity[key] = flat
    return entity


def upsert_run(client, processing_id: str, run_id: str, **fields) -> bool:
    """Merge-upsert a run-summary row. Best-effort; returns False instead of raising."""
    if client is None or not processing_id or not run_id:
        return False
    try:
        entity = build_entity(processing_id, run_id, fields)
        client.upsert_entity(entity)  # default mode is merge
        return True
    except Exception:
        logger.warning(
            "ledger upsert failed processingId=%s runId=%s", processing_id, run_id
        )
        return False
