"""Deterministic triage of a review run into specific, owned issues.

A review run usually carries several unrelated problems. Sending them to the policy agent as one
bag of warning codes produced one generic answer ("confirm with payer") - including for problems
that are ours (extraction) or that only need a data fix. Triage splits them by owner:

  * extraction   - our extraction is suspect (lines not grounded in the source, failed analysis).
                   Resolved internally (re-extract / verify); never sent to the agent or the payer.
  * data_quality - values to normalize or correct from the source (PRO format, missing fields).
                   Resolved internally with a rules-based action.
  * payer        - questions about what the payer did (short pay, credits, duplicates, conflicts).
                   Sent to the policy agent, one issue at a time, with the exact lines involved.

Pure functions only (runs inside the Durable orchestrator).
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation

from resolution.resolver import DETAIL_TRIGGERS, LINE_TRIGGERS
from shared import pro_rules
from validation.remittance_validator import PER_PAYMENT_CODES  # expected when an email covers several payments

PAYER_CODES = DETAIL_TRIGGERS["payerName"]

MAX_ISSUE_LINES = 25

EXTRACTION_WARNINGS = {
    "content_understanding_analysis_failed": ("Document analysis failed", "Document analysis failed for a source document"),
    "content_understanding_not_configured": ("Extraction not configured", "Content Understanding is not configured"),
    "candidate_content_unavailable": ("Source content unavailable", "A source document could not be read"),
    "unsupported_content_type": ("Unsupported attachment", "An attachment type is not supported for extraction"),
    "no_payment_lines_found": ("No payment lines extracted", "No payment lines were extracted from any source"),
    "attachment_parse_warning": ("Attachment not fully parsed", "An attachment could not be fully parsed"),
    "unclassified_attachment": ("Unclassified attachment", "An attachment could not be classified as remittance or not"),
    "spreadsheet_total_mismatch": ("Spreadsheet lines don't match its Total", "A spreadsheet's payment lines do not sum to its own Total row"),
    "column_mapping_needs_review": (
        "Spreadsheet columns unverified",
        "The ACH Extraction Expert and the header rules chose different columns, and no total confirms either",
    ),
}

METADATA_WARNINGS = {
    "missing_payer_name": "Payer name missing",
    "payer_name_matches_payee": "Payer name is the payee's name",
    "missing_remittance_date": "Remittance date missing",
    "invalid_remittance_date": "Remittance date invalid",
    "unsupported_currency": "Currency is not USD",
    "invalid_payment_total": "Stated payment total unreadable",
    "negative_payment_total": "Stated payment total is negative",
    "conflicting_payerName": "Sources disagree on the payer",
    "conflicting_remittanceDate": "Sources disagree on the remittance date",
    "conflicting_paymentReference": "Sources disagree on the payment reference",
    "conflicting_currency": "Sources disagree on the currency",
}


def _pro_format() -> tuple[str, str]:
    """(format description, suffix hint) from the PRO_* settings, e.g. ("6-12 digit", " (often a suffix such as X1 or Y1)")."""
    rules = pro_rules.rules()
    examples = list(rules["suffixExamples"])
    hint = ""
    if examples:
        listed = examples[0] if len(examples) == 1 else ", ".join(examples[:-1]) + " or " + examples[-1]
        hint = f" (often a suffix such as {listed})"
    return f"{rules['minDigits']}-{rules['maxDigits']} digit", hint


def data_line_warnings() -> dict:
    """code -> (headline noun one/many, title noun one/many). Built per call: the PRO format is a setting."""
    pro_format, pro_hint = _pro_format()
    return {
        "invalid_pro_number": (
            ("PRO not in standard format", "PROs not in standard format"),
            (f"PRO number that does not match the standard {pro_format} format{pro_hint}",
             f"PRO numbers that do not match the standard {pro_format} format{pro_hint}"),
        ),
        "missing_pro_number": (("line missing a PRO", "lines missing a PRO"),
                               ("payment line with no PRO / invoice number", "payment lines with no PRO / invoice number")),
        "missing_or_invalid_amount_paid": (("line missing an amount", "lines missing an amount"),
                                           ("payment line with no readable amount paid", "payment lines with no readable amount paid")),
    }


def _decimal(value) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return Decimal(str(value).replace(",", "").replace("$", ""))
    except (InvalidOperation, ValueError):
        return None


def _count(n: int, one: str, many: str) -> str:
    """'1 negative line' / '7 negative lines'."""
    return f"{n} {one if n == 1 else many}"


def _money(value: Decimal) -> str:
    return f"${abs(value):,.2f}"


def _line_view(line: dict) -> dict:
    fields = ("proInvoiceNumber", "rawProInvoiceNumber", "amountPaid", "invoiceDate", "originalInvoiceAmount",
              "discountAmount", "adjustmentAmount", "lineReference")
    view = {field: line.get(field) for field in fields if line.get(field) not in (None, "")}
    if view.get("rawProInvoiceNumber") == view.get("proInvoiceNumber"):
        view.pop("rawProInvoiceNumber", None)
    return view


def _issue(issue_id, kind, code, headline, title, lines=(), amount=None) -> dict:
    lines = list(lines)
    return {
        "id": issue_id,
        "kind": kind,
        "code": code,
        "headline": headline,
        "title": title,
        "lineCount": len(lines),
        "amount": float(amount) if amount is not None else None,
        "lines": [_line_view(line) for line in lines[:MAX_ISSUE_LINES]],
        "linesTruncated": len(lines) > MAX_ISSUE_LINES,
    }


def _sum_amounts(lines) -> Decimal:
    return sum((_decimal(line.get("amountPaid")) or Decimal("0")) for line in lines)


def triage(final_result: dict) -> list[dict]:
    """Split a review run's warnings into owned issues (extraction, data_quality, payer)."""
    quality = final_result.get("quality") or {}
    warnings = set(quality.get("warnings") or [])
    lines = final_result.get("paymentLines") or []
    issues: list[dict] = []

    def with_warning(code, pool=lines):
        return [line for line in pool if code in (line.get("warnings") or [])]

    # --- extraction (ours) --------------------------------------------------------------------
    ungrounded = with_warning("line_not_grounded")
    if ungrounded:
        issues.append(_issue(
            "extraction:line_not_grounded", "extraction", "line_not_grounded",
            f"{_count(len(ungrounded), 'line', 'lines')} mis-paired by extraction",
            f"{_count(len(ungrounded), 'extracted PRO/amount pair does', 'extracted PRO/amount pairs do')} not appear together in the source document",
            ungrounded, _sum_amounts(ungrounded),
        ))
    for code, (headline, title) in EXTRACTION_WARNINGS.items():
        if code in warnings:
            issues.append(_issue(f"extraction:{code}", "extraction", code, headline, title))
    # Computed before the resolution finding: an unresolved attempt is not itself an extraction fault.
    extraction_suspect = any(issue["kind"] == "extraction" for issue in issues)
    resolution = final_result.get("resolution") or {}
    # status "confirmed": the expert agreed with the extracted lines - not an extraction problem; its
    # explanation is carried by the payer finding instead (see below).
    # Only a line problem makes an unresolved attempt an extraction matter; details-only runs keep
    # their data findings (with the expert's recommendation, below).
    triggers = resolution.get("triggers")
    line_problem = resolution.get("lineProblem", bool(set(triggers) & LINE_TRIGGERS) if triggers else True)
    if resolution.get("status") in ("rejected", "failed") and line_problem:
        # The ACH Resolution Expert tried; tell the analyst what it suggested and why it was not used.
        explanation = resolution.get("explanation") or ""
        reason = resolution.get("reason") or resolution.get("error") or resolution["status"]
        if reason == "drops_trusted_lines":
            title = ("ACH Resolution Expert suggested keeping only some of the valid payment lines; not applied - "
                     f"choosing between valid payments is the analyst's call. Its reasoning: {explanation}")
        elif reason == "expert_could_not_resolve":
            title = f"ACH Resolution Expert: {explanation}"
        else:
            title = f"ACH Resolution Expert suggestion not applied ({reason.replace('_', ' ')}). {explanation}".strip()
        issues.append(_issue(
            "extraction:resolution_unresolved", "extraction", "resolution_unresolved",
            "Resolution Expert could not resolve", title,
        ))

    # Lines our own extraction flagged are not evidence of what the payer did.
    trusted = [line for line in lines if "line_not_grounded" not in (line.get("warnings") or [])]

    # --- data quality (correct from source) ---------------------------------------------------
    for code, (noun, title) in data_line_warnings().items():
        flagged = with_warning(code, trusted)
        if flagged:
            issues.append(_issue(f"data:{code}", "data_quality", code, _count(len(flagged), *noun), _count(len(flagged), *title), flagged))
    # Several payments in one email each carry their own date, reference and amount, so the sources
    # disagreeing on those - and the lines exceeding the one stated total - are expected, not data to
    # correct: they are folded into the multiple-payments finding.
    several = "multiple_payments_in_email" in warnings
    folded = [code for code in PER_PAYMENT_CODES if code in warnings] if several else []
    recommended = ((resolution.get("details") or {}).get("recommended")) or {}
    for code, title in METADATA_WARNINGS.items():
        if code in warnings and code not in folded:
            issue = _issue(f"data:{code}", "data_quality", code, title, title)
            payer = recommended.get("payerName") if code in PAYER_CODES else None
            if payer:
                # The ACH Resolution Expert's answer, with its evidence: one decision for the analyst.
                issue["title"] = (f"{title}. ACH Resolution Expert recommends \"{payer['value']}\": {payer['reason']} "
                                  f"(evidence in {payer['sourceId']}: \"{payer['evidence']}\")")
                issue["headline"] = f"Confirm payer: {payer['value']}"
                issue["action"] = f"Confirm the payer is \"{payer['value']}\" and apply cash to that customer's account, then complete the run."
                issue["recommendation"] = {"field": "payerName", **payer}
            issues.append(issue)

    # --- payer (policy questions for the agent) ------------------------------------------------
    negative = with_warning("negative_amount_paid", trusted)
    if negative:
        total = _sum_amounts(negative)
        issues.append(_issue(
            "payer:negative_amount_paid", "payer", "negative_amount_paid",
            f"{_count(len(negative), 'negative line', 'negative lines')} ({_money(total)})",
            f"{_count(len(negative), 'payment line', 'payment lines')} with negative amounts totalling -{_money(total)} (credits, debit memos or deductions)",
            negative, total,
        ))
    zero = with_warning("zero_amount_paid", trusted)
    if zero:
        issues.append(_issue(
            "payer:zero_amount_paid", "payer", "zero_amount_paid",
            _count(len(zero), "zero-amount line", "zero-amount lines"), f"{_count(len(zero), 'payment line', 'payment lines')} paid at $0.00", zero,
        ))
    conflicted = {str(line.get("proInvoiceNumber")) for line in with_warning("conflicting_amount_for_pro", trusted)}
    if conflicted:
        group = [line for line in trusted if str(line.get("proInvoiceNumber")) in conflicted]
        issues.append(_issue(
            "payer:conflicting_payment_line", "payer", "conflicting_payment_line",
            f"{_count(len(conflicted), 'PRO', 'PROs')} paid with different amounts",
            f"{_count(len(conflicted), 'PRO number appears', 'PRO numbers appear')} on more than one line with different amounts",
            group, _sum_amounts(group),
        ))
    # The same PRO/amount in *different* sources is one payment corroborated by several documents (a
    # thread quoting the remittance, a proof and its email). Only a repeat within one source suggests
    # the payer paid twice.
    def _same_source(group):
        index = group.get("duplicateOfLineIndex")
        original = lines[index] if isinstance(index, int) and 0 <= index < len(lines) else {}
        return (original.get("source") or {}).get("candidateId") == group.get("duplicateCandidateId")

    duplicates = [group for group in quality.get("duplicateGroups") or [] if _same_source(group)]
    if duplicates:
        issues.append(_issue(
            "payer:duplicate_payment", "payer", "duplicate_payment",
            _count(len(duplicates), "duplicate PRO payment", "duplicate PRO payments"),
            f"{_count(len(duplicates), 'identical PRO/amount line was', 'identical PRO/amount lines were')} reported more than once across sources",
            [{"proInvoiceNumber": d.get("proInvoiceNumber"), "amountPaid": d.get("amountPaid")} for d in duplicates],
        ))
    groups = final_result.get("paymentGroups") or []
    confirmed = resolution.get("explanation") if resolution.get("status") == "confirmed" else None
    stated = _decimal((final_result.get("remittanceMetadata") or {}).get("paymentTotal"))
    per_payment = []
    if {"conflicting_remittanceDate", "conflicting_paymentReference"} & set(folded):
        per_payment.append("each payment has its own date and reference, so the sources disagree on them")
    if "payment_total_mismatch" in folded and stated is not None:
        per_payment.append(f"the stated total ({_money(stated)}) is one payment, not the email")
    per_payment_text = "; ".join(per_payment)
    per_payment_text = per_payment_text[:1].upper() + per_payment_text[1:]

    def several_title(text):
        parts = [text, per_payment_text, f"ACH Resolution Expert confirmed the lines: {confirmed}" if confirmed else ""]
        return ". ".join(p.rstrip(".") for p in parts if p) + "."

    if several and len(groups) > 1:
        amounts = [_decimal(g.get("amount")) or Decimal("0") for g in groups]
        named = ", ".join(
            f"{g.get('label') or 'payment'} {_money(a)}" for g, a in zip(groups, amounts)
        )
        # An invoice paid in parts across those payments (same PRO, different payment) - named, not a conflict.
        by_pro: dict[str, list[str]] = {}
        for line in lines:
            group = (line.get("source") or {}).get("paymentGroup")
            if group and line.get("proInvoiceNumber"):
                by_pro.setdefault(str(line["proInvoiceNumber"]), []).append(f"{group} {_money(_decimal(line.get('amountPaid')) or Decimal('0'))}")
        split = "; ".join(f"PRO {pro} paid in {' and '.join(parts)}" for pro, parts in by_pro.items() if len(parts) > 1)
        # Structural, not monetary: every dollar is accounted for, so there is no exception amount - the
        # issue is that one result/CSV covers several bank credits. The amounts stay in the title.
        issues.append(_issue(
            "payer:multiple_payments_in_email", "payer", "multiple_payments_in_email",
            f"{len(groups)} payments in one email ({_money(sum(amounts))})",
            several_title(
                f"The email covers {len(groups)} separate payments ({named}); the result and its CSV describe one payment"
                + (f". {split}" if split else "")
            ),
        ))
    elif several:
        # Recognized by thread classification or the ACH Resolution Expert rather than by totals.
        issues.append(_issue(
            "payer:multiple_payments_in_email", "payer", "multiple_payments_in_email",
            "Several payments in one email",
            several_title("The thread covers several separate payments (each with its own amount or date); the result and its CSV describe one payment"),
            lines,
        ))
    if "remittance_inquiry_thread" in warnings:
        issues.append(_issue(
            "payer:remittance_inquiry_thread", "payer", "remittance_inquiry_thread",
            "Collections inquiry or dispute thread",
            "The thread discusses payments (e.g. an unapplied or already-paid amount, or where to apply money) rather than simply remitting one",
            lines,
        ))
    if "no_remittance_in_email" in warnings:
        issues.append(_issue(
            "payer:no_remittance_in_email", "payer", "no_remittance_in_email",
            "No remittance detail in the email", "The email and its sources carry no payment information",
        ))
    if "conflicting_paymentTotal" in warnings and "conflicting_paymentTotal" not in folded:
        issues.append(_issue("payer:conflicting_paymentTotal", "payer", "conflicting_paymentTotal",
                             "Sources disagree on the payment total", "The email and attachments state different payment totals"))
    if "external_link_not_fetched" in warnings:
        issues.append(_issue("payer:external_link_not_fetched", "payer", "external_link_not_fetched",
                             "Remittance detail behind a link", "Remittance detail is only available behind a link that was not fetched"))
    difference = _decimal(quality.get("totalDifference"))
    if "payment_total_mismatch" in warnings and difference is not None and "payment_total_mismatch" not in folded:
        if extraction_suspect:
            # Our extraction is incomplete, so the gap is not (yet) a payer question.
            issues[0]["title"] += f"; the stated total is off from the extracted lines by {_money(difference)} until extraction is resolved"
        else:
            # Neutral wording: whether a gap is a short pay, an overpayment or several payments in one
            # email is the Assurance Expert's (or the analyst's) call, not triage's.
            over = difference > 0
            issues.append(_issue(
                "payer:payment_total_mismatch", "payer", "payment_total_mismatch",
                f"{'Payment exceeds lines' if over else 'Lines exceed payment'} by {_money(difference)}",
                f"The stated payment total is {_money(difference)} {'more' if over else 'less'} than the sum of the payment lines"
                + (f". ACH Resolution Expert confirmed the lines: {confirmed}" if confirmed else ""),
                amount=abs(difference),
            ))

    # Any other blocking warning still gets an owner rather than being dropped.
    known = {issue["code"] for issue in issues} | set(EXTRACTION_WARNINGS) | set(METADATA_WARNINGS) | set(data_line_warnings()) | {
        "payment_total_mismatch", "payment_total_not_provided", "line_not_grounded", "conflicting_amount_for_pro",
        "amount_normalized_from_edi_addenda", "payment_total_derived_from_subtotals", "remittance_date_derived_from_schedule",
        "spreadsheet_no_paid_amounts", "spreadsheet_not_payment_detail", "column_mapping_differs_from_rules",
        "column_mapping_unverified", "resolved_by_expert", "pro_paid_across_payments",
        "remittance_date_derived_from_subject", "remittance_date_resolved_by_expert", "payment_reference_resolved_by_expert",
    }
    for code in sorted(warnings - known):
        if code.startswith("conflicting_") and code.endswith("_within_candidate"):
            continue
        issues.append(_issue(f"data:{code}", "data_quality", code, code.replace("_", " ").capitalize(), code.replace("_", " ")))
    return issues


# Rules-based findings for the issues we own (extraction and data quality).
def _invalid_pro_action() -> str:
    examples = pro_rules.rules()["suffixExamples"]
    lead = f"Confirm the PRO suffix convention (e.g. {', '.join(examples)}), " if examples else "Confirm the PRO format, "
    return lead + "correct these PROs to the base PRO from the source, then re-validate."


_DATA_ACTIONS = {
    "missing_pro_number": "Fill the missing PROs from the source document where visible; otherwise request a corrected remittance from the payer.",
    "missing_or_invalid_amount_paid": "Fill the missing amounts from the source document where visible; otherwise request a corrected remittance from the payer.",
}


def rule_finding(issue: dict) -> dict:
    """Deterministic finding for an extraction or data-quality issue (no agent involved)."""
    if issue["kind"] == "extraction":
        decision = "verify_extraction"
        rationale = f"{issue['title']}. This is an extraction-integrity problem, not evidence that the payer's remittance is wrong."
        action = (
            "Reprocess the run, or verify the flagged lines against the source document before applying cash. "
            "Do not contact the payer about these lines."
            if issue["code"] == "line_not_grounded"
            else "Check the source document and reprocess the run; apply cash only from verified lines."
        )
    else:
        decision = "correct_data"
        rationale = issue["title"] if issue.get("recommendation") else f"{issue['title']}. Extracted values must only be corrected from source evidence."
        action = issue.get("action") or (
            _invalid_pro_action() if issue["code"] == "invalid_pro_number"
            else _DATA_ACTIONS.get(issue["code"], "Correct the value from the authoritative source document, then re-validate.")
        )
    return {
        "issueId": issue["id"],
        "kind": issue["kind"],
        "headline": issue["headline"],
        "title": issue["title"],
        "lineCount": issue["lineCount"],
        "lines": issue["lines"][:10],
        "source": "rules",
        "decision": decision,
        "category": issue["code"],
        "amountAtRisk": abs(issue["amount"]) if issue["amount"] is not None else None,
        "confidence": None,
        "rationale": rationale,
        "recommendedAction": action,
        "recommendation": issue.get("recommendation"),
        "citations": [],
    }
