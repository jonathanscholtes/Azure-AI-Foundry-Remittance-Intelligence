from __future__ import annotations

from functools import lru_cache

from azure.data.tables import TableClient
from azure.identity import DefaultAzureCredential
from azure.storage.blob import BlobServiceClient

from .settings import get_settings


@lru_cache
def _credential() -> DefaultAzureCredential:
    # AZURE_CLIENT_ID (set by the Container App) selects the user-assigned identity.
    return DefaultAzureCredential()


@lru_cache
def blob_service() -> BlobServiceClient:
    settings = get_settings()
    if not settings.blob_endpoint:
        raise RuntimeError("STORAGE_BLOB_ENDPOINT is not configured")
    return BlobServiceClient(account_url=settings.blob_endpoint, credential=_credential())


def _table(name: str) -> TableClient:
    settings = get_settings()
    if not settings.table_endpoint:
        raise RuntimeError("STORAGE_TABLE_ENDPOINT is not configured")
    return TableClient(endpoint=settings.table_endpoint, table_name=name, credential=_credential())


@lru_cache
def ledger_table() -> TableClient:
    return _table(get_settings().ledger_table)


@lru_cache
def events_table() -> TableClient:
    return _table(get_settings().events_table)


class IntakeBus:
    """Sends intake reference messages and reads queue depth (Entra auth, no SAS)."""

    def __init__(self, fqdn: str, queue: str):
        from azure.servicebus import ServiceBusClient
        from azure.servicebus.management import ServiceBusAdministrationClient

        self.queue = queue
        self._client = ServiceBusClient(fully_qualified_namespace=fqdn, credential=_credential())
        self._admin = ServiceBusAdministrationClient(fully_qualified_namespace=fqdn, credential=_credential())

    def send(self, body: str, *, message_id: str, correlation_id: str) -> None:
        from azure.servicebus import ServiceBusMessage

        with self._client.get_queue_sender(self.queue) as sender:
            sender.send_messages(
                ServiceBusMessage(
                    body,
                    message_id=message_id,
                    correlation_id=correlation_id,
                    content_type="application/json",
                    subject="remittance.received",
                )
            )

    def depth(self) -> dict:
        props = self._admin.get_queue_runtime_properties(self.queue)
        return {
            "active": props.active_message_count,
            "deadLetter": props.dead_letter_message_count,
            "scheduled": props.scheduled_message_count,
            "total": props.total_message_count,
        }

    def replay_dead_letters(self, max_messages: int = 50) -> int:
        from azure.servicebus import ServiceBusMessage, ServiceBusSubQueue

        replayed = 0
        with self._client.get_queue_receiver(self.queue, sub_queue=ServiceBusSubQueue.DEAD_LETTER) as receiver, \
                self._client.get_queue_sender(self.queue) as sender:
            for message in receiver.receive_messages(max_message_count=max_messages, max_wait_time=5):
                body = b"".join(bytes(part) for part in message.body)
                sender.send_messages(
                    ServiceBusMessage(
                        body,
                        message_id=f"{message.message_id}:replay",
                        correlation_id=message.correlation_id,
                        content_type="application/json",
                        subject="remittance.replayed",
                    )
                )
                receiver.complete_message(message)
                replayed += 1
        return replayed


@lru_cache
def intake_bus() -> IntakeBus | None:
    settings = get_settings()
    if not settings.service_bus_fqdn:
        return None
    return IntakeBus(settings.service_bus_fqdn, settings.intake_queue)
