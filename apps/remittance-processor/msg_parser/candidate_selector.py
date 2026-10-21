from __future__ import annotations

import re
from pathlib import Path

from shared import pro_rules


BUSINESS_EXTENSIONS = {
    ".pdf",
    ".xlsx",
    ".xls",
    ".docx",
    ".doc",
    ".docm",
    ".xlsm",
    ".ods",
    ".odt",
    ".rtf",
    ".csv",
    ".txt",
    ".png",
    ".jpg",
    ".jpeg",
    ".tif",
    ".tiff",
    ".bmp",
    ".heic",
    ".heif",
    ".txt",
    ".docx",
    ".doc",
    ".docm",
    ".xlsm",
    ".ods",
    ".odt",
    ".rtf",
}
CONTENT_UNDERSTANDING_EXTENSIONS = {
    ".pdf",
    ".docx",
    ".doc",
    ".png",
    ".jpg",
    ".jpeg",
    ".tif",
    ".tiff",
    ".bmp",
    ".heic",
    ".heif",
}
REMITTANCE_TERMS = re.compile(r"remit|payment|ach|invoice|detail|statement", re.IGNORECASE)
LOW_VALUE_ASSET_NAME = re.compile(r"logo|signature|icon|tracking|pixel|facebook|twitter|linkedin", re.IGNORECASE)


def _meaningful_body(body: str | None) -> bool:
    if not body:
        return False
    lines = [line.strip() for line in body.splitlines() if line.strip()]
    meaningful = [line for line in lines if not line.lower().startswith("sent from my ")]
    return bool(meaningful and sum(map(len, meaningful)) >= 20)


def _strategy(extension: str, source_type: str) -> str:
    if extension in CONTENT_UNDERSTANDING_EXTENSIONS or source_type == "message_body":
        return "content_understanding"
    if extension in BUSINESS_EXTENSIONS:
        return "deterministic_parser"
    return "manual_review"


def select_candidates(
    candidates: list[dict], *, subject: str | None = None, body: str | None = None
) -> dict:
    """Score candidates transparently and retain duplicates and low-value assets."""
    scored: list[dict] = []
    seen_hashes: set[str] = set()
    for candidate in candidates:
        item = dict(candidate)
        item.setdefault("sourceType", "attachment")
        name = str(item.get("name") or "")
        extension = str(item.get("extension") or Path(name).suffix).lower()
        item["extension"] = extension
        reasons: list[str] = []
        score = 0

        if extension in BUSINESS_EXTENSIONS:
            score += 40
            reasons.append("Supported business-document type")
        if REMITTANCE_TERMS.search(name):
            score += 40
            reasons.append("Filename indicates remittance or payment content")
        if not item.get("isInline", False):
            score += 20
            reasons.append("Non-inline source")
        if int(item.get("size", 0)) >= 1024:
            score += 10
            reasons.append("Substantive file size")
        if subject and REMITTANCE_TERMS.search(subject):
            score += 10
            reasons.append("Message subject indicates remittance or payment content")
        if body and REMITTANCE_TERMS.search(body):
            score += 10
            reasons.append("Message body indicates remittance or payment content")

        digest = item.get("sha256")
        is_small_inline_image = (
            item.get("isInline", False)
            and extension in {".png", ".jpg", ".jpeg", ".tif", ".tiff"}
            and int(item.get("size", 0)) < 4_096
        )
        # Inline GIF/BMP images are decorative (logos, signatures, spacers), never remittance documents,
        # so retain them for audit without flagging an "unrecognized attachment".
        is_inline_decorative_image = item.get("isInline", False) and extension in {".gif", ".bmp"}
        is_named_low_value_asset = bool(LOW_VALUE_ASSET_NAME.search(name))
        if digest and digest in seen_hashes:
            item["classification"] = "duplicate_candidate"
            reasons.append("Duplicate content hash")
        elif is_small_inline_image or is_inline_decorative_image or is_named_low_value_asset:
            item["classification"] = "ignored_candidate"
            reasons.append("Likely inline or decorative asset; retained for audit")
        else:
            item["classification"] = (
                "remittance_candidate" if score >= 40 else "unclassified_candidate"
            )
            if digest:
                seen_hashes.add(digest)

        item["score"] = score
        item["selectionReasons"] = reasons
        scored.append(item)

    pro = pro_rules.rules()
    subject_reference = bool(
        subject
        and (
            REMITTANCE_TERMS.search(subject)
            or re.search(rf"\b\d{{{pro['minDigits']},{pro['maxDigits']}}}[A-Za-z]{{0,2}}\b", subject)
        )
    )
    if _meaningful_body(body) or subject_reference:
        body_reasons = (
            ["Message body contains meaningful non-signature text"]
            if _meaningful_body(body)
            else ["Message subject contains a remittance or invoice reference"]
        )
        candidate_text = f"Subject: {subject or ''}\n\n{body or ''}"
        scored.append(
            {
                "candidateId": "message-body",
                "sourceType": "message_body",
                "name": "message-body.txt",
                "extension": ".txt",
                "contentType": "text/plain",
                "size": len(candidate_text.encode("utf-8")),
                "isInline": False,
                "classification": "remittance_candidate",
                "score": 30 + (10 if REMITTANCE_TERMS.search(subject or "") else 0),
                "selectionReasons": body_reasons,
            }
        )

    eligible = [candidate for candidate in scored if candidate["classification"] == "remittance_candidate"]
    selected = max(eligible, key=lambda candidate: candidate["score"], default=None)
    strategy = _strategy(selected["extension"], selected["sourceType"]) if selected else "manual_review"
    return {
        "candidates": scored,
        "recommendedProcessing": {
            "primaryCandidateId": selected.get("candidateId") if selected else None,
            "strategy": strategy,
            "requiresMultipleCandidateAnalysis": len(eligible) > 1,
        },
    }