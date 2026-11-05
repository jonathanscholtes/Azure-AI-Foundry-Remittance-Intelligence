from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Literal

from fastapi import APIRouter, Depends, FastAPI, File, HTTPException, Path, Query, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response
from pydantic import BaseModel, Field, ValidationError

from .repository import SUPPORTED_EXTENSIONS, LedgerRepository, QueueUnavailable, ReviewConflict, get_repository
from .settings import get_settings
from .transforms import REVIEW_OUTCOMES

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


# --- Analyst review (the ACH Review Expert agent, via the APIM MCP server) ----------------------
# APIM declares these routes as the ACH Review API (infra/apim/review-mcp.bicep) and exports them as
# the review MCP server; each operation_id is the name of an MCP tool the agent calls.

SOURCE_ID_RE = re.compile(r"^[a-z0-9-]{1,40}$")

review = APIRouter(prefix="/api/review", tags=["review"])


class CompleteReview(BaseModel):
    reviewer: str = Field(min_length=2, max_length=120, description="Name or email of the analyst signing off the review.")
    outcome: Literal[REVIEW_OUTCOMES] = Field(
        description="approved: the extracted payment lines are correct (header details such as the payer may have been "
                    "confirmed); releases the CSV. handled_manually: lines are wrong, cash applied manually outside this "
                    "system; no CSV. rejected: no cash applied (not a remittance, duplicate, or sent back to the payer); "
                    "no CSV.")
    notes: str = Field(min_length=3, max_length=2000, description="What the analyst decided and why, for the audit trail.")


@review.get("/runs", operation_id="list_runs", summary="List remittance runs (open/completed reviews, in-flight, failed)")
def review_list_runs(
    review_state: Literal["open", "completed"] | None = Query(default=None, alias="review",
                                                              description="open = awaiting manual review; completed = signed off"),
    status: str | None = Query(default=None, description="Run status: InFlight, Completed, ReviewRequired, Failed or DuplicateSkipped"),
    payer: str | None = Query(default=None, description="Exact payer name"),
    q: str | None = Query(default=None, description="Text in payer, file name, subject, processing id or payment reference"),
    date_from: str | None = Query(default=None, alias="from", description="Processed on or after (YYYY-MM-DD)"),
    date_to: str | None = Query(default=None, alias="to", description="Processed on or before (YYYY-MM-DD)"),
    limit: int = Query(default=25, ge=1, le=200),
    repository: LedgerRepository = Depends(repo),
):
    items = repository.list_review_runs(review=review_state, status=status, payer=payer, search=q,
                                        date_from=date_from, date_to=date_to, limit=limit)
    return {"count": len(items), "items": items}


@review.get("/runs/{processing_id}", operation_id="get_run", summary="Get one run's review case")
def review_get_run(processing_id: str = Path(description="Processing id, e.g. REM-0123456789ABCDEF0123"),
                   repository: LedgerRepository = Depends(repo)):
    case = repository.get_review_case(processing_id)
    if not case:
        raise HTTPException(status_code=404, detail="Remittance not found.")
    return case


@review.get("/runs/{processing_id}/sources/{source_id}/text", operation_id="get_source_text",
            summary="Read the original email or a document's text, one page at a time")
def review_get_source_text(
    processing_id: str = Path(description="Processing id"),
    source_id: str = Path(description="Source id from the case's sources list: message-body or doc-NNN"),
    offset: int = Query(default=0, ge=0, description="Character offset; use nextOffset from the previous page"),
    repository: LedgerRepository = Depends(repo),
):
    if not SOURCE_ID_RE.match(source_id):
        raise HTTPException(status_code=400, detail="Invalid source id.")
    page = repository.get_source_text(processing_id, source_id, offset)
    if page is None:
        raise HTTPException(status_code=404, detail="Source not found for this remittance.")
    return page


@review.post("/runs/{processing_id}/complete", operation_id="complete_review",
             summary="Mark a run's manual review as completed")
def review_complete(body: CompleteReview | str, processing_id: str = Path(description="Processing id"),
                    repository: LedgerRepository = Depends(repo)):
    # APIM's MCP export types the request body as a string, so the tool call arrives as a JSON string
    # holding the object ("{\"reviewer\": ...}"); accept that form too, with the same validation.
    if isinstance(body, str):
        try:
            body = CompleteReview.model_validate_json(body)
        except ValidationError as exc:
            raise RequestValidationError(exc.errors(include_url=False))
    try:
        result = repository.complete_review(processing_id, reviewer=body.reviewer.strip(), outcome=body.outcome,
                                            notes=body.notes.strip())
    except ReviewConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    if not result:
        raise HTTPException(status_code=404, detail="Remittance not found.")
    return result


@review.get("/pipeline", operation_id="get_pipeline_status", summary="What is processing right now")
def review_pipeline(window: int = Query(default=60, ge=15, le=1440, description="Look-back window in minutes"),
                    repository: LedgerRepository = Depends(repo)):
    return repository.pipeline_status(window_minutes=window)


app.include_router(review)
