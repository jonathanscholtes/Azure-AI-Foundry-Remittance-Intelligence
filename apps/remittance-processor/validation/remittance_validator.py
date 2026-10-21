from __future__ import annotations

import csv
import io
import os
import re
from decimal import Decimal, InvalidOperation

from shared import pro_rules

_METADATA_FIELDS = ("payerName", "remittanceDate", "paymentTotal", "currency", "paymentReference")


def normalize_pro(value, rules: dict | None = None) -> tuple[str | None, str | None, str | None]:
    """(normalized PRO, raw, error code). The PRO format comes from the PRO_* app settings."""
    raw = "" if value is None else str(value).strip()
    if not raw:
        return None, raw or None, "missing_pro_number"
    rules = rules or pro_rules.rules()
    suffix, pattern = pro_rules.patterns(rules["minDigits"], rules["maxDigits"], tuple(rules["stripSuffixes"]))
    normalized = suffix.sub("", raw) if suffix else raw
    if not pattern.fullmatch(normalized):
        return None, raw, "invalid_pro_number"
    return normalized, raw, None


def _decimal(value) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    text = str(value).strip().replace("$", "").replace(",", "")
    if not text:
        return None
    if text.startswith("(") and text.endswith(")"):
        text = "-" + text[1:-1].strip()
    try:
        amount = Decimal(text)
    except (InvalidOperation, ValueError):
        return None
    return amount if amount.is_finite() else None


def _confidence(result: dict, field: str) -> float:
    confidence = (result.get("metadataEvidence") or {}).get(field, {}).get("confidence")
    try:
        return float(confidence) if confidence is not None else -1.0
    except (TypeError, ValueError):
        return -1.0


_LEGAL_SUFFIX_RE = re.compile(r"\b(inc|incorporated|llc|corp|corporation|co|company|ltd|limited|lp|llp|plc)\b")
_TRAILING_ACCOUNT_RE = re.compile(r"\s*[-–]\s*\d+\s*$")


def _normalize_name(value) -> str:
    """Fold a company name to a comparison key: lowercased, no punctuation, legal suffix, or trailing acct #.

    So "Northwind" / "Northwind, Inc." and "CONTOSO MACHINE SHOP INC - 100200" / "Contoso Machine Shop" compare equal.
    """
    text = str(value or "").lower().strip()
    text = _TRAILING_ACCOUNT_RE.sub("", text)
    text = re.sub(r"[.,_]", " ", text)  # underscores so a mailbox ("AP_Riverside") folds like a name
    text = _LEGAL_SUFFIX_RE.sub(" ", text)
    return re.sub(r"\s+", " ", text).strip()


def _source_priority(candidate: dict) -> int:
    """A document/attachment carries the authoritative payment date; email text (the body or an
    embedded email's body) is only a fallback."""
    return 0 if candidate.get("sourceType") in ("message_body", "embedded_message_body") else 1


def _payee_aliases(explicit) -> list[str]:
    """Names of the receiving org (the payee). Configured via RECEIVING_ORG_ALIASES so it stays out of code."""
    if explicit is not None:
        return explicit
    raw = os.environ.get("RECEIVING_ORG_ALIASES", "")
    return [alias for alias in (part.strip() for part in raw.split(",")) if alias]


def _matches_payee(value, payee_aliases) -> bool:
    payer_norm = _normalize_name(value)
    if not payer_norm:
        return False
    for alias in payee_aliases:
        alias_norm = _normalize_name(alias)
        if alias_norm and (alias_norm in payer_norm or payer_norm in alias_norm):
            return True
    return False


def _merge_metadata(candidate_results: list[dict], payee_aliases: list[str] = ()) -> tuple[dict, list[str], list[dict]]:
    metadata = {field: None for field in _METADATA_FIELDS}
    warnings: list[str] = []
    conflicts: list[dict] = []
    for field in _METADATA_FIELDS:
        values = []
        for candidate in candidate_results:
            value = (candidate.get("remittanceMetadata") or {}).get(field)
            if value is not None and str(value).strip():
                values.append((candidate, value))
        # The payee's name is not a valid payer; drop it so a real payer (e.g. from the sender) wins.
        # If every candidate returned only the payee, keep it so the downstream guard still flags it.
        if field == "payerName" and payee_aliases:
            non_payee = [(candidate, value) for candidate, value in values if not _matches_payee(value, payee_aliases)]
            if non_payee:
                values = non_payee
        if not values:
            continue
        if field == "remittanceDate":
            # Prefer the document/attachment date over the email-stated date (confidence then score break ties).
            values.sort(
                key=lambda item: (_source_priority(item[0]), _confidence(item[0], field), int(item[0].get("score", 0))),
                reverse=True,
            )
        else:
            values.sort(
                key=lambda item: (_confidence(item[0], field), int(item[0].get("score", 0))),
                reverse=True,
            )
        metadata[field] = values[0][1]
        if field == "payerName":
            # Formatting variants of the same name ("Northwind" vs "Northwind, Inc.") are not a conflict.
            distinct = {_normalize_name(value) for _, value in values}
        elif field == "remittanceDate":
            # A document date supersedes the email-stated date, so a document/email mismatch is not a
            # conflict; compare only document sources when any exist (document-vs-document still flags).
            doc_values = [value for candidate, value in values if _source_priority(candidate) == 1]
            distinct = {str(value) for value in (doc_values or [v for _, v in values])}
        else:
            distinct = {str(value) for _, value in values}
        if len(distinct) > 1:
            warnings.append(f"conflicting_{field}")
            conflicts.append(
                {
                    "field": field,
                    "selectedValue": values[0][1],
                    "candidateValues": [
                        {"candidateId": candidate.get("candidateId"), "value": value}
                        for candidate, value in values
                    ],
                }
            )
    return metadata, warnings, conflicts


# Warnings that put a run in review (any conflicting_* also does). Everything else is a note: it
# records how a value was inferred or what a source was, without needing a person.
BLOCKING_WARNINGS = {
    "missing_payer_name",
    "invalid_remittance_date",
    "unsupported_currency",
    "invalid_payment_total",
    "negative_payment_total",
    "missing_pro_number",
    "missing_invoice_number",
    "invalid_pro_number",
    "payer_name_matches_payee",
    "missing_or_invalid_amount_paid",
    "negative_amount_paid",
    "zero_amount_paid",
    "conflicting_payment_line",
    "line_not_grounded",
    "spreadsheet_total_mismatch",
    "column_mapping_needs_review",
    "payment_line_count_mismatch",
    "no_payment_lines_found",
    "payment_total_mismatch",
    "conflicting_payerName",
    "conflicting_remittanceDate",
    "conflicting_paymentTotal",
    "conflicting_currency",
    "conflicting_paymentReference",
    "content_understanding_not_configured",
    "content_understanding_analysis_failed",
    "candidate_content_unavailable",
    "unsupported_content_type",
    "header_row_not_detected; first two columns used and require review",
    "spreadsheet_error_cell",
    "formula_unresolved",
}


def is_blocking(warning: str, extra_blocking=()) -> bool:
    return warning in BLOCKING_WARNINGS or warning in extra_blocking or warning.startswith("conflicting_")


# Expected when an email covers several payments - each has its own date, reference and amount - so
# with multiple_payments_in_email they are notes, not separate review reasons.
PER_PAYMENT_CODES = ("conflicting_remittanceDate", "conflicting_paymentReference", "conflicting_paymentTotal", "payment_total_mismatch")


def split_warnings(warnings, extra_blocking=()) -> tuple[list[str], list[str]]:
    """(review reasons, notes): the single source of truth the UI and summaries use."""
    unique = sorted(set(warnings or []))
    several = "multiple_payments_in_email" in unique

    def blocking(w):
        return is_blocking(w, extra_blocking) and not (several and w in PER_PAYMENT_CODES)

    return [w for w in unique if blocking(w)], [w for w in unique if not blocking(w)]


_SUBJECT_DATE_RE = re.compile(r"(?<![\d/.,$-])(\d{1,2})[/-](\d{1,2})(?:[/-](\d{4}|\d{2}))?(?![\d/.,])")
SUBJECT_DATE_WINDOW_DAYS = 60


def date_from_subject(subject: str | None, sent_date: str | None) -> str | None:
    """A payment date written in the subject ("ACH Payment of $30,665.42 10/02"), as ISO - used only
    when no source states one. Month/day order (US); a missing year comes from the sent date. Only
    one distinct date may appear, and it must fall within SUBJECT_DATE_WINDOW_DAYS of the sent date."""
    from datetime import date, timedelta

    if not subject:
        return None
    try:
        sent = date.fromisoformat(str(sent_date)[:10]) if sent_date else None
    except ValueError:
        sent = None
    found = set()
    for month, day, year in _SUBJECT_DATE_RE.findall(subject):
        if not year and sent is None:
            continue
        full_year = (2000 + int(year) if len(year) == 2 else int(year)) if year else sent.year
        try:
            value = date(full_year, int(month), int(day))
        except ValueError:
            continue
        if not year and value > sent + timedelta(days=SUBJECT_DATE_WINDOW_DAYS):
            value = value.replace(year=value.year - 1)  # "12/30" in a January email
        found.add(value)
    if len(found) != 1:
        return None
    value = found.pop()
    if sent is not None and abs((value - sent).days) > SUBJECT_DATE_WINDOW_DAYS:
        return None
    return value.isoformat()


def _stable_line_key(pro: str, amount: Decimal, line: dict) -> tuple:
    return (
        pro,
        amount.quantize(Decimal("0.01")),
        str(line.get("invoiceDate") or ""),
        str(line.get("lineReference") or ""),
    )


def validate_and_merge(
    candidate_results: list[dict],
    *,
    stated_total=None,
    currency: str | None = None,
    tolerance: Decimal = Decimal("0.01"),
    expected_line_count: int | None = None,
    payee_aliases: list[str] | None = None,
    sender_org: str | None = None,
    sender_mailbox: str | None = None,
    subject_date: str | None = None,
) -> dict:
    aliases = _payee_aliases(payee_aliases)
    ordered = sorted(candidate_results, key=lambda item: int(item.get("score", 0)), reverse=True)
    metadata, warnings, conflicts = _merge_metadata(ordered, aliases)
    # No source states a payment date, but the subject does ("ACH Payment ... 10/02"): use it, noted.
    if not metadata.get("remittanceDate") and subject_date:
        metadata["remittanceDate"] = subject_date
        warnings.append("remittance_date_derived_from_subject")

    # Fall back to the sending organization (from the email domain) only when extraction returned
    # nothing, the payee's name, or just the sender's mailbox ("AP_Riverside") - never override a
    # real company name present in the content (e.g. "Fabrikam Wine & Spirits").
    if sender_org:
        current = metadata.get("payerName")
        if (
            not current
            or _matches_payee(current, aliases)
            or (sender_mailbox and _normalize_name(current) == _normalize_name(sender_mailbox))
        ):
            metadata["payerName"] = sender_org
    for candidate in ordered:
        warnings.extend(str(warning) for warning in candidate.get("warnings", []))
    stated_value = _decimal(stated_total if stated_total is not None else metadata["paymentTotal"])
    if stated_total is not None:
        metadata["paymentTotal"] = float(stated_value) if stated_value is not None else stated_total

    resolved_currency = currency or metadata.get("currency")
    defaults_applied = []
    if not resolved_currency:
        resolved_currency = "USD"
        defaults_applied.append("currency=USD")
    metadata["currency"] = str(resolved_currency).upper()

    if not metadata.get("payerName"):
        warnings.append("missing_payer_name")
    elif _matches_payee(metadata["payerName"], aliases):
        # Every source returned only the receiving org's name; flag rather than record the wrong party.
        warnings.append("payer_name_matches_payee")
    if metadata.get("remittanceDate"):
        from datetime import date

        try:
            date.fromisoformat(str(metadata["remittanceDate"])[:10])
        except ValueError:
            warnings.append("invalid_remittance_date")
    else:
        warnings.append("missing_remittance_date")
    if metadata["currency"] != "USD":
        warnings.append("unsupported_currency")
    if stated_total is not None and stated_value is None:
        warnings.append("invalid_payment_total")
    elif stated_value is not None and stated_value < 0:
        warnings.append("negative_payment_total")

    payment_lines = []
    duplicate_groups = []
    seen: dict[tuple, int] = {}
    seen_by_pro: dict[str, list[Decimal]] = {}
    # A candidate whose lines carry no amounts (e.g. a zero-balance exceptions image) is not the
    # payment detail; skip its lines when another candidate provides real amounts, so it does not
    # pollute the result with false missing-amount flags.
    candidates_with_amounts = {
        candidate.get("candidateId")
        for candidate in ordered
        if any(_decimal(line.get("amountPaid")) is not None for line in candidate.get("paymentLines") or [])
    }
    for candidate in ordered:
        candidate_id = candidate.get("candidateId")
        if candidates_with_amounts and candidate_id not in candidates_with_amounts and candidate.get("paymentLines"):
            continue
        for source_line in candidate.get("paymentLines") or []:
            line = dict(source_line)
            warnings.extend(str(warning) for warning in line.get("warnings", []))
            raw_pro = line.get("proInvoiceNumber")
            pro_number, raw_value, pro_error = normalize_pro(raw_pro)
            line["rawProInvoiceNumber"] = raw_value
            line["proInvoiceNumber"] = pro_number
            if pro_error:
                line.setdefault("warnings", []).append(pro_error)
                warnings.append(pro_error)

            amount = _decimal(line.get("amountPaid"))
            if amount is None:
                line["amountPaid"] = None
                line.setdefault("warnings", []).append("missing_or_invalid_amount_paid")
                warnings.append("missing_or_invalid_amount_paid")
            else:
                line["amountPaid"] = float(amount)
                if amount < 0:
                    line.setdefault("warnings", []).append("negative_amount_paid")
                    warnings.append("negative_amount_paid")
                if amount == 0:
                    line.setdefault("warnings", []).append("zero_amount_paid")
                    warnings.append("zero_amount_paid")

            source = line.setdefault("source", {})
            source.setdefault("candidateId", candidate_id)
            source.setdefault("sourceType", candidate.get("sourceType"))
            if pro_number and amount is not None:
                key = _stable_line_key(pro_number, amount, line)
                if key in seen:
                    original_index = seen[key]
                    duplicate_groups.append(
                        {
                            "duplicateOfLineIndex": original_index,
                            "proInvoiceNumber": pro_number,
                            "amountPaid": float(amount),
                            "duplicateCandidateId": candidate_id,
                        }
                    )
                    payment_lines[original_index]["source"].setdefault("duplicateCandidateIds", []).append(candidate_id)
                    continue
                else:
                    seen[key] = len(payment_lines)
                    # The same PRO with different amounts is a conflict - unless the lines belong to
                    # different payments (sub-tables with their own Totals): an invoice paid in parts.
                    group = (candidate_id, source["paymentGroup"]) if source.get("paymentGroup") else None
                    priors = seen_by_pro.setdefault(pro_number, [])
                    differing = [prior_group for prior_amount, prior_group in priors if prior_amount != amount]
                    if differing:
                        if group is not None and all(g is not None and g != group for g in differing):
                            warnings.append("pro_paid_across_payments")
                        else:
                            line.setdefault("warnings", []).append("conflicting_amount_for_pro")
                            warnings.append("conflicting_payment_line")
                    priors.append((amount, group))
            payment_lines.append(line)

    if not payment_lines:
        warnings.append("no_payment_lines_found")
    if expected_line_count is not None and len(payment_lines) != expected_line_count:
        warnings.append("payment_line_count_mismatch")

    calculated_total = sum(
        (_decimal(line.get("amountPaid")) or Decimal("0"))
        for line in payment_lines
    )
    difference = None
    is_reconciled = None
    if stated_value is not None:
        difference = stated_value - calculated_total
        is_reconciled = abs(difference) <= tolerance
        if not is_reconciled:
            warnings.append("payment_total_mismatch")
    else:
        warnings.append("payment_total_not_provided")
        if "column_mapping_unverified" in warnings:
            # The ACH Extraction Expert and the header rules disagree on a sheet's columns, the sheet
            # has no Total row, and the email states no total: nothing verifies the amounts.
            warnings.append("column_mapping_needs_review")

    review_reasons, notes = split_warnings(warnings)
    requires_review = bool(review_reasons)
    status = "ReviewRequired" if requires_review else "Extracted"
    # The Content Understanding analyzer(s) the candidates actually used; "none" when every source was
    # read deterministically (spreadsheet parser) or not analyzed at all.
    analyzers = sorted({str(item["analyzerId"]) for item in candidate_results if item.get("analyzerId")})
    return {
        "schemaVersion": "1.0",
        "analyzerVersion": ", ".join(analyzers) or "none",
        "status": status,
        "remittanceMetadata": metadata,
        "paymentLines": payment_lines,
        "quality": {
            "lineCount": len(payment_lines),
            "calculatedPaymentTotal": float(calculated_total),
            "totalDifference": float(difference) if difference is not None else None,
            "isReconciled": is_reconciled,
            "requiresReview": requires_review,
            "defaultsApplied": defaults_applied,
            "warnings": sorted(set(warnings)),
            "reviewReasons": review_reasons,
            "notes": notes,
            "conflicts": conflicts,
            "duplicateGroups": duplicate_groups,
        },
    }


def create_payment_csv(result: dict) -> bytes:
    if result.get("quality", {}).get("requiresReview"):
        raise ValueError("A review-required remittance cannot produce a ready CSV")
    output = io.StringIO(newline="")
    writer = csv.writer(output, lineterminator="\n")
    for line in result.get("paymentLines", []):
        amount = _decimal(line.get("amountPaid"))
        if not line.get("proInvoiceNumber") or amount is None:
            raise ValueError("Validated payment line is missing its PRO or amount")
        writer.writerow([line["proInvoiceNumber"], format(amount.quantize(Decimal("0.01")), "f")])
    return output.getvalue().encode("utf-8")


def create_output_filename(metadata: dict) -> str:
    """Build the downstream file name: payer_date_total.csv.

    Fields are underscore-separated; payer is sanitized to [A-Za-z0-9-] so the name
    stays unambiguous. Uniqueness across remittances is provided by the blob path
    ({processingId}/{runId}/), not the file name.
    """

    def safe_piece(value, fallback: str) -> str:
        cleaned = re.sub(r"[^A-Za-z0-9]+", "-", str(value or "").strip()).strip("-")
        return (cleaned[:40] or fallback)

    remittance_date = str(metadata.get("remittanceDate") or "").strip()[:10]
    date_piece = remittance_date if re.fullmatch(r"\d{4}-\d{2}-\d{2}", remittance_date) else "NO-DATE"
    payment_total = _decimal(metadata.get("paymentTotal"))
    total_piece = format(payment_total.quantize(Decimal("0.01")), "f") if payment_total is not None else "NO-TOTAL"
    payer_piece = safe_piece(metadata.get("payerName"), "UNKNOWN-PAYER")
    return f"{payer_piece}_{date_piece}_{total_piece}.csv"