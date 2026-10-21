"""ACH Resolution Expert runtime: evidence pack, code-backed tools, and the verification gate.

When a run fails verification (see TRIGGERS), the ACH Resolution Expert - a Foundry agent on the
GPT-5.6 Terra reasoning deployment (agents/resolution_agent.py) - reasons across every source and
proposes the payment lines. Its tools are this module's own code (read a source's text, read its
tables with the deterministic readers, check a candidate line set). The proposal is accepted only if
``gate`` passes: it keeps every trusted line (the expert may recover lines, never select among valid
ones), every line is grounded in its source, and the total reconciles to the stated payment total.
Otherwise the run stays in review with the expert's proposal and explanation. The stated total is
never changed by the expert.

The run's details (payer, date, reference) in question are resolved the same way - proposed with
evidence, verified in code (``gate_details``): a date or reference whose evidence is found in the
cited source (the email header counts) is applied; a payer is only recommended, because it decides
which customer account the cash is applied to.
"""

from __future__ import annotations

import json
import logging
import re
from collections import Counter
from decimal import Decimal, InvalidOperation

logger = logging.getLogger("remittance_intake")

AGENT_SETTING = "RESOLUTION_AGENT_NAME"
# Verification failures the expert may resolve. Business review reasons (PRO suffixes, credits, a
# collections dispute...) are not extraction problems and go straight to the Assurance Expert.
LINE_TRIGGERS = {
    "line_not_grounded",
    "payment_total_mismatch",
    "no_payment_lines_found",
    "content_understanding_analysis_failed",
    "column_mapping_needs_review",
    "spreadsheet_total_mismatch",
}
# The run's details: field -> the review reasons that put it in question.
DETAIL_TRIGGERS = {
    "payerName": {"conflicting_payerName", "missing_payer_name", "payer_name_matches_payee"},
    "remittanceDate": {"conflicting_remittanceDate", "invalid_remittance_date"},
    "paymentReference": {"conflicting_paymentReference"},
}
# A missing date alone is only a note - not worth a reasoning call on a clean run. It is included
# when the expert runs anyway, and calls the expert on its own when the run already needs review
# for another reason (the analyst then gets the date filled, with evidence).
SECONDARY_TRIGGERS = {"missing_remittance_date": "remittanceDate"}
# Runs where filling a single date makes no sense: each payment has its own, or there is no remittance.
_NO_DATE_RESOLUTION = {"multiple_payments_in_email", "no_remittance_in_email"}
TRIGGERS = LINE_TRIGGERS | {code for codes in DETAIL_TRIGGERS.values() for code in codes}
# Applied automatically when grounded; a payer decides which customer account the cash goes to, so
# it is only ever recommended (the analyst confirms).
AUTO_APPLY_FIELDS = ("remittanceDate", "paymentReference")
DATE_WINDOW_DAYS = 60
HEADER_SOURCE = "message-header"
PAGE_CHARS = 8000
EXCERPT_CHARS = 2000
MAX_CASE_LINES = 40
MAX_TABLE_LINES = 300


def needs_resolution(warnings: list[str], *, requires_review: bool = False) -> list[str]:
    """The verification failures that call for the expert (pure - used by the orchestrator).

    ``requires_review``: the run is already in review for another reason, so a missing date is worth
    the call on its own."""
    from validation.remittance_validator import PER_PAYMENT_CODES

    present = set(warnings or [])
    triggers = present & TRIGGERS
    # Several payments in one email explain the gap to the (single) stated total and the differing
    # dates / references; recovering lines cannot fix them, and choosing among payments is the analyst's.
    if "multiple_payments_in_email" in present:
        triggers -= set(PER_PAYMENT_CODES)
    if triggers or (requires_review and not present & _NO_DATE_RESOLUTION):
        triggers |= present & set(SECONDARY_TRIGGERS)
    return sorted(triggers)


def header_text(message: dict) -> str:
    """The email's own header as a citable source (the subject often states the date or amount)."""
    parts = [("Subject", message.get("subject")), ("From", " ".join(filter(None, [message.get("senderName"),
             f"<{message['senderEmail']}>" if message.get("senderEmail") else None]))), ("Sent", message.get("sentDate"))]
    return "\n".join(f"{label}: {value}" for label, value in parts if value)


def _detail_fields(triggers) -> set[str]:
    fields = {field for field, codes in DETAIL_TRIGGERS.items() if codes & set(triggers)}
    return fields | {field for code, field in SECONDARY_TRIGGERS.items() if code in triggers}


def skip_reason(warnings: list[str]) -> str | None:
    """Why the expert was not called although verification failed (audit only - pure)."""
    warnings = warnings or []
    if "payment_total_mismatch" in warnings and "multiple_payments_in_email" in warnings and not needs_resolution(warnings):
        return "explained_by_multiple_payments"
    return None


def confirms_lines(proposal: dict, final_result: dict) -> bool:
    """The expert did not resolve, but its lines are exactly the run's lines (same source, same
    amount): extraction is right and the run is a business question (several payments, a dispute...)."""
    lines = proposal.get("lines") or []
    current = final_result.get("paymentLines") or []
    if proposal.get("outcome") == "resolved" or not lines or not current:
        return False
    proposed = Counter((str(l.get("sourceId")), _amount(l.get("amount"))) for l in lines)
    extracted = Counter(((l.get("source") or {}).get("candidateId"), _amount(l.get("amountPaid"))) for l in current)
    return proposed == extracted


def apply_resolution(final_result: dict, resolved: dict) -> dict:
    """Fold the expert's result into the run (pure - shared by the orchestrator and the golden runner).

    Accepted: the re-validated result with the expert's lines (a note, not a review reason).
    Confirmed: the lines stand; the thread kind it named (e.g. several payments) becomes the review reason."""
    def add(result, code, blocking):
        from validation.remittance_validator import split_warnings

        quality = result["quality"]
        reasons, notes = set(quality.get("reviewReasons") or []), set(quality.get("notes") or [])
        (reasons if blocking else notes).add(code)
        quality["warnings"] = sorted(set(quality["warnings"]) | {code})
        # Re-split with the run's own blocking set (it may include review codes beyond the validator's).
        resplit, _ = split_warnings(quality["warnings"], extra_blocking=reasons)
        quality["reviewReasons"] = resplit
        quality["notes"] = sorted((set(quality["warnings"]) - set(resplit)) | (notes - set(resplit)))

    resolution = {k: v for k, v in resolved.items() if k not in ("validated", "usage")}  # usage: in the run's usage block
    if resolved.get("status") == "accepted" and resolved.get("validated"):
        final_result = resolved["validated"]["finalResult"]
        add(final_result, "resolved_by_expert", blocking=False)
    elif resolved.get("status") == "confirmed" and resolved.get("threadWarning"):
        add(final_result, resolved["threadWarning"], blocking=True)
    applied = ((resolved.get("details") or {}).get("applied")) or {}
    if applied:
        quality = final_result["quality"]
        for field, record in applied.items():
            clears, note = _DETAIL_CLEARS[field]
            final_result["remittanceMetadata"][field] = record["value"]
            quality["warnings"] = sorted((set(quality["warnings"]) - clears) | {note})
            quality["reviewReasons"] = sorted(set(quality.get("reviewReasons") or []) - clears)
            quality["notes"] = sorted((set(quality.get("notes") or []) - clears) | {note})
        # The details were the only reasons for review: the run completes like any clean run.
        quality["requiresReview"] = bool(quality["reviewReasons"])
        final_result["status"] = "ReviewRequired" if quality["requiresReview"] else "Extracted"
    final_result["resolution"] = resolution
    return final_result


def _amount(value) -> Decimal | None:
    try:
        return Decimal(str(value).replace(",", "").replace("$", "")).quantize(Decimal("0.01"))
    except (InvalidOperation, ValueError):
        return None


class SourceTexts:
    """Full text of each source, read lazily: the text payload (bodies, embedded emails) or the
    markdown Content Understanding produced (PDFs, images, Office documents)."""

    def __init__(self, extractions: list[dict], read_bytes, extra: dict[str, str] | None = None):
        self.by_id = {item["candidateId"]: item for item in extractions}
        self.read_bytes = read_bytes
        # Texts that are not extractions (the email header), citable like any source.
        self.cache: dict[str, str] = {key: value for key, value in (extra or {}).items() if value}
        self.by_id.update({key: {"candidateId": key} for key in self.cache})

    def get(self, source_id: str) -> str:
        if source_id in self.cache:
            return self.cache[source_id]
        item = self.by_id.get(source_id)
        if item is None:
            raise KeyError(f"unknown source {source_id}")
        text = ""
        if item.get("sourceTextUri"):
            text = self.read_bytes(item["sourceTextUri"]).decode("utf-8", errors="replace")
        elif item.get("rawAnalysisBlobUri"):
            raw = json.loads(self.read_bytes(item["rawAnalysisBlobUri"]))
            result = raw.get("result") or {}
            parts = [result] + list(result.get("parts") or [])
            texts = []
            for part in parts:
                inner = (part or {}).get("result", part) or {}
                texts += [c["markdown"] for c in inner.get("contents") or [] if isinstance(c, dict) and isinstance(c.get("markdown"), str)]
            text = "\n".join(texts)
        self.cache[source_id] = text
        return text


def build_case(final_result: dict, extractions: list[dict], triggers: list[str], header: str | None = None) -> dict:
    """The evidence pack for the expert: stated facts, the problems, and every source."""
    from validation.remittance_validator import _payee_aliases

    metadata = final_result.get("remittanceMetadata") or {}
    quality = final_result.get("quality") or {}
    roles = ((final_result.get("sourceClassification") or {}).get("roles")) or {}
    fields = _detail_fields(triggers)
    return {
        "statedPaymentTotal": metadata.get("paymentTotal"),
        "payerName": metadata.get("payerName"),
        "remittanceDate": metadata.get("remittanceDate"),
        "paymentReference": metadata.get("paymentReference"),
        "problems": triggers,
        "lineProblem": bool(set(triggers) & LINE_TRIGGERS),
        "detailsInQuestion": sorted(fields),
        # The receiving company (never the payer) and what each source said for a disputed detail.
        "payeeNames": _payee_aliases(None),
        "detailConflicts": [c for c in quality.get("conflicts") or [] if c.get("field") in fields],
        "currentLines": {"count": quality.get("lineCount"), "sum": quality.get("calculatedPaymentTotal"),
                         "differenceFromStated": quality.get("totalDifference")},
        "emailHeader": {"id": HEADER_SOURCE, "text": header} if header else None,
        "sources": [
            {
                "id": item["candidateId"],
                "name": item.get("name"),
                "kind": (item.get("normalized") or {}).get("sourceType"),
                "embeddedIn": item.get("embeddedIn"),
                "classifiedRole": (roles.get(item["candidateId"]) or {}).get("role"),
                "details": {field: ((item.get("normalized") or {}).get("remittanceMetadata") or {}).get(field)
                            for field in ("payerName", "remittanceDate", "paymentReference")},
                "excerpt": (item.get("excerpt") or "")[:EXCERPT_CHARS],
                "lineCount": len((item.get("normalized") or {}).get("paymentLines") or []),
                "lines": [
                    {"pro": line.get("proInvoiceNumber"), "amount": line.get("amountPaid"), "warnings": line.get("warnings") or []}
                    for line in ((item.get("normalized") or {}).get("paymentLines") or [])[:MAX_CASE_LINES]
                ],
                "warnings": item.get("warnings") or [],
            }
            for item in extractions
        ],
    }


def make_tools(texts: SourceTexts, stated_total) -> dict:
    """The expert's tools: plain functions over the run's own data (no side effects)."""
    from extraction.content_understanding import ground_lines
    from extraction.html_tables import extract_html_payment_tables
    from extraction.text_tables import extract_text_payment_tables

    def get_source_text(sourceId: str, offset: int = 0) -> dict:
        text = texts.get(sourceId)
        offset = max(0, int(offset or 0))
        page = text[offset : offset + PAGE_CHARS]
        return {"sourceId": sourceId, "length": len(text), "offset": offset, "text": page,
                "nextOffset": offset + len(page) if offset + len(page) < len(text) else None}

    def read_tables(sourceId: str) -> dict:
        text = texts.get(sourceId)
        result = extract_html_payment_tables(text) if "<table" in text.lower() else extract_text_payment_tables(text)
        lines = result.get("paymentLines") or []
        return {
            "sourceId": sourceId,
            "accepted": bool(result.get("accepted")),
            "tables": result.get("tables") or [],
            "lineCount": len(lines),
            "lines": [{"pro": l.get("proInvoiceNumber"), "amount": l.get("amountPaid")} for l in lines[:MAX_TABLE_LINES]],
        }

    def check_lines(lines: list) -> dict:
        checked, total = [], Decimal("0")
        for line in lines or []:
            probe = [{"proInvoiceNumber": str(line.get("pro") or ""), "amountPaid": line.get("amount")}]
            try:
                ground_lines(probe, texts.get(str(line.get("sourceId"))))
                grounded = bool((probe[0].get("source") or {}).get("grounded"))
            except KeyError:
                grounded = False
            amount = _amount(line.get("amount")) or Decimal("0")
            total += amount
            checked.append({**line, "grounded": grounded})
        stated = _amount(stated_total)
        return {
            "lines": checked,
            "allGrounded": all(c["grounded"] for c in checked) if checked else False,
            "sum": float(total),
            "statedPaymentTotal": float(stated) if stated is not None else None,
            "differenceFromStated": float(stated - total) if stated is not None else None,
        }

    return {"get_source_text": get_source_text, "read_tables": read_tables, "check_lines": check_lines}


def describe(agent: str) -> dict:
    """Agent version + model for the audit trail (replaceable in tests)."""
    from shared.foundry_agents import describe_agent

    return describe_agent(agent)


def run_agent(agent: str, case: dict, tools: dict) -> tuple[str, list[dict]]:
    """Invoke the expert (replaceable for tests and the golden runner's cache)."""
    from shared.foundry_agents import invoke_agent_with_tools

    return invoke_agent_with_tools(agent, json.dumps(case, default=str), tools)


def _is_trusted(line: dict) -> bool:
    """A line verification vouched for: grounding passed, or read exactly by a table/sheet reader."""
    if "line_not_grounded" in (line.get("warnings") or []) or _amount(line.get("amountPaid")) is None:
        return False
    source = line.get("source") or {}
    return source.get("grounded") is True or source.get("method") in ("html_table", "text_table") or "row" in source


def gate(proposal: dict, extractions: list[dict], texts: SourceTexts, validate) -> dict:
    """Accept the proposal only if every line is grounded in its source and the total reconciles.

    Returns {"accepted", "reason", "extractions"} where ``extractions`` are copies whose lines are the
    proposal's (only when accepted). ``validate(normalized_list)`` is the deterministic validator."""
    from extraction.content_understanding import ground_lines

    lines = proposal.get("lines") or []
    if proposal.get("outcome") != "resolved" or not lines:
        return {"accepted": False, "reason": "expert_could_not_resolve"}
    # Recovering is allowed, selecting is not: with several valid lines of equal amounts, picking a
    # subset can always "reconcile". Every trusted line (grounding passed, or read exactly by a table /
    # spreadsheet reader - and not excluded by classification) must keep its slot - same source, same
    # amount; a corrected PRO keeps the slot. A missing slot means the expert chose among valid lines:
    # the run stays in review with the proposal as a suggestion.
    proposed = Counter((str(l.get("sourceId")), _amount(l.get("amount"))) for l in lines)
    trusted = Counter(
        (item["candidateId"], _amount(line.get("amountPaid")))
        for item in extractions
        for line in (item.get("normalized") or {}).get("paymentLines") or []
        if _is_trusted(line)
    )
    dropped = trusted - proposed
    if dropped:
        return {"accepted": False, "reason": "drops_trusted_lines",
                "dropped": [f"{source} {amount}" for (source, amount) in list(dropped.elements())[:10]]}
    by_source: dict[str, list[dict]] = {}
    for line in lines:
        source_id = str(line.get("sourceId"))
        if source_id not in texts.by_id:
            return {"accepted": False, "reason": f"unknown_source:{source_id}"}
        candidate = {"proInvoiceNumber": str(line.get("pro") or ""), "amountPaid": line.get("amount"),
                     "source": {"candidateId": source_id, "method": "resolution_expert", "evidence": str(line.get("evidence") or "")[:300]},
                     "confidence": None}
        ground_lines([candidate], texts.get(source_id))
        if not candidate["source"].get("grounded"):
            return {"accepted": False, "reason": f"line_not_grounded:{candidate['proInvoiceNumber']}"}
        candidate.pop("warnings", None)
        by_source.setdefault(source_id, []).append(candidate)
    resolved = []
    for item in extractions:
        normalized = dict(item.get("normalized") or {})
        normalized["paymentLines"] = by_source.get(item["candidateId"], [])
        resolved.append({**item, "normalized": normalized, "lineCount": len(normalized["paymentLines"])})
    check = validate([item["normalized"] for item in resolved])
    if not (check.get("quality") or {}).get("isReconciled"):
        return {"accepted": False, "reason": "not_reconciled"}
    return {"accepted": True, "reason": "grounded_and_reconciled", "extractions": resolved}


_MONTHS = ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec")


def _words(text) -> str:
    """Words and numbers only, in order: the cited text must be in the source, but table pipes, colons,
    line breaks or markdown that CU put around it (or the expert left out) do not matter."""
    return " " + " ".join(re.findall(r"[0-9a-z]+", str(text or "").lower())) + " "


def _alnum(text) -> str:
    return re.sub(r"[^0-9a-z]", "", str(text or "").lower())


def _detail_problem(field: str, value: str, evidence: str, sent_date) -> str | None:
    """Why a grounded detail value is still not acceptable (None = acceptable)."""
    from datetime import date

    if field == "remittanceDate":
        try:
            parsed = date.fromisoformat(value[:10])
        except ValueError:
            return "date_unreadable"
        numbers = {int(n) for n in re.findall(r"\d+", evidence)}
        month_named = _MONTHS[parsed.month - 1] in evidence.lower()
        if not ((parsed.month in numbers or month_named) and parsed.day in numbers):
            return "date_not_in_evidence"
        try:
            sent = date.fromisoformat(str(sent_date)[:10]) if sent_date else None
        except ValueError:
            sent = None
        if sent is not None and abs((parsed - sent).days) > DATE_WINDOW_DAYS:
            return "date_far_from_email"
        return None
    if field == "paymentReference":
        return None if _alnum(value) and _alnum(value) in _alnum(evidence) else "reference_not_in_evidence"
    if field == "payerName":
        from validation.remittance_validator import _matches_payee, _normalize_name, _payee_aliases

        if _matches_payee(value, _payee_aliases(None)):
            return "payer_is_payee"
        return None if _normalize_name(value) and _normalize_name(value) in _normalize_name(evidence) else "payer_not_in_evidence"
    return "unknown_field"


def gate_details(proposal: dict, texts: SourceTexts, triggers, *, sent_date=None) -> dict:
    """Verify the expert's run details. Each must answer a detail in question, cite evidence found in
    the cited source, and contain the value (a date also lies near the email's date). A verified date
    or reference is applied; a payer is only recommended. Returns {applied, recommended, rejected}."""
    fields = _detail_fields(triggers)
    result = {"applied": {}, "recommended": {}, "rejected": []}
    for item in proposal.get("details") or []:
        field, value = item.get("field"), str(item.get("value") or "").strip()
        evidence, source_id = str(item.get("evidence") or "").strip(), str(item.get("sourceId") or "")
        record = {"value": value, "sourceId": source_id, "evidence": evidence[:300], "reason": str(item.get("reason") or "")[:400]}
        if field not in fields or field in result["applied"] or field in result["recommended"]:
            continue
        problem = None
        if not value or len(evidence) < 4:
            problem = "no_evidence"
        elif source_id not in texts.by_id:
            problem = "unknown_source"
        elif _words(evidence) not in _words(texts.get(source_id)):
            problem = "evidence_not_in_source"
        else:
            problem = _detail_problem(field, value, evidence, sent_date)
        if problem:
            result["rejected"].append({"field": field, **record, "problem": problem})
        elif field in AUTO_APPLY_FIELDS:
            result["applied"][field] = record
        else:
            result["recommended"][field] = record
    return result


def details_status(details: dict) -> tuple[str, str]:
    """(status, reason) of a details-only resolution."""
    if details.get("applied"):
        return "accepted", "details_resolved"
    if details.get("recommended"):
        return "recommended", "details_recommended"
    return "rejected", "details_not_verified"


# Review reasons cleared when the expert's value for a detail is applied, and the note recorded.
_DETAIL_CLEARS = {
    "remittanceDate": ({"conflicting_remittanceDate", "invalid_remittance_date", "missing_remittance_date"}, "remittance_date_resolved_by_expert"),
    "paymentReference": ({"conflicting_paymentReference"}, "payment_reference_resolved_by_expert"),
}
