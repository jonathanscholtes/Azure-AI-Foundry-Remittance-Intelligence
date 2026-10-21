from __future__ import annotations

import os
from functools import lru_cache


class Settings:
    """Runtime configuration, read from environment (set by the Container App)."""

    def __init__(self) -> None:
        self.blob_endpoint = os.environ.get("STORAGE_BLOB_ENDPOINT", "")
        self.table_endpoint = os.environ.get("STORAGE_TABLE_ENDPOINT", "")
        self.ledger_table = os.environ.get("LEDGER_TABLE_NAME", "remittances")
        self.events_table = os.environ.get("EVENTS_TABLE_NAME", "remittanceevents")
        self.incoming_container = os.environ.get("INCOMING_CONTAINER", "incoming")
        # Fully qualified Service Bus namespace (<name>.servicebus.windows.net) + intake queue.
        self.service_bus_fqdn = os.environ.get("SERVICE_BUS_FQDN", "")
        self.intake_queue = os.environ.get("INTAKE_QUEUE_NAME", "remittance-intake")
        self.max_upload_mb = int(os.environ.get("MAX_MSG_SIZE_MB", "25"))
        # How far back list/pipeline queries look by default (keeps table scans bounded).
        self.default_window_days = int(os.environ.get("DEFAULT_WINDOW_DAYS", "14"))
        # Comma-separated list of allowed CORS origins; "*" allows all (demo default).
        raw_origins = os.environ.get("ALLOWED_ORIGINS", "*")
        self.allowed_origins = [o.strip() for o in raw_origins.split(",") if o.strip()] or ["*"]


@lru_cache
def get_settings() -> Settings:
    return Settings()
