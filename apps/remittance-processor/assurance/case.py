"""Build the compact, structured case the policy agent reasons over.

Only structured remittance facts are sent - never attachment bytes or Base64 content. The agent sees
only the *payer* issues from triage, each with the exact lines involved (capped), so it can answer
each one specifically. Issues we own (extraction, data quality) are listed as context only, so the
agent does not recommend contacting the payer about them.

One exception: for a collections inquiry / dispute thread the lines can't say what happened (a
failed or pending payment reconciles just as well as a paid one), so the case carries a short
excerpt of the email body - the newest message first - as ``threadExcerpt``.
"""

from __future__ import annotations

from assurance.triage import triage

# Plain-language reasons shown as "Sent to agents because" (gate reasons).
REVIEW_REASONS = {
    "payment_total_mismatch": "Stated payment total does not reconcile to the sum of payment lines",
    "payment_total_not_provided": "No stated payment total to reconcile against",
    "no_payment_lines_found": "No payment lines were extracted",
    "conflicting_payment_line": "The same PRO appears with different amounts",
    "negative_amount_paid": "A payment line has a negative amount (possible deduction or chargeback)",
    "zero_amount_paid": "A payment line has a zero amount",
    "missing_payer_name": "Payer name is missing",
    "missing_remittance_date": "Remittance date is missing",
    "external_link_not_fetched": "Remittance detail is behind a link that was not fetched",
    "content_understanding_analysis_failed": "Document analysis failed for a candidate",
    "invalid_pro_number": "Some PRO numbers are not in the standard format",
    "multiple_payments_in_email": "The email covers several separate payments",
    "remittance_inquiry_thread": "The thread is a collections inquiry or dispute, not a plain remittance",
    "no_remittance_in_email": "The email carries no payment information",
    "line_not_grounded": "Some extracted PRO/amount pairs do not appear together in the source document",
}


THREAD_EXCERPT_CHARS = 2000
_BODY_SOURCES = ("message_body", "embedded_message_body")


def thread_excerpt(extractions: list[dict]) -> str | None:
    """The email body's excerpt (already captured at extraction), the top-level body first."""
    for source_type in _BODY_SOURCES:
        for item in extractions or []:
            kind = item.get("sourceType") or (item.get("normalized") or {}).get("sourceType")
            if kind == source_type and item.get("excerpt"):
                return str(item["excerpt"])[:THREAD_EXCERPT_CHARS]
    return None


def build_case(final_result: dict, issues: list[dict] | None = None, *, excerpt: str | None = None) -> dict:
    metadata = final_result.get("remittanceMetadata") or {}
    quality = final_result.get("quality") or {}
    issues = triage(final_result) if issues is None else issues
    case = {
        "processingId": final_result.get("processingId"),
        "payerName": metadata.get("payerName"),
        "remittanceDate": metadata.get("remittanceDate"),
        "paymentReference": metadata.get("paymentReference"),
        "currency": metadata.get("currency"),
        "statedPaymentTotal": metadata.get("paymentTotal"),
        "calculatedPaymentTotal": quality.get("calculatedPaymentTotal"),
        "totalDifference": quality.get("totalDifference"),
        "isReconciled": quality.get("isReconciled"),
        "lineCount": quality.get("lineCount"),
        "issues": [
            {key: issue[key] for key in ("id", "code", "title", "lineCount", "amount", "lines", "linesTruncated")}
            for issue in issues
            if issue["kind"] == "payer"
        ],
        "handledInternally": [issue["title"] for issue in issues if issue["kind"] != "payer"],
    }
    if excerpt and any(issue["code"] == "remittance_inquiry_thread" for issue in case["issues"]):
        case["threadExcerpt"] = excerpt[:THREAD_EXCERPT_CHARS]
    return case
