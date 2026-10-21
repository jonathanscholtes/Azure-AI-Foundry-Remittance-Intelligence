"""Durable activity implementations (plain functions, unit-testable without the Functions host).

Activities do all I/O: Blob reads/writes, Content Understanding, Foundry agents. They pass only
blob references and compact JSON between each other - never document bytes - and every write is
idempotent (overwrite=True on deterministic paths) so a Durable retry is always safe.

The business logic is the existing Phase 1 code (msg_parser, extraction, validation) unchanged.
"""

from __future__ import annotations

import functools
import logging
import os
import re
import time
from decimal import Decimal, InvalidOperation
from pathlib import PurePosixPath

from extraction.candidate_router import route_candidates
from extraction.content_understanding import analyze_binary_candidate
from classification import intake_decision
from extraction.html_tables import extract_html_payment_tables
from extraction.text_tables import extract_text_payment_tables
from msg_parser.candidate_selector import select_candidates
from msg_parser.eml_parser import parse_message
from msg_parser.manifest import create_source_manifest
from msg_parser.text_normalizer import html_to_plain_text
from shared import usage
from shared.hashing import sha256_hex
from shared.storage import read_bytes, read_json_if_exists, upload_bytes, upload_json
from validation.remittance_validator import (
    create_output_filename,
    create_payment_csv,
    date_from_subject,
    split_warnings,
    validate_and_merge,
)

logger = logging.getLogger("remittance_intake")

SUPPORTED_EXTENSIONS = (".msg", ".eml")
REVIEW_CODES = {
    "attachment_parse_warning", "external_link_not_fetched", "unclassified_attachment", "multiple_payments_in_email",
    "remittance_inquiry_thread", "no_remittance_in_email",
}
# Decision-1 thread kinds that put a run in review (single_remittance / cannot_tell change nothing).
THREAD_WARNINGS = {
    "multiple_payments": "multiple_payments_in_email",
    "dispute_or_inquiry": "remittance_inquiry_thread",
    "no_remittance": "no_remittance_in_email",
}
THROTTLE_STATUS_CODES = {408, 429, 500, 502, 503, 504}


def processing_id_for(content: bytes) -> str:
    return f"REM-{sha256_hex(content)[:20].upper()}"


def _elapsed_ms(started: float) -> int:
    return int((time.perf_counter() - started) * 1000)


def _with_usage(activity):
    """Attach the AI usage the activity recorded (CU, agents, Decision-1) to its result as ``usage``;
    the orchestrator adds the activities' blocks into the run's usage. Additive - nothing reads it
    to decide anything."""

    @functools.wraps(activity)
    def wrapper(*args, **kwargs):
        with usage.collect() as block:
            result = activity(*args, **kwargs)
        if isinstance(result, dict) and block:
            result = {**result, "usage": block}
        return result

    return wrapper


def _max_size_mb() -> int:
    try:
        return max(1, int(os.environ.get("MAX_MSG_SIZE_MB", "25")))
    except ValueError:
        return 25


def _reconciliation_tolerance() -> Decimal:
    try:
        tolerance = Decimal(os.environ.get("RECONCILIATION_TOLERANCE", "0.01"))
    except (InvalidOperation, ValueError):
        return Decimal("0.01")
    return tolerance if tolerance >= 0 else Decimal("0.01")


_URL_RE = re.compile(r"https?://[^\s\"'<>]+", re.IGNORECASE)
_REMIT_LINK_RE = re.compile(
    r"(?:remittance|payment|invoice|statement)\s+detail"
    r"|\bportal\b"
    r"|(?:view|download|access|retrieve|see|click)\b[^.\n]{0,40}?(?:remit|payment|invoice|statement|deduction)",
    re.IGNORECASE,
)


def _external_link_count(parsed) -> int:
    """Count only links that plausibly hide remittance detail - an action phrase ("view your payment..."),
    a portal, or "remittance detail" near the link. Signature/marketing links (websites, catalogs, social,
    logos) are ignored so they are not flagged as un-fetched remittance detail."""
    content = parsed.body or parsed.html_body or ""
    count = 0
    for match in _URL_RE.finditer(content):
        window = content[max(0, match.start() - 70) : match.end() + 70]
        if _REMIT_LINK_RE.search(window):
            count += 1
    return count


_FREE_EMAIL_DOMAINS = {
    "gmail.com", "outlook.com", "hotmail.com", "yahoo.com", "aol.com",
    "icloud.com", "live.com", "msn.com", "proton.me", "protonmail.com",
}
_COMPOUND_TLD_SLDS = {"co", "com", "org", "net", "gov", "edu", "ac"}


def _org_from_email(email: str | None) -> str | None:
    """Derive the sender's organization from its email domain (tailspin.com -> "Tailspin").

    Returns None for free/personal mail providers, where the domain is not the paying organization.
    """
    if not email or "@" not in email:
        return None
    domain = email.rsplit("@", 1)[1].strip().lower()
    if not domain or domain in _FREE_EMAIL_DOMAINS:
        return None
    labels = [label for label in domain.split(".") if label]
    if len(labels) >= 3 and labels[-2] in _COMPOUND_TLD_SLDS and len(labels[-1]) == 2:
        second_level = labels[-3]  # e.g. acme.co.uk -> acme
    elif len(labels) >= 2:
        second_level = labels[-2]
    else:
        second_level = labels[0]
    second_level = second_level.replace("-", " ").strip()
    return second_level.title() or None


def _message_block(parsed) -> dict:
    return {
        "subject": parsed.subject,
        "senderName": parsed.sender_name,
        "senderEmail": parsed.sender_email,
        "sentDate": parsed.sent_date,
        "messageId": parsed.message_id,
        "inReplyTo": parsed.in_reply_to,
        "references": parsed.references,
        "hasBody": bool(parsed.body or parsed.html_body),
        "hasAttachments": bool(parsed.attachments),
    }


# --- 1. intake + parse ---------------------------------------------------------------------------


def intake_parse(payload: dict, *, blob_service, parse=parse_message) -> dict:
    """Load the source message, verify integrity, decompose it and persist every part."""
    started = time.perf_counter()
    processing_id = payload["processingId"]
    source_blob_uri = f"{payload.get('sourceContainer') or 'incoming'}/{payload['sourceBlobName']}"
    source_name = PurePosixPath(payload.get("sourceName") or payload["sourceBlobName"]).name

    if not source_name.lower().endswith(SUPPORTED_EXTENSIONS):
        return {"outcome": "rejected", "reason": "unsupported_extension", "sourceName": source_name}

    source_content = read_bytes(blob_service, source_blob_uri)
    limit_mb = _max_size_mb()
    if len(source_content) > limit_mb * 1024 * 1024:
        return {
            "outcome": "rejected",
            "reason": "message_too_large",
            "sourceName": source_name,
            "sourceSizeBytes": len(source_content),
            "limitMb": limit_mb,
        }

    source_hash = sha256_hex(source_content)
    if processing_id_for(source_content) != processing_id:
        # The processingId is the content hash; a mismatch means the blob changed after enqueue.
        return {"outcome": "rejected", "reason": "source_hash_mismatch", "sourceName": source_name}

    if not payload.get("force"):
        existing = read_json_if_exists(blob_service, "results", f"{processing_id}/final-remittance.json")
        if existing and existing.get("status") == "Extracted":
            return {
                "outcome": "duplicate",
                "sourceName": source_name,
                "sourceSha256": source_hash,
                "priorRunId": existing.get("runId"),
            }

    parse_started = time.perf_counter()
    parsed = parse(source_content, source_name)
    parse_ms = _elapsed_ms(parse_started)

    attachment_records: list[dict] = []
    for index, attachment in enumerate(parsed.attachments, start=1):
        candidate_id = f"doc-{index:03d}"
        blob_uri = upload_bytes(
            blob_service,
            "attachments",
            f"{processing_id}/{candidate_id}/{attachment.name}",
            attachment.content,
            attachment.content_type,
        )
        attachment_records.append(
            {
                "candidateId": candidate_id,
                "sourceType": attachment.source_type,
                "embeddedIn": attachment.embedded_in,
                "detectedType": attachment.detected_type,
                "name": attachment.name,
                "extension": attachment.extension,
                "contentType": attachment.content_type,
                "size": len(attachment.content),
                "sha256": sha256_hex(attachment.content),
                "isInline": attachment.is_inline,
                "blobUri": blob_uri,
                "payloadBlobUri": blob_uri,
                "payloadContentType": attachment.content_type,
            }
        )

    normalized_body = parsed.body or html_to_plain_text(parsed.html_body)
    selection = select_candidates(
        [dict(record) for record in attachment_records],
        subject=parsed.subject,
        body=normalized_body,
    )

    body_blob_uri = None
    html_blob_uri = None
    if normalized_body:
        body_blob_uri = upload_bytes(
            blob_service,
            "bodies",
            f"{processing_id}/message-body.txt",
            normalized_body.encode("utf-8"),
            "text/plain; charset=utf-8",
        )
    if parsed.html_body is not None:
        html_blob_uri = upload_bytes(
            blob_service,
            "bodies",
            f"{processing_id}/message-body.html",
            parsed.html_body.encode("utf-8"),
            "text/html; charset=utf-8",
        )
    # Do NOT feed the sender into Content Understanding: it makes CU latch onto the sender's display
    # name/mailbox ("FWS Accounting", "AP_Riverside") instead of the real company name present in
    # the body ("Fabrikam Wine & Spirits"). The sender is used only as a validation fallback (via
    # the email domain) when extraction returns nothing or the payee - see validate_reconcile.
    candidate_body_text = f"Subject: {parsed.subject or ''}\n\n{normalized_body or ''}".strip()
    for candidate in selection["candidates"]:
        if candidate.get("sourceType") != "message_body" or not candidate_body_text:
            continue
        body_bytes = candidate_body_text.encode("utf-8")
        candidate_uri = upload_bytes(
            blob_service,
            "candidates",
            f"{processing_id}/message-content.txt",
            body_bytes,
            "text/plain; charset=utf-8",
        )
        candidate.update(
            {
                "blobUri": body_blob_uri or candidate_uri,
                "plainTextBlobUri": body_blob_uri,
                "htmlBlobUri": html_blob_uri,
                "subject": parsed.subject,
                "analysisBlobUri": candidate_uri,
                "payloadBlobUri": candidate_uri,
                "payloadContentType": "text/plain",
                "sha256": sha256_hex(body_bytes),
            }
        )

    external_link_count = _external_link_count(parsed)
    unclassified_count = sum(
        candidate.get("classification") == "unclassified_candidate" for candidate in selection["candidates"]
    )
    manual_review_items = []
    if parsed.warnings:
        manual_review_items.append({"code": "attachment_parse_warning", "count": len(parsed.warnings)})
    if external_link_count:
        manual_review_items.append({"code": "external_link_not_fetched", "count": external_link_count})
    if unclassified_count:
        manual_review_items.append({"code": "unclassified_attachment", "count": unclassified_count})

    message = {**_message_block(parsed), "plainTextBlobUri": body_blob_uri, "htmlBlobUri": html_blob_uri}
    manifest = create_source_manifest(
        source_name=source_name,
        source_content=source_content,
        source_blob_uri=source_blob_uri,
        message=message,
        attachments=attachment_records,
        candidates=selection,
        run_id=payload.get("runId"),
        correlation_id=payload.get("correlationId"),
    )

    return {
        "outcome": "parsed",
        "sourceName": source_name,
        "sourceBlobUri": source_blob_uri,
        "sourceSha256": source_hash,
        "sourceSizeBytes": len(source_content),
        "message": {k: message[k] for k in ("subject", "senderName", "senderEmail", "sentDate", "hasBody", "hasAttachments")},
        "attachmentCount": len(attachment_records),
        "candidates": selection["candidates"],
        "recommendedProcessing": selection["recommendedProcessing"],
        "manifest": manifest,
        "manualReviewItems": manual_review_items,
        "parserWarningCount": len(parsed.warnings),
        "externalLinkCount": external_link_count,
        "unclassifiedCount": unclassified_count,
        "parseDurationMs": parse_ms,
        "durationMs": _elapsed_ms(started),
    }


# --- 2. extract (one activity per candidate, fanned out) ------------------------------------------


def _status_code(exc: Exception):
    for attr in ("status_code", "status"):
        value = getattr(exc, attr, None)
        if isinstance(value, int):
            return value
    response = getattr(exc, "response", None)
    return getattr(response, "status_code", None)


def _retry_after_seconds(exc: Exception, attempt: int) -> float:
    response = getattr(exc, "response", None)
    headers = getattr(response, "headers", None) or {}
    try:
        value = float(headers.get("Retry-After") or headers.get("retry-after"))
        return min(max(value, 1.0), 60.0)
    except (TypeError, ValueError):
        return min(2.0 * (2 ** attempt), 60.0)


def throttled_analyze(analyze=analyze_binary_candidate, *, max_attempts: int = 5, sleep=time.sleep, stats=None):
    """Wrap a Content Understanding call with bounded backoff on 429/5xx (honours Retry-After)."""

    def call(**kwargs):
        for attempt in range(max_attempts):
            try:
                return analyze(**kwargs)
            except Exception as exc:
                code = _status_code(exc)
                if code not in THROTTLE_STATUS_CODES or attempt == max_attempts - 1:
                    raise
                if stats is not None:
                    stats["throttleRetries"] = stats.get("throttleRetries", 0) + 1
                    if code == 429:
                        stats["throttled429"] = stats.get("throttled429", 0) + 1
                sleep(_retry_after_seconds(exc, attempt))

    return call


@_with_usage
def extract_candidate(payload: dict, *, blob_service, analyze=analyze_binary_candidate, sleep=time.sleep) -> dict:
    started = time.perf_counter()
    processing_id = payload["processingId"]
    run_id = payload["runId"]
    candidate = payload["candidate"]
    candidate_id = candidate["candidateId"]

    payloads = {}
    if candidate.get("payloadBlobUri"):
        payloads[candidate_id] = (
            read_bytes(blob_service, candidate["payloadBlobUri"]),
            candidate.get("payloadContentType") or candidate.get("contentType") or "application/octet-stream",
        )
    # A body with a self-verifying HTML payment table: read the rows directly and send CU only the
    # condensed body (metadata). Otherwise html_table is None and CU extracts everything as before.
    html_table = None
    if candidate.get("sourceType") == "message_body" and candidate.get("htmlBlobUri"):
        try:
            html = read_bytes(blob_service, candidate["htmlBlobUri"]).decode("utf-8", errors="replace")
            parsed_tables = extract_html_payment_tables(html, prefix=f"Subject: {candidate.get('subject') or ''}\n\n")
        except Exception as exc:  # a table-parse problem must never block the CU path
            logger.warning("html table parse failed candidate=%s error=%s", candidate_id, type(exc).__name__)
            parsed_tables = {"accepted": False}
        if parsed_tables.get("accepted"):
            html_table = parsed_tables
            payloads[candidate_id] = (parsed_tables["condensedText"].encode("utf-8"), "text/plain")
    # MSG bodies usually carry no HTML (de-encapsulating the RTF body is too slow), but the plain-text
    # body keeps the table as one cell per line - read it the same self-verifying way. Embedded
    # (attached) email bodies are plain text too.
    text_body = candidate.get("sourceType") in ("message_body", "embedded_message_body")
    if html_table is None and text_body and candidate_id in payloads:
        try:
            parsed_tables = extract_text_payment_tables(payloads[candidate_id][0].decode("utf-8", errors="replace"))
        except Exception as exc:
            logger.warning("text table parse failed candidate=%s error=%s", candidate_id, type(exc).__name__)
            parsed_tables = {"accepted": False}
        if parsed_tables.get("accepted"):
            html_table = {**parsed_tables, "strategy": "text_table+content_understanding"}
            payloads[candidate_id] = (parsed_tables["condensedText"].encode("utf-8"), "text/plain")
    stats: dict = {}
    normalized, raw = route_candidates(
        [candidate],
        payloads,
        endpoint=os.environ.get("CONTENT_UNDERSTANDING_ENDPOINT"),
        analyzer_id=os.environ.get("CONTENT_UNDERSTANDING_ANALYZER_ID", "ach.remittance.v1"),
        analyze=throttled_analyze(analyze, sleep=sleep, stats=stats),
    )
    result = normalized[0] if normalized else {
        "candidateId": candidate_id,
        "sourceType": candidate.get("sourceType"),
        "score": candidate.get("score", 0),
        "remittanceMetadata": {},
        "paymentLines": [],
        "warnings": ["candidate_not_extracted"],
    }
    if html_table:
        result["paymentLines"] = [
            {**line, "source": {**line["source"], "candidateId": candidate_id}} for line in html_table["paymentLines"]
        ]
        result["strategy"] = html_table.get("strategy", "html_table+content_understanding")
        result["deterministicTables"] = html_table["tables"]
        # Each accepted sub-table that closes with its own Total is a separate payment (e.g. one per
        # region with its own scheduled date). Recorded so validation can flag several payments.
        groups = [
            {"label": t.get("totalLabel"), "amount": t.get("tableTotal"), "lineCount": t.get("records") or t.get("lineCount")}
            for t in html_table["tables"]
            if t.get("accepted") and t.get("tableTotal") is not None
        ]
        if len(groups) > 1:
            result["paymentGroups"] = groups
        if raw:
            raw[0]["deterministicTables"] = html_table["tables"]
    raw_uri = None
    if raw:
        raw_uri = upload_json(blob_service, "analysis", f"{processing_id}/{run_id}/{candidate_id}/raw-analysis.json", raw[0])
    normalized_uri = upload_json(
        blob_service, "analysis", f"{processing_id}/{run_id}/{candidate_id}/normalized-extraction.json", result
    )
    # Who mapped the table columns for this source (for the UI): the ACH Extraction Expert or the rules.
    mapped_by = set(result.get("columnMappedBy") or [])
    for table in (html_table or {}).get("tables") or []:
        mapped_by.add(table.get("mappedBy") or (table.get("columns") or {}).get("mappedBy"))
    mapped_by.discard(None)
    return {
        "candidateId": candidate_id,
        "name": candidate.get("name"),
        "embeddedIn": candidate.get("embeddedIn"),
        "excerpt": _excerpt(raw[0] if raw else None, payloads.get(candidate_id)),
        # Full text for the ACH Resolution Expert's tools: the stored text payload of a text source.
        "sourceTextUri": candidate.get("payloadBlobUri")
        if str(candidate.get("payloadContentType") or "").startswith("text") else None,
        "strategy": result.get("strategy") or (raw[0].get("strategy") if raw else None),
        "mappedBy": "agent" if "agent" in mapped_by else ("rules" if mapped_by else None),
        "operationId": raw[0].get("operationId") if raw else None,
        "normalized": result,
        "rawAnalysisBlobUri": raw_uri,
        "normalizedExtractionBlobUri": normalized_uri,
        "lineCount": len(result.get("paymentLines") or []),
        "warnings": result.get("warnings") or [],
        "throttleRetries": stats.get("throttleRetries", 0),
        "throttled429": stats.get("throttled429", 0),
        "durationMs": _elapsed_ms(started),
    }


_EXCERPT_CHARS = 1500


def _excerpt(raw_entry: dict | None, payload: tuple[bytes, str] | None) -> str:
    """A short text view of what was read from a source, for source classification: CU's markdown, or
    the text payload itself. Kept in the extraction result (it never leaves the content plane)."""
    texts: list[str] = []
    result = (raw_entry or {}).get("result") or {}
    for part in [result] + list(result.get("parts") or []):
        inner = (part or {}).get("result", part) or {}
        for content in inner.get("contents") or []:
            if isinstance(content, dict) and isinstance(content.get("markdown"), str):
                texts.append(content["markdown"])
        if texts:
            break
    if not texts and payload and str(payload[1]).startswith("text"):
        texts.append(payload[0][: _EXCERPT_CHARS * 2].decode("utf-8", errors="replace"))
    text = " ".join(" ".join(texts).split())
    return text[:_EXCERPT_CHARS]


# --- 3. validate + reconcile (deterministic) -----------------------------------------------------


def _transfer_payments(extractions: list[dict], classification: dict, quality: dict) -> list[dict]:
    """Several transfers in one thread, from the transfer proofs Decision-1 identified: one payment per
    distinct transfer date, kept only when those transfers add up exactly to the extracted lines - so
    two receipts of the same transfer, or a proof for a payment we have no lines for, never count."""
    roles = classification.get("roles") or {}
    by_date: dict[str, dict] = {}
    for item in extractions:
        if (roles.get(item["candidateId"]) or {}).get("role") != "payment_proof":
            continue
        meta = (item.get("normalized") or {}).get("remittanceMetadata") or {}
        date, amount = meta.get("remittanceDate"), _decimal_or_none(meta.get("paymentTotal"))
        if not date or amount is None:
            continue
        group = by_date.setdefault(date, {"label": f"Transfer {date}", "amount": amount, "date": date, "sources": []})
        if group["amount"] != amount:
            return []  # one date, two different amounts: not a clean set of transfers
        group["sources"].append(item["candidateId"])
    lines_total = _decimal_or_none(quality.get("calculatedPaymentTotal"))
    if len(by_date) < 2 or lines_total is None or sum(g["amount"] for g in by_date.values()) != lines_total:
        return []
    return [{**g, "amount": float(g["amount"]), "basis": "transfer_proofs"} for g in sorted(by_date.values(), key=lambda g: g["date"])]


def _decimal_or_none(value) -> Decimal | None:
    try:
        return Decimal(str(value).replace(",", "").replace("$", "")).quantize(Decimal("0.01")) if value is not None else None
    except (InvalidOperation, ValueError):
        return None


def _merge(normalized: list[dict], intake: dict) -> dict:
    """The deterministic validator with the message's own fallbacks: the sender's organization for
    the payer, and a date written in the subject. Shared by validation and resolution re-validation."""
    message = intake.get("message") or {}
    sender_email = message.get("senderEmail")
    return validate_and_merge(
        normalized,
        tolerance=_reconciliation_tolerance(),
        sender_org=_org_from_email(sender_email),
        sender_mailbox=sender_email.split("@", 1)[0] if sender_email and "@" in sender_email else None,
        subject_date=date_from_subject(message.get("subject"), message.get("sentDate")),
    )


@_with_usage
def validate_reconcile(payload: dict, *, blob_service) -> dict:
    started = time.perf_counter()
    processing_id = payload["processingId"]
    run_id = payload["runId"]
    intake = payload["intake"]
    extractions = payload["extractions"]

    # Microsoft-Decision-1, only when rules can't settle the email (2+ CU sources with lines): exclude
    # lines from statements / correspondence and proofs that only repeat a payment line; classify the
    # thread. Fail-safe - any problem leaves the lines as extracted.
    classification = (
        payload.get("classification") or {}
        if payload.get("skipClassification")  # re-validating the ACH Resolution Expert's lines
        else intake_decision.apply(
            extractions, intake_decision.classify(extractions, subject=(intake.get("message") or {}).get("subject"))
        )
    )
    final_result = _merge([item["normalized"] for item in extractions], intake)
    quality = final_result["quality"]
    if intake.get("parserWarningCount"):
        quality["warnings"].append("attachment_parse_warning")
    if intake.get("externalLinkCount"):
        quality["warnings"].append("external_link_not_fetched")
    if intake.get("unclassifiedCount"):
        quality["warnings"].append("unclassified_attachment")
    # Several payments in one email: the result (and its CSV) describes one payment, so a bank credit
    # could not be matched to it. Flag for review and name the payments; splitting per payment is a
    # production decision (one CSV per payment).
    payment_groups = [group for item in extractions for group in (item["normalized"].get("paymentGroups") or [])]
    if len(payment_groups) <= 1:
        payment_groups = _transfer_payments(extractions, classification, quality) or payment_groups
    if len(payment_groups) > 1:
        final_result["paymentGroups"] = payment_groups
        quality["warnings"].append("multiple_payments_in_email")
    thread_kind = classification.get("threadKind")
    if thread_kind in THREAD_WARNINGS:
        quality["warnings"].append(THREAD_WARNINGS[thread_kind])
    if classification.get("status"):
        final_result["sourceClassification"] = classification
    if any(warning in REVIEW_CODES for warning in quality["warnings"]):
        quality["requiresReview"] = True
        final_result["status"] = "ReviewRequired"
    quality["warnings"] = sorted(set(quality["warnings"]))
    quality["reviewReasons"], quality["notes"] = split_warnings(quality["warnings"], REVIEW_CODES)
    final_result.update(
        {
            "processingId": processing_id,
            "runId": run_id,
            "correlationId": payload.get("correlationId"),
            "source": {
                "messageName": intake.get("sourceName"),
                "sha256": intake.get("sourceSha256"),
                "candidateIds": [item["candidateId"] for item in extractions],
            },
            "manualReviewItems": intake.get("manualReviewItems") or [],
            "candidateResults": [
                {
                    "candidateId": item["candidateId"],
                    "name": item.get("name"),
                    "strategy": item.get("strategy"),
                    "lineCount": item.get("lineCount"),
                    "warnings": item.get("warnings", []),
                    "operationId": item.get("operationId"),
                    "durationMs": item.get("durationMs"),
                    "rawAnalysisBlobUri": item.get("rawAnalysisBlobUri"),
                    "normalizedExtractionBlobUri": item.get("normalizedExtractionBlobUri"),
                }
                for item in extractions
            ],
        }
    )
    validation_uri = upload_json(blob_service, "results", f"{processing_id}/{run_id}/validation.json", quality)
    return {
        "finalResult": final_result,
        "validationBlobUri": validation_uri,
        "durationMs": _elapsed_ms(started),
        "classification": {
            "status": classification.get("status"),
            "sourcesClassified": len(classification.get("roles") or {}),
            "linesExcluded": len(classification.get("excluded") or []),
            "threadKind": classification.get("threadKind"),
        } if classification.get("status") else None,
    }


# --- 3b. resolution (only when verification fails) ------------------------------------------------


def _trim_arguments(arguments):
    """Tool-call arguments for the audit trail, without bulky line lists."""
    if not isinstance(arguments, dict):
        return arguments
    trimmed = dict(arguments)
    if isinstance(trimmed.get("lines"), list):
        trimmed["lines"] = f"{len(trimmed['lines'])} line(s)"
    return trimmed


@_with_usage
def resolve_extraction(payload: dict, *, blob_service) -> dict:
    """ACH Resolution Expert: a second, reasoned attempt for a run that failed verification.

    Lines: the expert proposes the payment lines using code-backed tools; ``resolver.gate`` accepts
    them only if every line is grounded in its source and the total reconciles. On acceptance the run
    is re-validated with the expert's lines (``validated``). Details (payer, date, reference):
    ``resolver.gate_details`` applies a grounded date / reference and keeps a payer as a
    recommendation. Otherwise nothing changes and the expert's explanation is returned for the
    analyst. Never raises."""
    from resolution import resolver
    from shared.foundry_agents import agent_configured, parse_agent_json

    started = time.perf_counter()
    final_result = payload["finalResult"]
    extractions = payload["extractions"]
    message = (payload.get("intake") or {}).get("message") or {}
    quality = final_result.get("quality") or {}
    triggers = resolver.needs_resolution(quality.get("warnings") or [], requires_review=bool(quality.get("requiresReview")))
    base = {"triggers": triggers}
    agent = agent_configured(resolver.AGENT_SETTING)
    if not agent or not triggers:
        return {**base, "status": "not_configured" if not agent else "not_needed", "durationMs": _elapsed_ms(started)}
    line_problem = bool(set(triggers) & resolver.LINE_TRIGGERS)
    try:
        texts = resolver.SourceTexts(extractions, lambda uri: read_bytes(blob_service, uri),
                                     extra={resolver.HEADER_SOURCE: resolver.header_text(message)})
        case = resolver.build_case(final_result, extractions, triggers, header=texts.get(resolver.HEADER_SOURCE))
        tools = resolver.make_tools(texts, (final_result.get("remittanceMetadata") or {}).get("paymentTotal"))
        text, trace = resolver.run_agent(agent, case, tools)
        proposal = parse_agent_json(text)
        decision = (
            resolver.gate(proposal, extractions, texts, lambda normalized: _merge(normalized, payload.get("intake") or {}))
            if line_problem else {"accepted": False, "reason": None}
        )
        details = resolver.gate_details(proposal, texts, triggers, sent_date=message.get("sentDate"))
    except Exception as exc:  # the run simply stays as it was
        logger.warning("resolution expert failed error=%s detail=%s", type(exc).__name__, str(exc)[:300])
        return {**base, "status": "failed", "error": type(exc).__name__, "errorDetail": str(exc)[:300],
                "lineProblem": line_problem, "durationMs": _elapsed_ms(started)}

    # Not resolved, but its lines are exactly the run's lines: extraction is right, and what it names
    # (several payments, a dispute...) is the business question - not an extraction failure.
    confirmed = line_problem and not decision["accepted"] and resolver.confirms_lines(proposal, final_result)
    if line_problem:
        status = "accepted" if decision["accepted"] else "confirmed" if confirmed else "rejected"
        reason = "lines_confirmed" if confirmed else decision["reason"]
    else:  # only the details (payer / date / reference) were in question
        status, reason = resolver.details_status(details)
    summary = {
        **base,
        **resolver.describe(agent),
        "status": status,
        "reason": reason,
        "lineProblem": line_problem,
        "details": details,
        "threadWarning": THREAD_WARNINGS.get(proposal.get("threadKind")) if confirmed else None,
        "dropped": decision.get("dropped"),
        "outcome": proposal.get("outcome"),
        "threadKind": proposal.get("threadKind"),
        "explanation": str(proposal.get("explanation") or "")[:600],
        "confidence": proposal.get("confidence"),
        "proposedLines": len(proposal.get("lines") or []),
        # Full audit of what the expert proposed and how it worked, whether or not it was applied.
        "proposal": [
            {"sourceId": line.get("sourceId"), "pro": line.get("pro"), "amount": line.get("amount"),
             "evidence": str(line.get("evidence") or "")[:200]}
            for line in (proposal.get("lines") or [])[:50]
        ],
        "trace": [
            {"tool": step.get("tool"), "ok": step.get("ok"), "arguments": _trim_arguments(step.get("arguments"))}
            for step in trace[:20]
        ],
        "toolCalls": [step["tool"] for step in trace],
    }
    if decision["accepted"]:
        summary["validated"] = validate_reconcile(
            {**payload, "extractions": decision["extractions"], "skipClassification": True,
             "classification": final_result.get("sourceClassification") or {}},
            blob_service=blob_service,
        )
    summary["durationMs"] = _elapsed_ms(started)
    return summary


# --- 4. assurance expert (gated; only for review runs) --------------------------------------------


@_with_usage
def run_assurance_expert(payload: dict, *, run_expert=None) -> dict:
    from assurance.experts import run_expert as default_run_expert

    return (run_expert or default_run_expert)(payload["expert"], payload["case"])


# --- 5. publish -----------------------------------------------------------------------------------


def publish_results(payload: dict, *, blob_service) -> dict:
    started = time.perf_counter()
    processing_id = payload["processingId"]
    run_id = payload["runId"]
    final_result = payload["finalResult"]
    quality = final_result["quality"]
    assurance = payload.get("assurance")

    # Cost per message: price the run's raw usage with the current price table (config/pricing.json).
    usage_totals = {}
    if isinstance(final_result.get("usage"), dict):
        final_result["usage"]["estimate"] = usage.estimate(final_result["usage"], usage.load_pricing())
        usage_totals = usage.totals(final_result["usage"])

    csv_uri = None
    csv_name = None
    if not quality["requiresReview"]:
        csv_name = create_output_filename(final_result["remittanceMetadata"])
        csv_uri = upload_bytes(
            blob_service,
            "results",
            f"{processing_id}/{run_id}/{csv_name}",
            create_payment_csv(final_result),
            "text/csv; charset=utf-8",
        )
    final_result["outputFile"] = {"format": "csv", "hasHeader": False, "blobUri": csv_uri, "fileName": csv_name}

    assurance_uri = None
    if assurance:
        final_result["assurance"] = assurance
        assurance_uri = upload_json(blob_service, "results", f"{processing_id}/{run_id}/assurance.json", assurance)

    run_final_uri = upload_json(blob_service, "results", f"{processing_id}/{run_id}/final-remittance.json", final_result)
    latest_final_uri = upload_json(blob_service, "results", f"{processing_id}/final-remittance.json", final_result)

    manifest = dict(payload.get("manifest") or {})
    manifest["processing"] = {
        "status": final_result["status"],
        "runId": run_id,
        "auditBlobUri": f"audit/{processing_id}/{run_id}.json",
        "validationBlobUri": payload.get("validationBlobUri"),
        "finalResultBlobUri": run_final_uri,
        "latestFinalResultBlobUri": latest_final_uri,
        "assuranceBlobUri": assurance_uri,
        "csvBlobUri": csv_uri,
    }
    upload_json(blob_service, "manifests", f"{processing_id}/{run_id}.json", manifest)
    upload_json(blob_service, "manifests", f"{processing_id}.json", manifest)
    return {
        "csvBlobUri": csv_uri,
        "csvFileName": csv_name,
        "finalResultBlobUri": latest_final_uri,
        "runFinalResultBlobUri": run_final_uri,
        "assuranceBlobUri": assurance_uri,
        "manifestBlobUri": f"manifests/{processing_id}.json",
        "usageTotals": usage_totals,
        "usageEstimate": (final_result.get("usage") or {}).get("estimate"),  # cost per component, for the run event
        "durationMs": _elapsed_ms(started),
    }
