"""ACH remittance processor - Service Bus intake + Durable Functions orchestration.

    UI/API upload | email adapter | load generator
        -> Blob `incoming/{processingId}/{name}` (content plane)
        -> Service Bus `remittance-intake` (reference message only)
        -> intake_starter (this file) -> remittance_orchestrator (orchestration/orchestrator.py)
        -> activities (orchestration/activities.py) + Recorder (shared/recorder.py)

The starter completes the Service Bus message as soon as Durable has accepted the work, so no
message lock is held while Content Understanding or an agent runs. A malformed message raises and
is retried by Service Bus, then dead-lettered after maxDeliveryCount (replayable from the DLQ).
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timezone
from functools import lru_cache

import azure.durable_functions as df
import azure.functions as func

from orchestration import activities
from orchestration.orchestrator import orchestrate
from shared.ledger import events_table_client, ledger_table_client
from shared.recorder import record as record_state
from shared.storage import blob_service_client

app = df.DFApp()
logger = logging.getLogger("remittance_intake")

REQUIRED_MESSAGE_FIELDS = ("processingId", "sourceBlobName")
IN_FLIGHT = {"Running", "Pending", "ContinuedAsNew"}


@lru_cache
def _blob():
    return blob_service_client()


@lru_cache
def _ledger():
    return ledger_table_client()


@lru_cache
def _events():
    return events_table_client()


def parse_intake_message(body: bytes, *, correlation_id: str | None = None, delivery_count: int | None = None) -> dict:
    """Validate the intake contract (schemas/intake-message-v1.json). Raises ValueError -> DLQ."""
    message = json.loads(body)
    if not isinstance(message, dict):
        raise ValueError("intake message must be a JSON object")
    missing = [field for field in REQUIRED_MESSAGE_FIELDS if not message.get(field)]
    if missing:
        raise ValueError(f"intake message missing {missing}")
    run_id = str(uuid.uuid4())
    return {
        **message,
        "runId": run_id,
        "correlationId": message.get("correlationId") or correlation_id or run_id,
        "stream": message.get("stream") or "upload",
        "submittedAtUtc": message.get("submittedAtUtc") or datetime.now(timezone.utc).isoformat(),
        "deliveryCount": delivery_count,
    }


@app.service_bus_queue_trigger(
    arg_name="msg",
    queue_name="%INTAKE_QUEUE_NAME%",
    connection="ServiceBusConnection",
)
@app.durable_client_input(client_name="client")
async def intake_starter(msg: func.ServiceBusMessage, client: df.DurableOrchestrationClient) -> None:
    message = parse_intake_message(
        msg.get_body(),
        correlation_id=msg.correlation_id,
        delivery_count=msg.delivery_count,
    )
    instance_id = message["processingId"]
    existing = await client.get_status(instance_id)
    runtime_status = getattr(getattr(existing, "runtime_status", None), "value", None) or getattr(existing, "runtime_status", None)
    if runtime_status and str(runtime_status) in IN_FLIGHT:
        # The same content is already being processed (duplicate arrival across streams).
        logger.info("intake_duplicate_in_flight processingId=%s", instance_id)
        return
    await client.start_new("remittance_orchestrator", instance_id, message)
    logger.info(
        "intake_scheduled processingId=%s runId=%s stream=%s",
        instance_id,
        message["runId"],
        message["stream"],
    )


@app.orchestration_trigger(context_name="context")
def remittance_orchestrator(context: df.DurableOrchestrationContext):
    result = yield from orchestrate(context, df)
    return result


@app.activity_trigger(input_name="payload")
def record(payload: dict) -> dict:
    return record_state(payload, ledger_client=_ledger(), events_client=_events(), blob_service=_blob())


@app.activity_trigger(input_name="payload")
def intake_parse(payload: dict) -> dict:
    return activities.intake_parse(payload, blob_service=_blob())


@app.activity_trigger(input_name="payload")
def extract_candidate(payload: dict) -> dict:
    return activities.extract_candidate(payload, blob_service=_blob())


@app.activity_trigger(input_name="payload")
def validate_reconcile(payload: dict) -> dict:
    return activities.validate_reconcile(payload, blob_service=_blob())


@app.activity_trigger(input_name="payload")
def resolve_extraction(payload: dict) -> dict:
    return activities.resolve_extraction(payload, blob_service=_blob())


@app.activity_trigger(input_name="payload")
def run_assurance_expert(payload: dict) -> dict:
    return activities.run_assurance_expert(payload)


@app.activity_trigger(input_name="payload")
def publish_results(payload: dict) -> dict:
    return activities.publish_results(payload, blob_service=_blob())
