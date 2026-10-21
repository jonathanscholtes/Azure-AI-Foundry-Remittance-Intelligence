"""Blob content-plane helpers shared by the Durable activities.

Blob URIs inside the pipeline are always ``"<container>/<blob name>"`` references (never SAS URLs).
"""

from __future__ import annotations

import json
import os

from azure.identity import DefaultAzureCredential
from azure.storage.blob import BlobServiceClient, ContentSettings


def blob_service_client() -> BlobServiceClient:
    connection_string = os.environ.get("STORAGE_CONNECTION_STRING")
    if connection_string:
        return BlobServiceClient.from_connection_string(connection_string)

    endpoint = os.environ.get("STORAGE_BLOB_ENDPOINT")
    if not endpoint:
        raise RuntimeError("Set STORAGE_CONNECTION_STRING or STORAGE_BLOB_ENDPOINT")
    return BlobServiceClient(account_url=endpoint, credential=DefaultAzureCredential())


def upload_bytes(service, container: str, blob_name: str, data: bytes, content_type: str) -> str:
    service.get_blob_client(container, blob_name).upload_blob(
        data,
        overwrite=True,
        content_settings=ContentSettings(content_type=content_type),
    )
    return f"{container}/{blob_name}"


def upload_json(service, container: str, blob_name: str, value) -> str:
    return upload_bytes(
        service,
        container,
        blob_name,
        json.dumps(value, ensure_ascii=True, indent=2, default=str).encode("utf-8"),
        "application/json",
    )


def split_uri(blob_uri: str) -> tuple[str, str]:
    container, _, name = (blob_uri or "").partition("/")
    if not container or not name:
        raise ValueError(f"Invalid blob reference: {blob_uri!r}")
    return container, name


def read_bytes(service, blob_uri: str) -> bytes:
    container, name = split_uri(blob_uri)
    return service.get_blob_client(container, name).download_blob().readall()


def read_json_if_exists(service, container: str, blob_name: str):
    client = service.get_blob_client(container, blob_name)
    if not client.exists():
        return None
    try:
        return json.loads(client.download_blob().readall())
    except Exception:
        return {}
