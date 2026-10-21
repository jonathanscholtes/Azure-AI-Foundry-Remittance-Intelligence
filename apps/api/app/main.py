from __future__ import annotations

from datetime import datetime, timezone

from fastapi import Depends, FastAPI, File, HTTPException, Query, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response

from .repository import SUPPORTED_EXTENSIONS, LedgerRepository, QueueUnavailable, get_repository
from .settings import get_settings

app = FastAPI(title="ACH Remittance Intelligence API", version="2.0.0")

settings = get_settings()
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.allowed_origins,
    allow_methods=["GET", "POST", "DELETE"],
    allow_headers=["*"],
)

# Artifacts the browser may render inline; everything else (notably HTML email bodies) downloads.
INLINE_TYPES = ("application/pdf", "image/png", "image/jpeg", "image/gif", "text/plain", "text/csv", "application/json")


def repo() -> LedgerRepository:
    return get_repository()


@app.get("/api/health")
def health():
    return {"status": "ok", "timeUtc": datetime.now(timezone.utc).isoformat()}


INTAKE_STREAMS = {"upload", "email", "loadtest"}


@app.post("/api/remittances", status_code=202)
async def upload_remittance(
    file: UploadFile = File(...),
    stream: str = Query(default="upload"),
    repository: LedgerRepository = Depends(repo),
):
    if stream not in INTAKE_STREAMS:
        raise HTTPException(status_code=400, detail=f"stream must be one of {sorted(INTAKE_STREAMS)}")
    name = (file.filename or "").strip()
    if not name.lower().endswith(SUPPORTED_EXTENSIONS):
        raise HTTPException(status_code=400, detail="Only Outlook .msg (or MIME .eml) files are supported.")
    data = await file.read()
    limit = settings.max_upload_mb * 1024 * 1024
    if len(data) == 0:
        raise HTTPException(status_code=400, detail="Uploaded file is empty.")
    if len(data) > limit:
        raise HTTPException(status_code=413, detail=f"File exceeds the {settings.max_upload_mb} MB limit.")
    try:
        return repository.upload(name, data, stream=stream)
    except QueueUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    except Exception as exc:  # pragma: no cover - surface storage/auth errors cleanly
        raise HTTPException(status_code=502, detail=f"Submitting the remittance failed: {type(exc).__name__}")


@app.get("/api/remittances")
def list_remittances(
    status: str | None = Query(default=None),
    payer: str | None = Query(default=None),
    q: str | None = Query(default=None),
    date_from: str | None = Query(default=None, alias="from"),
    date_to: str | None = Query(default=None, alias="to"),
    limit: int = Query(default=200, ge=1, le=1000),
    repository: LedgerRepository = Depends(repo),
):
    items = repository.list_runs(status=status, date_from=date_from, date_to=date_to, payer=payer, search=q, limit=limit)
    return {"items": items}


@app.get("/api/remittances/{processing_id}")
def get_remittance(processing_id: str, repository: LedgerRepository = Depends(repo)):
    run = repository.get_run(processing_id)
    if not run:
        raise HTTPException(status_code=404, detail="Remittance not found.")
    return run


@app.delete("/api/remittances/{processing_id}")
def delete_remittance(processing_id: str, repository: LedgerRepository = Depends(repo)):
    """Permanently delete a remittance and all its stored content (blobs + ledger/event rows)."""
    result = repository.delete(processing_id)
    if not result:
        raise HTTPException(status_code=404, detail="Remittance not found.")
    return result


@app.get("/api/remittances/{processing_id}/events")
def get_remittance_events(processing_id: str, run_id: str | None = Query(default=None, alias="runId"), repository: LedgerRepository = Depends(repo)):
    return {"processingId": processing_id, "events": repository.get_events(processing_id, run_id)}


@app.get("/api/remittances/{processing_id}/audit")
def get_remittance_audit(processing_id: str, repository: LedgerRepository = Depends(repo)):
    audit = repository.get_audit(processing_id)
    if not audit:
        raise HTTPException(status_code=404, detail="Remittance not found.")
    return audit


@app.get("/api/remittances/{processing_id}/files")
def list_remittance_files(processing_id: str, repository: LedgerRepository = Depends(repo)):
    files = repository.list_files(processing_id)
    if files is None:
        raise HTTPException(status_code=404, detail="Remittance not found.")
    return files


@app.get("/api/remittances/{processing_id}/files/{container}/{path:path}")
def download_remittance_file(processing_id: str, container: str, path: str, repository: LedgerRepository = Depends(repo)):
    result = repository.download(processing_id, container, path)
    if not result:
        raise HTTPException(status_code=404, detail="File not found.")
    filename, data, content_type = result
    base_type = content_type.split(";")[0].strip().lower()
    disposition = "inline" if base_type in INLINE_TYPES else "attachment"
    return Response(
        content=data,
        media_type=content_type if disposition == "inline" else "application/octet-stream",
        headers={
            "Content-Disposition": f'{disposition}; filename="{filename}"',
            "X-Content-Type-Options": "nosniff",
            "Content-Security-Policy": "sandbox",
        },
    )


@app.get("/api/remittances/{processing_id}/csv")
def get_remittance_csv(processing_id: str, repository: LedgerRepository = Depends(repo)):
    result = repository.get_csv(processing_id)
    if not result:
        raise HTTPException(status_code=404, detail="No CSV available for this remittance.")
    filename, data = result
    return Response(
        content=data,
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.post("/api/remittances/{processing_id}/reprocess", status_code=202)
def reprocess_remittance(processing_id: str, repository: LedgerRepository = Depends(repo)):
    try:
        result = repository.reprocess(processing_id)
    except QueueUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    if not result:
        raise HTTPException(status_code=404, detail="Source message not found for this remittance.")
    return result


@app.get("/api/pipeline/status")
def pipeline_status(window: int = Query(default=60, ge=15, le=1440), repository: LedgerRepository = Depends(repo)):
    return repository.pipeline_status(window_minutes=window)


@app.post("/api/pipeline/dead-letter/replay")
def replay_dead_letters(max_messages: int = Query(default=50, ge=1, le=500), repository: LedgerRepository = Depends(repo)):
    try:
        return {"replayed": repository.replay_dead_letters(max_messages)}
    except QueueUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc))


@app.get("/api/summaries/daily")
def daily_summary(date: str | None = Query(default=None), repository: LedgerRepository = Depends(repo)):
    day = date or datetime.now(timezone.utc).date().isoformat()
    return repository.daily_summary(day)


@app.get("/api/summaries/weekly")
def weekly_summary(week: str | None = Query(default=None), repository: LedgerRepository = Depends(repo)):
    if not week:
        now = datetime.now(timezone.utc)
        iso_year, iso_week, _ = now.isocalendar()
        week = f"{iso_year}-W{iso_week:02d}"
    return repository.weekly_summary(week)
