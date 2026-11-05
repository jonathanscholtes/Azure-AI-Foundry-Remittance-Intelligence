"""Ledger repository: reads run summaries/events from Table Storage, artifacts from Blob, and
submits work to the Service Bus intake queue.

This is the single seam the UI depends on. Swapping Table Storage for Cosmos DB later means
providing another class with the same methods - no API/UI change. The API never writes pipeline
run state (the Function's Recorder does); it writes the raw source blob, the queue message, and
an analyst's sign-off on a ReviewRequired run (review fields on the run row + one timeline event).
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
import uuid
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from pathlib import PurePosixPath

from .settings import get_settings
from .storage import blob_service, events_table, intake_bus, ledger_table
from .transforms import (
    ARTIFACT_CONTAINERS,
    BODY_SOURCE_ID,
    DATE_RE,
    KNOWN_STATES,
    PROCESSING_ID_RE,
    REVIEW_STATES,
    WEEK_RE,
    aggregate,
    analysis_markdown,
    artifact_allowed,
    csv_file_name,
    entity_to_summary,
    event_entity_to_event,
    latest_per_processing,
    page_text,
    payment_csv,
    pipeline_snapshot,
    review_case,
    review_run_row,
    review_state,
)

SUPPORTED_EXTENSIONS = (".msg", ".eml")
PIPELINE_CACHE_SECONDS = 2.0

logger = logging.getLogger("ach_api")


class QueueUnavailable(RuntimeError):
    pass


class ReviewConflict(RuntimeError):
    """The run is not open for review (wrong status, already completed, or changed concurrently)."""


def _safe_name(filename: str) -> str:
    name = PurePosixPath(filename.replace("\\", "/")).name
    name = "".join(ch if ch.isalnum() or ch in " ._-()" else "_" for ch in name).strip(" .")
    return name[:200] or "message.msg"


class LedgerRepository:
    def __init__(self, table=None, blob=None, events=None, bus=None, incoming_container: str | None = None):
        self._table = table if table is not None else ledger_table()
        self._blob = blob if blob is not None else blob_service()
        self._events = events if events is not None else events_table()
        self._bus = bus if bus is not None else intake_bus()
        self._incoming = incoming_container or get_settings().incoming_container
        self._pipeline_cache: dict[int, tuple[float, dict]] = {}

    # --- helpers -------------------------------------------------------------

    def _query(self, query_filter: str | None, parameters: dict | None = None):
        if query_filter:
            return list(self._table.query_entities(query_filter=query_filter, parameters=parameters))
        return list(self._table.list_entities())

    def _since_filter(self, days: int):
        since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        return "updatedAtUtc ge @since", {"since": since}

    def _read_json(self, blob_uri: str | None):
        if not blob_uri:
            return None
        container, _, name = blob_uri.partition("/")
        if not container or not name:
            return None
        try:
            return json.loads(self._blob.get_blob_client(container, name).download_blob().readall())
        except Exception:
            return None

    def _runs_for(self, processing_id: str):
        return self._query("PartitionKey eq @pk", {"pk": processing_id})

    def _latest(self, processing_id: str):
        if not PROCESSING_ID_RE.match(processing_id):
            return None
        entities = self._runs_for(processing_id)
        if not entities:
            return None
        return entity_to_summary(latest_per_processing(entities)[0]), entities

    # --- reads ---------------------------------------------------------------

    def list_runs(self, status=None, date_from=None, date_to=None, payer=None, search=None, limit=100):
        filters, params = [], {}
        if date_from and DATE_RE.match(date_from):
            filters.append("processedDate ge @dfrom")
            params["dfrom"] = date_from
        if date_to and DATE_RE.match(date_to):
            filters.append("processedDate le @dto")
            params["dto"] = date_to
        if not filters:
            clause, since = self._since_filter(get_settings().default_window_days)
            filters.append(clause)
            params.update(since)
        entities = self._query(" and ".join(filters), params)
        summaries = [entity_to_summary(e) for e in latest_per_processing(entities)]
        if status == "InFlight":
            summaries = [s for s in summaries if s["status"] not in {"Completed", "ReviewRequired", "Failed", "DuplicateSkipped"}]
        elif status and status in KNOWN_STATES:
            summaries = [s for s in summaries if s["status"] == status]
        if payer:
            summaries = [s for s in summaries if (s.get("payerName") or "").lower() == payer.lower()]
        if search:
            needle = search.lower()
            summaries = [
                s for s in summaries
                if any(needle in str(s.get(k) or "").lower() for k in ("payerName", "sourceName", "processingId", "subject", "paymentReference"))
            ]
        summaries.sort(key=lambda s: s.get("submittedAtUtc") or s.get("updatedAtUtc") or "", reverse=True)
        return summaries[: max(1, min(int(limit), 1000))]

    def _queued_placeholder(self, processing_id: str):
        # Accepted but not yet picked up: the source blob exists, the Function has not written a
        # ledger row. Report it as Queued (read-only) instead of 404 so pollers see the real state.
        if not PROCESSING_ID_RE.match(processing_id):
            return None
        container = self._blob.get_container_client(self._incoming)
        blob = next(iter(container.list_blobs(name_starts_with=f"{processing_id}/")), None)
        if blob is None:
            return None
        modified = getattr(blob, "last_modified", None)
        stamp = modified.isoformat() if modified else None
        return {
            "processingId": processing_id,
            "runId": None,
            "status": "Queued",
            "currentState": "Queued",
            "sourceName": PurePosixPath(blob.name).name,
            "sourceSizeBytes": blob.size,
            "submittedAtUtc": stamp,
            "updatedAtUtc": stamp,
            "requiresReview": False,
            "warnings": [],
            "runs": [],
            "events": [],
        }

    def get_run(self, processing_id: str):
        found = self._latest(processing_id)
        if not found:
            return self._queued_placeholder(processing_id)
        summary, entities = found
        summary["runs"] = sorted(
            ({"runId": e.get("RowKey"), "status": e.get("status"), "updatedAtUtc": e.get("updatedAtUtc")} for e in entities),
            key=lambda r: r.get("updatedAtUtc") or "",
            reverse=True,
        )
        final = self._read_json(summary.get("finalResultBlobUri"))
        if final and final.get("runId") in (None, summary["runId"]):
            for key in ("remittanceMetadata", "paymentLines", "quality", "candidateResults", "manualReviewItems", "assurance", "outputFile",
                        "sourceClassification", "resolution", "paymentGroups", "usage"):
                summary[key] = final.get(key)
        manifest = self._read_json(summary.get("manifestBlobUri") or f"manifests/{processing_id}.json")
        if manifest:
            summary["message"] = {
                k: (manifest.get("message") or {}).get(k)
                for k in ("subject", "senderName", "senderEmail", "sentDate", "hasBody", "hasAttachments")
            }
            summary["sourceCandidates"] = [
                {
                    k: c.get(k)
                    for k in ("candidateId", "sourceType", "name", "extension", "contentType", "size", "isInline",
                              "classification", "score", "selectionReasons", "blobUri")
                }
                for c in manifest.get("candidates") or []
            ]
            summary["recommendedProcessing"] = manifest.get("recommendedProcessing")
        summary["events"] = self.get_events(processing_id, summary["runId"])
        return summary

    def get_events(self, processing_id: str, run_id: str | None = None):
        if not PROCESSING_ID_RE.match(processing_id):
            return []
        rows = list(self._events.query_entities(query_filter="PartitionKey eq @pk", parameters={"pk": processing_id}))
        if run_id:
            rows = [r for r in rows if r.get("runId") == run_id]
        rows.sort(key=lambda r: r.get("RowKey") or "")
        return [event_entity_to_event(r) for r in rows]

    def get_audit(self, processing_id: str):
        found = self._latest(processing_id)
        if not found:
            return None
        summary, _ = found
        events = self.get_events(processing_id, summary["runId"])
        if not events:
            audit = self._read_json(summary.get("auditBlobUri")) or {}
            events = audit.get("events", [])
        return {
            "processingId": processing_id,
            "runId": summary.get("runId"),
            "correlationId": summary.get("correlationId"),
            "events": events,
        }

    def list_files(self, processing_id: str):
        found = self._latest(processing_id)
        if not found:
            return None
        summary, _ = found
        run_id = summary["runId"]
        files = []
        for container in ARTIFACT_CONTAINERS:
            client = self._blob.get_container_client(container)
            prefix = f"{processing_id}/"
            for blob in client.list_blobs(name_starts_with=prefix):
                parts = blob.name.split("/")
                # Only the latest run's analysis/results; other runs remain in Blob for audit.
                if container in ("analysis", "results") and len(parts) > 2 and parts[1] != run_id:
                    continue
                files.append(
                    {
                        "container": container,
                        "path": blob.name,
                        "name": parts[-1],
                        "size": blob.size,
                        "contentType": getattr(getattr(blob, "content_settings", None), "content_type", None),
                        "lastModified": blob.last_modified.isoformat() if getattr(blob, "last_modified", None) else None,
                    }
                )
        return {"processingId": processing_id, "runId": run_id, "files": files}

    def download(self, processing_id: str, container: str, path: str):
        if not artifact_allowed(container, path, processing_id):
            return None
        client = self._blob.get_blob_client(container, path)
        try:
            downloader = client.download_blob()
            data = downloader.readall()
            content_type = downloader.properties.content_settings.content_type
        except Exception:
            return None
        return PurePosixPath(path).name, data, content_type or "application/octet-stream"

    def get_csv(self, processing_id: str):
        found = self._latest(processing_id)
        if not found:
            return None
        run, _ = found
        if not run.get("csvBlobUri"):
            return None
        container, _, name = run["csvBlobUri"].partition("/")
        try:
            data = self._blob.get_blob_client(container, name).download_blob().readall()
        except Exception:
            return None
        return (run.get("csvFileName") or "remittance.csv", data)

    # --- summaries + live pipeline -------------------------------------------

    def daily_summary(self, date: str):
        if not DATE_RE.match(date):
            return aggregate([], scope={"type": "daily", "date": date})
        entities = self._query("processedDate eq @d", {"d": date})
        runs = [entity_to_summary(e) for e in latest_per_processing(entities)]
        return aggregate(runs, scope={"type": "daily", "date": date})

    def weekly_summary(self, week: str):
        if not WEEK_RE.match(week):
            return aggregate([], scope={"type": "weekly", "week": week})
        entities = self._query("processedWeek eq @w", {"w": week})
        runs = [entity_to_summary(e) for e in latest_per_processing(entities)]
        return aggregate(runs, scope={"type": "weekly", "week": week})

    def pipeline_status(self, window_minutes: int = 60):
        # Several dashboards poll this every few seconds; coalesce them onto one table scan.
        cached = self._pipeline_cache.get(window_minutes)
        if cached and time.monotonic() - cached[0] < PIPELINE_CACHE_SECONDS:
            return cached[1]
        snapshot = self._pipeline_status(window_minutes)
        self._pipeline_cache[window_minutes] = (time.monotonic(), snapshot)
        return snapshot

    def _pipeline_status(self, window_minutes: int):
        now = datetime.now(timezone.utc)
        clause, params = "updatedAtUtc ge @since", {"since": (now - timedelta(hours=24)).isoformat()}
        runs = [entity_to_summary(e) for e in latest_per_processing(self._query(clause, params))]
        snapshot = pipeline_snapshot(runs, now=now, window_minutes=window_minutes)
        queue = None
        if self._bus is not None:
            try:
                queue = self._bus.depth()
            except Exception as exc:  # metrics are informational; never fail the dashboard
                queue = {"error": type(exc).__name__}
        snapshot["queue"] = queue
        return snapshot

    # --- writes (source blob + queue message only) ---------------------------

    def _enqueue(self, *, processing_id, blob_name, source_name, sha256, size, stream, force=False, correlation_id=None):
        if self._bus is None:
            raise QueueUnavailable("SERVICE_BUS_FQDN is not configured")
        correlation_id = correlation_id or str(uuid.uuid4())
        submitted = datetime.now(timezone.utc).isoformat()
        message = {
            "schemaVersion": "1.0",
            "processingId": processing_id,
            "correlationId": correlation_id,
            "stream": stream,
            "sourceContainer": self._incoming,
            "sourceBlobName": blob_name,
            "sourceName": source_name,
            "sourceSha256": sha256,
            "sourceSizeBytes": size,
            "submittedAtUtc": submitted,
            "force": force,
        }
        # MessageId = processingId lets Service Bus duplicate detection drop an identical file sent
        # twice within the window; an explicit reprocess gets a unique id so it is never dropped.
        message_id = f"{processing_id}:{uuid.uuid4()}" if force else processing_id
        self._bus.send(json.dumps(message), message_id=message_id, correlation_id=correlation_id)
        return {"processingId": processing_id, "correlationId": correlation_id, "blobName": blob_name,
                "status": "Queued", "queuedAtUtc": submitted}

    def upload(self, filename: str, data: bytes, stream: str = "upload"):
        digest = hashlib.sha256(data).hexdigest()
        processing_id = "REM-" + digest[:20].upper()
        safe_name = _safe_name(filename)
        blob_name = f"{processing_id}/{safe_name}"
        self._blob.get_container_client(self._incoming).get_blob_client(blob_name).upload_blob(data, overwrite=True)
        return self._enqueue(
            processing_id=processing_id, blob_name=blob_name, source_name=safe_name,
            sha256=digest, size=len(data), stream=stream,
        )

    def reprocess(self, processing_id: str):
        found = self._latest(processing_id)
        if not found:
            return None
        run, _ = found
        container = self._blob.get_container_client(self._incoming)
        blob = next(iter(container.list_blobs(name_starts_with=f"{processing_id}/")), None)
        if blob is None:
            return None
        return self._enqueue(
            processing_id=processing_id, blob_name=blob.name, source_name=run.get("sourceName") or PurePosixPath(blob.name).name,
            sha256=run.get("sourceSha256"), size=blob.size, stream="reprocess", force=True,
        )

    def delete(self, processing_id: str):
        """Permanently remove a remittance: every artifact blob, then the ledger + event rows.

        Irreversible. Content is deleted before the index rows so a partial failure leaves a
        still-visible (retryable) run rather than orphaned blobs with no pointer. Returns a
        delete summary, or None if nothing existed for this id (so the API can 404).
        """
        if not PROCESSING_ID_RE.match(processing_id):
            return None

        runs = self._runs_for(processing_id)
        try:
            events = list(self._events.query_entities(query_filter="PartitionKey eq @pk", parameters={"pk": processing_id}))
        except Exception:
            events = []

        def _owned(name: str) -> bool:
            # Fixed-length ids can't prefix one another, but match exactly to be safe.
            return name == processing_id or name.startswith(f"{processing_id}/") or name.startswith(f"{processing_id}.")

        deleted_blobs = 0
        for container in ARTIFACT_CONTAINERS:
            client = self._blob.get_container_client(container)
            try:
                blobs = list(client.list_blobs(name_starts_with=processing_id))
            except Exception:
                continue
            for blob in blobs:
                if not _owned(blob.name):
                    continue
                try:
                    client.get_blob_client(blob.name).delete_blob()
                    deleted_blobs += 1
                except Exception:
                    logger.warning("blob delete failed container=%s name=%s", container, blob.name)

        deleted_rows = self._delete_entities(self._table, runs)
        deleted_events = self._delete_entities(self._events, events)

        if not runs and not events and not deleted_blobs:
            return None
        return {
            "processingId": processing_id,
            "deleted": {"runs": deleted_rows, "events": deleted_events, "blobs": deleted_blobs},
        }

    @staticmethod
    def _delete_entities(table, entities) -> int:
        deleted = 0
        for entity in entities:
            try:
                table.delete_entity(entity["PartitionKey"], entity["RowKey"])
                deleted += 1
            except Exception:
                logger.warning("entity delete failed pk=%s rk=%s", entity.get("PartitionKey"), entity.get("RowKey"))
        return deleted

    # --- analyst review ------------------------------------------------------

    def list_review_runs(self, review=None, status=None, payer=None, search=None, date_from=None, date_to=None, limit=50):
        """Runs for the review agent; review='open'/'completed' narrows to ReviewRequired runs."""
        if review in REVIEW_STATES:
            status = "ReviewRequired"
        runs = self.list_runs(status=status, date_from=date_from, date_to=date_to, payer=payer, search=search, limit=1000)
        rows = [review_run_row(run) for run in runs]
        if review in REVIEW_STATES:
            rows = [row for row in rows if row["reviewState"] == review]
        return rows[: max(1, min(int(limit), 200))]

    def get_review_case(self, processing_id: str):
        run = self.get_run(processing_id)
        return review_case(run) if run else None

    def _read_text(self, blob_uri: str | None) -> str:
        container, _, name = (blob_uri or "").partition("/")
        if not container or not name:
            return ""
        try:
            return self._blob.get_blob_client(container, name).download_blob().readall().decode("utf-8", errors="replace")
        except Exception:
            return ""

    def get_source_text(self, processing_id: str, source_id: str, offset: int = 0):
        """A page of the text of one source: the email body, or what was read from a document."""
        found = self._latest(processing_id)
        if not found:
            return None
        summary, _ = found
        manifest = self._read_json(summary.get("manifestBlobUri") or f"manifests/{processing_id}.json") or {}
        if source_id == BODY_SOURCE_ID:
            message = manifest.get("message") or {}
            header = "\n".join(
                f"{label}: {message.get(key)}"
                for label, key in (("Subject", "subject"), ("From", "senderName"), ("Sender email", "senderEmail"), ("Sent", "sentDate"))
                if message.get(key)
            )
            body = self._read_text(f"bodies/{processing_id}/message-body.txt")
            if not body and not header:
                return None
            return page_text(source_id, f"{header}\n\n{body}".strip(), offset)

        final = self._read_json(summary.get("finalResultBlobUri")) or {}
        item = next((c for c in final.get("candidateResults") or [] if c.get("candidateId") == source_id), None)
        candidate = next((c for c in manifest.get("candidates") or [] if c.get("candidateId") == source_id), None)
        if item is None and candidate is None:
            return None
        # Content Understanding's reading of a PDF/image/Office file, else the stored text payload
        # (embedded emails, text attachments), else the lines extraction found (spreadsheets).
        text = analysis_markdown(self._read_json((item or {}).get("rawAnalysisBlobUri")) or {})
        if not text and candidate and str(candidate.get("payloadContentType") or candidate.get("contentType") or "").startswith("text"):
            text = self._read_text(candidate.get("payloadBlobUri") or candidate.get("blobUri"))
        if not text:
            normalized = self._read_json((item or {}).get("normalizedExtractionBlobUri")) or {}
            lines = normalized.get("paymentLines") or []
            if lines:
                text = "No document text is stored for this source; the payment lines read from it are:\n" + "\n".join(
                    f"{line.get('proInvoiceNumber')}\t{line.get('amountPaid')}" for line in lines
                )
        return page_text(source_id, text, offset)

    def complete_review(self, processing_id: str, *, reviewer: str, outcome: str, notes: str, channel: str = "review-agent"):
        """Record an analyst's sign-off on the latest run. Raises ReviewConflict unless it is open."""
        if not PROCESSING_ID_RE.match(processing_id):
            return None
        entities = self._runs_for(processing_id)
        if not entities:
            return None
        entity = latest_per_processing(entities)[0]
        state = review_state(entity_to_summary(entity))
        if state is None:
            raise ReviewConflict(f"Run is {entity.get('status')}, not ReviewRequired.")
        if state == "completed":
            raise ReviewConflict(f"Review was already completed by {entity.get('reviewedBy')} at {entity.get('reviewedAtUtc')}.")

        from azure.core import MatchConditions
        from azure.core.exceptions import ResourceModifiedError
        from azure.data.tables import UpdateMode

        run_id = entity["RowKey"]
        now = datetime.now(timezone.utc).isoformat()
        review = {"reviewStatus": "Completed", "reviewedBy": reviewer, "reviewedAtUtc": now, "reviewOutcome": outcome,
                  "reviewNotes": notes, "reviewChannel": channel}
        if outcome == "approved":
            # The analyst confirmed the extracted lines, so release the downstream CSV the pipeline held back.
            # (handled_manually means the lines were wrong: a CSV of them would be too.)
            review.update(self._release_csv(processing_id, run_id, entity))
        # Merge only the review fields, If-Match on the row's etag, so a reprocess or a second reviewer
        # writing at the same moment is rejected instead of silently overwritten.
        conditions = {}
        etag = (getattr(entity, "metadata", None) or {}).get("etag")
        if etag:
            conditions = {"etag": etag, "match_condition": MatchConditions.IfNotModified}
        try:
            self._table.update_entity({"PartitionKey": processing_id, "RowKey": run_id, **review}, mode=UpdateMode.MERGE, **conditions)
        except ResourceModifiedError as exc:
            raise ReviewConflict("The run changed while the review was being recorded; reload it and try again.") from exc

        events = self.get_events(processing_id, run_id)
        seq = max((int(e.get("seq") or 0) for e in events), default=0) + 1
        details = {"reviewer": reviewer, "outcome": outcome, "notes": notes, "channel": channel,
                   "csvBlobUri": review.get("csvBlobUri")}
        try:
            self._events.upsert_entity({
                "PartitionKey": processing_id,
                "RowKey": f"{run_id}_{seq:04d}",
                "runId": run_id,
                "seq": seq,
                "event": "review_completed",
                "eventTimeUtc": now,
                "previousState": "ReviewRequired",
                "newState": "ReviewRequired",
                "stage": "ManualReview",
                "detailsJson": json.dumps(details, sort_keys=True)[:30000],
            })
        except Exception:  # the run row is the record; the timeline entry is best-effort
            logger.warning("review event write failed processingId=%s runId=%s", processing_id, run_id)
        return {"processingId": processing_id, "runId": run_id, **review}

    def _release_csv(self, processing_id: str, run_id: str, entity) -> dict:
        """Write the run's downstream CSV from its final result; ReviewConflict if the lines can't make one."""
        final = self._read_json(entity.get("finalResultBlobUri")) or {}
        if final.get("runId") not in (None, run_id):
            raise ReviewConflict("The run's final result belongs to another run; reload it and try again.")
        try:
            data = payment_csv(final.get("paymentLines") or [])
        except ValueError as exc:
            raise ReviewConflict(f"{exc} It can't be approved; use handled_manually or rejected.") from exc
        name = csv_file_name(final.get("remittanceMetadata") or {})
        from azure.storage.blob import ContentSettings

        self._blob.get_blob_client("results", f"{processing_id}/{run_id}/{name}").upload_blob(
            data, overwrite=True, content_settings=ContentSettings(content_type="text/csv; charset=utf-8"))
        return {"csvBlobUri": f"results/{processing_id}/{run_id}/{name}", "csvFileName": name}

    def replay_dead_letters(self, max_messages: int = 50) -> int:
        if self._bus is None:
            raise QueueUnavailable("SERVICE_BUS_FQDN is not configured")
        return self._bus.replay_dead_letters(max_messages)


@lru_cache
def get_repository() -> LedgerRepository:
    return LedgerRepository()
