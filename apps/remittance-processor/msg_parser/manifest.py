from __future__ import annotations

from datetime import datetime, timezone

from shared.hashing import sha256_hex


def create_source_manifest(
    *,
    source_name: str,
    source_content: bytes,
    source_blob_uri: str,
    message: dict,
    attachments: list[dict],
    candidates: dict,
    run_id: str | None = None,
    correlation_id: str | None = None,
) -> dict:
    source_hash = sha256_hex(source_content)
    processing_id = f"REM-{source_hash[:20].upper()}"
    is_eml = source_name.lower().endswith(".eml")
    manifest = {
        "schemaVersion": "1.0",
        "processingId": processing_id,
        "createdAt": datetime.now(timezone.utc).isoformat(),
        "source": {
            "name": source_name,
            "sourceType": "eml" if is_eml else "msg",
            "contentType": "message/rfc822" if is_eml else "application/vnd.ms-outlook",
            "size": len(source_content),
            "sha256": source_hash,
            "blobUri": source_blob_uri,
        },
        "message": message,
        "candidates": candidates["candidates"],
        "recommendedProcessing": candidates["recommendedProcessing"],
        "attachments": attachments,
    }
    if run_id:
        manifest["runId"] = run_id
    if correlation_id:
        manifest["correlationId"] = correlation_id
    return manifest