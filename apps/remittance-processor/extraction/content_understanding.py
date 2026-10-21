from __future__ import annotations

import bisect
import concurrent.futures
import json
import re
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any

from shared import usage


_METADATA_FIELDS = (
    "payerName",
    "remittanceDate",
    "paymentTotal",
    "currency",
    "paymentReference",
)
_LINE_FIELDS = (
    "proInvoiceNumber",
    "amountPaid",
    "invoiceDate",
    "originalInvoiceAmount",
    "discountAmount",
    "adjustmentAmount",
    "lineReference",
)
_VALUE_KEYS = (
    "valueString",
    "valueNumber",
    "valueDate",
    "valueInteger",
    "valueBoolean",
    "valueArray",
    "valueObject",
)


def _plain_value(field: Any) -> Any:
    if not isinstance(field, dict):
        return field
    for key in _VALUE_KEYS:
        if key in field:
            return field[key]
    return field.get("value")


def _field_record(field: Any) -> tuple[Any, dict]:
    if not isinstance(field, dict):
        return field, {}
    value = _plain_value(field)
    evidence = {}
    if field.get("confidence") is not None:
        evidence["confidence"] = field["confidence"]
    if field.get("source") is not None:
        evidence["source"] = field["source"]
    return value, evidence


def _analysis_field_sets(raw_result: dict) -> list[dict]:
    result = raw_result.get("result", raw_result)
    contents = result.get("contents") or []
    field_sets = [
        content["fields"]
        for content in contents
        if isinstance(content, dict) and isinstance(content.get("fields"), dict)
    ]
    if field_sets:
        return field_sets
    fields = result.get("fields")
    return [fields] if isinstance(fields, dict) else []


def _normalize_payment_line(line: Any, candidate_id: str, content_index: int) -> dict:
    if isinstance(line, dict) and "valueObject" in line:
        line = line["valueObject"]
    if not isinstance(line, dict):
        return {
            "proInvoiceNumber": None,
            "amountPaid": None,
            "source": {"candidateId": candidate_id, "contentIndex": content_index},
            "warnings": ["payment line was not a structured object"],
        }

    normalized = {}
    evidence = {}
    for field_name in _LINE_FIELDS:
        value, field_evidence = _field_record(line.get(field_name))
        normalized[field_name] = value
        if field_evidence:
            evidence[field_name] = field_evidence

    source = {"candidateId": candidate_id, "contentIndex": content_index}
    for field_name, field_evidence in evidence.items():
        if field_evidence.get("source"):
            source.setdefault("fieldSources", {})[field_name] = field_evidence["source"]
        if field_evidence.get("confidence") is not None:
            source.setdefault("fieldConfidence", {})[field_name] = field_evidence["confidence"]
    normalized["source"] = source
    normalized["confidence"] = min(
        source.get("fieldConfidence", {}).values(),
        default=None,
    )
    _apply_edi_implied_decimal(normalized)
    return normalized


_RMR_TOKEN_SPLIT = re.compile(r"[*:>~\\]")


def _apply_edi_implied_decimal(line: dict) -> None:
    """X12 RMR remittance addenda encode money with no decimal point (123450 means $1,234.50). When a
    line amount was lifted verbatim from an RMR segment as a whole number, apply the implied 2 decimals
    so it is read as dollars, not as a 100x-inflated figure. Recorded with a warning so it is auditable.
    """
    reference = str(line.get("lineReference") or "")
    if "RMR" not in reference.upper():
        return
    tokens = {token for token in _RMR_TOKEN_SPLIT.split(reference) if token}
    corrected = False
    for field in ("amountPaid", "originalInvoiceAmount"):
        value = line.get(field)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            continue
        if float(value) != int(value):
            continue  # already carries cents - not a bare implied-decimal integer
        if str(int(value)) in tokens:
            line[field] = round(value / 100, 2)
            corrected = True
    if corrected:
        line.setdefault("warnings", []).append("amount_normalized_from_edi_addenda")


# An RMR segment as it appears in the source text: "RMR*IV*78000001201*123450***123450****\" (ends at
# the segment terminator, whitespace or markup).
_RMR_SEGMENT_RE = re.compile(r"RMR\*[^\s<>|~\\]*", re.IGNORECASE)


def _apply_edi_implied_decimal_from_source(lines: list[dict], markdown: str) -> None:
    """The same implied-decimal rule, read from the source text instead of the line's reference: the
    analyzer doesn't always copy the RMR segment into ``lineReference`` (it may pick another id), and
    then a 100x amount would stand. Converted only when one RMR segment in the source holds both the
    line's PRO and its whole-number amount."""
    if not markdown or "RMR" not in markdown.upper():
        return
    segments = [{token for token in _RMR_TOKEN_SPLIT.split(match.group(0)) if token} for match in _RMR_SEGMENT_RE.finditer(markdown)]
    for line in lines:
        if "amount_normalized_from_edi_addenda" in (line.get("warnings") or []):
            continue
        pro = str(line.get("proInvoiceNumber") or "").strip()
        if not pro:
            continue
        corrected = False
        for field in ("amountPaid", "originalInvoiceAmount"):
            value = line.get(field)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or float(value) != int(value):
                continue
            if any(pro in tokens and str(int(value)) in tokens for tokens in segments):
                line[field] = round(value / 100, 2)
                corrected = True
        if corrected:
            line.setdefault("warnings", []).append("amount_normalized_from_edi_addenda")


# Some payers send a single email covering several sub-batches (e.g. per region), each with its own
# subtotal and scheduled date, and no document-level total or header date. The flat analyzer schema has
# no slot for those, so Content Understanding returns an empty paymentTotal/remittanceDate. When that
# happens we recover both from the markdown text so the run can still reconcile, recording a derived
# source + a non-blocking warning so the inference is auditable rather than silent.
_MONEY_RE = re.compile(r"\$?\s*([\d,]+\.\d{2})")
_TOTAL_LABEL_RE = re.compile(r"(?i)^[\s*|>_#\-]*(?:sub[-\s]*)?total\b")
_SCHEDULED_RE = re.compile(r"(?i)scheduled\s+for\s+(\d{1,2})/(\d{1,2})/(\d{2,4})")


def _markdown_text(contents: list) -> str:
    return "\n".join(
        content["markdown"]
        for content in contents
        if isinstance(content, dict) and isinstance(content.get("markdown"), str)
    )


def _first_money(text: str) -> Decimal | None:
    match = _MONEY_RE.search(text)
    if not match:
        return None
    try:
        return Decimal(match.group(1).replace(",", ""))
    except InvalidOperation:
        return None


def _sum_subtotals(markdown: str) -> tuple[float | None, int]:
    """Sum each 'Total ...' subtotal row (amount on the same or the next line)."""
    lines = markdown.splitlines()
    total = Decimal("0")
    count = 0
    for index, line in enumerate(lines):
        if not _TOTAL_LABEL_RE.match(line):
            continue
        amount = _first_money(line)
        look = index
        while amount is None and look + 1 < len(lines) and look - index < 2:
            look += 1
            amount = _first_money(lines[look])
        if amount is not None:
            total += amount
            count += 1
    return (float(total), count) if count else (None, 0)


def _earliest_scheduled_date(markdown: str) -> str | None:
    """Return the earliest 'Scheduled for M/D/YY' date as ISO, or None."""
    found = []
    for month, day, year in _SCHEDULED_RE.findall(markdown):
        resolved_year = int(year)
        if resolved_year < 100:
            resolved_year += 2000
        try:
            found.append(date(resolved_year, int(month), int(day)))
        except ValueError:
            continue
    return min(found).isoformat() if found else None


def _recover_missing_metadata(metadata: dict, metadata_evidence: dict, warnings: list, markdown: str) -> None:
    """Backfill an empty paymentTotal/remittanceDate from markdown subtotals/scheduled dates."""
    if not markdown:
        return
    if not (metadata.get("paymentTotal") is not None and str(metadata["paymentTotal"]).strip()):
        derived_total, subtotal_count = _sum_subtotals(markdown)
        if derived_total is not None:
            metadata["paymentTotal"] = derived_total
            metadata_evidence["paymentTotal"] = {"confidence": 0.5, "source": f"derived:sum_of_{subtotal_count}_subtotals"}
            warnings.append("payment_total_derived_from_subtotals")
    if not (metadata.get("remittanceDate") is not None and str(metadata["remittanceDate"]).strip()):
        derived_date = _earliest_scheduled_date(markdown)
        if derived_date is not None:
            metadata["remittanceDate"] = derived_date
            metadata_evidence["remittanceDate"] = {"confidence": 0.5, "source": "derived:earliest_scheduled_date"}
            warnings.append("remittance_date_derived_from_schedule")


# Grounding: a generative extractor can pair a PRO with the neighbouring row's amount. A shifted list
# keeps (nearly) the same total, so reconciliation cannot catch it. Each extracted line is checked
# against the analyzed markdown: its amount must appear on the PRO's own text line, or after the PRO
# and before the next row identifier. Rows lay out identifier-first in tables, so an amount that only
# appears before its PRO is the previous row's - flagged, never silently accepted.
_AMOUNT_TOKEN_RE = re.compile(r"(?<![\w/.,])\(?-?\$?\s?(\d{1,3}(?:,\d{3})+|\d+)(\.\d{1,2})?\)?(?![\w/])")
_GROUND_WINDOW_CHARS = 600


def _amounts_in(text: str) -> set[Decimal]:
    found = set()
    for match in _AMOUNT_TOKEN_RE.finditer(text):
        try:
            found.add(Decimal(match.group(1).replace(",", "") + (match.group(2) or "")).quantize(Decimal("0.01")))
        except InvalidOperation:
            continue
    return found


def _line_amount_candidates(line: dict) -> set[Decimal]:
    try:
        amount = abs(Decimal(str(line.get("amountPaid")).replace(",", "").replace("$", ""))).quantize(Decimal("0.01"))
    except (InvalidOperation, ValueError):
        return set()
    values = {amount}
    if "amount_normalized_from_edi_addenda" in (line.get("warnings") or []):
        values.add((amount * 100).quantize(Decimal("0.01")))  # source carries the implied-decimal integer
    return values


def ground_lines(lines: list[dict], markdown: str) -> int:
    """Mark each line ``source.grounded``; add ``line_not_grounded`` when its PRO/amount pair is not
    in the source. Returns the number of ungrounded lines. Lines without a PRO or amount are skipped
    (they are already flagged as missing)."""
    if not markdown or not lines:
        return 0
    occurrences: dict[str, list[int]] = {}
    for line in lines:
        pro = str(line.get("proInvoiceNumber") or "").strip()
        if pro and pro not in occurrences:
            positions, start = [], markdown.find(pro)
            while start != -1:
                # whole token only: "1234567" must not match inside "01234567"
                before = markdown[start - 1] if start else " "
                after = markdown[start + len(pro)] if start + len(pro) < len(markdown) else " "
                if not before.isalnum() and not after.isalnum():
                    positions.append(start)
                start = markdown.find(pro, start + 1)
            occurrences[pro] = positions
    row_starts = sorted({pos for positions in occurrences.values() for pos in positions})

    ungrounded = 0
    for line in lines:
        pro = str(line.get("proInvoiceNumber") or "").strip()
        expected = _line_amount_candidates(line)
        if not pro or not expected:
            continue
        # Same-shaped identifiers also end the row, so a row the extractor skipped cannot lend its amount.
        shape = re.compile(rf"(?<![\w.,]){'[0-9]' if pro.isdigit() else '[A-Za-z0-9]'}{{{len(pro)}}}(?![\w.,])")
        grounded = False
        for pos in occurrences.get(pro, []):
            end = pos + len(pro)
            line_start = markdown.rfind("\n", 0, pos) + 1
            line_end = markdown.find("\n", end)
            same_line = markdown[line_start : line_end if line_end != -1 else len(markdown)]
            next_index = bisect.bisect_right(row_starts, pos)
            next_row = min(row_starts[next_index] if next_index < len(row_starts) else len(markdown), end + _GROUND_WINDOW_CHARS)
            window = markdown[end:next_row]
            next_shape = shape.search(window) if len(pro) >= 6 else None  # short ids look like amounts
            if next_shape:
                window = window[: next_shape.start()]
            if expected & (_amounts_in(same_line.replace(pro, " ")) | _amounts_in(window)):
                grounded = True
                break
        line.setdefault("source", {})["grounded"] = grounded
        if not grounded:
            ungrounded += 1
            line.setdefault("warnings", []).append("line_not_grounded")
    return ungrounded


def normalize_analysis(raw_result: dict, *, candidate_id: str, source_type: str) -> dict:
    field_sets = _analysis_field_sets(raw_result)
    metadata = {field_name: None for field_name in _METADATA_FIELDS}
    metadata_evidence = {}
    warnings = []
    for field_name in _METADATA_FIELDS:
        values = []
        for fields in field_sets:
            value, evidence = _field_record(fields.get(field_name))
            if value is not None and str(value).strip():
                values.append((value, evidence))
        if values:
            values.sort(
                key=lambda pair: float(pair[1].get("confidence", -1) or -1),
                reverse=True,
            )
            metadata[field_name] = values[0][0]
            if values[0][1]:
                metadata_evidence[field_name] = values[0][1]
            if len({str(value) for value, _ in values}) > 1:
                warnings.append(f"conflicting_{field_name}_within_candidate")

    lines = []
    for content_index, fields in enumerate(field_sets):
        raw_lines = fields.get("paymentLines")
        line_values = _plain_value(raw_lines) or []
        if not isinstance(line_values, list):
            if raw_lines is not None:
                warnings.append("analyzer paymentLines value was not an array")
            continue
        lines.extend(
            _normalize_payment_line(line, candidate_id, content_index)
            for line in line_values
        )

    result = raw_result.get("result", raw_result)
    markdown = _markdown_text(result.get("contents") or [])
    _recover_missing_metadata(metadata, metadata_evidence, warnings, markdown)
    _apply_edi_implied_decimal_from_source(lines, markdown)
    ungrounded = ground_lines(lines, markdown)

    return {
        "candidateId": candidate_id,
        "sourceType": source_type,
        "remittanceMetadata": metadata,
        "metadataEvidence": metadata_evidence,
        "paymentLines": lines,
        "warnings": warnings,
        "ungroundedLineCount": ungrounded,
    }


def analyze_binary_candidate(
    *,
    endpoint: str,
    analyzer_id: str,
    content: bytes,
    content_type: str,
    candidate_id: str,
    source_type: str,
    client=None,
) -> dict:
    """Analyze one source candidate and return both raw and normalized results, plus the billing
    usage the operation reported (pages, contextualization and model tokens; None if absent)."""
    credential = None
    if client is None:
        from azure.ai.contentunderstanding import ContentUnderstandingClient
        from azure.identity import DefaultAzureCredential

        credential = DefaultAzureCredential()
        client = ContentUnderstandingClient(
            endpoint=endpoint,
            credential=credential,
            api_version=_API_VERSION,
        )

    poller = client.begin_analyze_binary(
        analyzer_id,
        content,
        content_type=content_type,
    )
    operation_id = getattr(poller, "operation_id", None)
    result = poller.result()
    if hasattr(result, "as_dict"):
        raw_result = result.as_dict()
    elif isinstance(result, dict):
        raw_result = result
    else:
        raw_result = dict(result)

    return {
        "operationId": operation_id,
        "normalized": normalize_analysis(
            raw_result,
            candidate_id=candidate_id,
            source_type=source_type,
        ),
        "raw": raw_result,
        "usage": usage.cu_from_operation(_operation_usage(poller, endpoint, operation_id, credential)),
    }


_API_VERSION = "2025-11-01"


def _operation_usage(poller, endpoint: str, operation_id: str | None, credential) -> dict | None:
    """The analyze operation's ``usage`` - billing only, never fails the analysis. The SDK returns just
    the result, so read the operation envelope from the poller's final response, else GET the finished
    operation by id (documented; no charge)."""
    try:
        envelope = poller.polling_method()._pipeline_response.http_response.json()
        if isinstance(envelope, dict) and isinstance(envelope.get("usage"), dict):
            return envelope["usage"]
    except Exception:
        pass
    if not (operation_id and credential and endpoint):
        return None
    try:
        import urllib.request

        token = credential.get_token("https://cognitiveservices.azure.com/.default").token
        request = urllib.request.Request(
            f"{endpoint.rstrip('/')}/contentunderstanding/analyzerResults/{operation_id}?api-version={_API_VERSION}",
            headers={"Authorization": f"Bearer {token}"},
        )
        with urllib.request.urlopen(request, timeout=30) as response:
            return (json.loads(response.read()) or {}).get("usage")
    except Exception:
        return None


# A single Content Understanding pass fails on very large bodies (a 150-line, 117 KB remittance comes
# back empty). Rather than add format-specific parsers, we chunk oversized *text* candidates into
# digestible pieces that CU already handles across formats, then merge. Chunks overlap by a few lines
# so a row split across a boundary is whole in at least one chunk; the merge de-duplicates.
_CHUNK_MAX_CHARS = 20000
_CHUNK_OVERLAP_LINES = 15
_CHUNK_MAX_PARALLEL = 4  # bounded so chunk fan-out does not blow past the CU rate limit


def chunk_text_for_analysis(text: str, max_chars: int = _CHUNK_MAX_CHARS, overlap_lines: int = _CHUNK_OVERLAP_LINES) -> list[str]:
    """Split text into line-aligned chunks <= max_chars, each carrying the last few lines of the
    previous chunk so a line-item row straddling a boundary appears intact in at least one chunk."""
    if len(text) <= max_chars:
        return [text]
    lines = text.splitlines(keepends=True)
    chunks: list[str] = []
    current: list[str] = []
    size = 0
    for line in lines:
        if size + len(line) > max_chars and current:
            chunks.append("".join(current))
            current = current[-overlap_lines:] if overlap_lines else []
            size = sum(len(item) for item in current)
        current.append(line)
        size += len(line)
    if current:
        chunks.append("".join(current))
    return chunks


def _shared_prefix(previous_chunk: str, chunk: str) -> str:
    """The overlap lines a chunk repeats from the end of the previous chunk."""
    previous_lines = previous_chunk.splitlines(keepends=True)
    lines = chunk.splitlines(keepends=True)
    for count in range(min(len(previous_lines), len(lines), _CHUNK_OVERLAP_LINES * 4), 0, -1):
        if lines[:count] == previous_lines[-count:]:
            return "".join(lines[:count])
    return ""


def _amount_key(line: dict):
    value = line.get("amountPaid")
    if value in (None, ""):
        return None
    try:
        return Decimal(str(value).replace(",", "").replace("$", "")).quantize(Decimal("0.01"))
    except (InvalidOperation, ValueError):
        return None


def _better_copy(candidate: dict, existing: dict) -> bool:
    """Prefer the copy that carries an amount, then the one grounded in its source."""
    if _amount_key(existing) is None and _amount_key(candidate) is not None:
        return True
    return (existing.get("source") or {}).get("grounded") is False and (candidate.get("source") or {}).get("grounded") is True


def merge_normalized_analyses(
    parts: list[dict], *, candidate_id: str, source_type: str, overlaps: list[str] | None = None
) -> dict:
    """Merge per-chunk normalized analyses: first non-empty metadata per field, unioned warnings, and
    every payment line in document order.

    Only the chunk overlap is de-duplicated: a line is dropped when it repeats a line of the
    *previous* chunk (same PRO, same amount or one side missing) and - when ``overlaps`` is given -
    its PRO sits in the text this chunk shares with the previous one. A PRO legitimately paid on
    several rows (partial payments, adjustments) is therefore kept, not collapsed to one row."""
    metadata = {field_name: None for field_name in _METADATA_FIELDS}
    metadata_evidence: dict = {}
    warnings: list[str] = []
    merged_lines: list[dict] = []
    previous: dict[str, list[int]] = {}
    for part_index, part in enumerate(parts):
        part_metadata = part.get("remittanceMetadata") or {}
        for field_name in _METADATA_FIELDS:
            if (metadata.get(field_name) in (None, "")) and str(part_metadata.get(field_name) or "").strip():
                metadata[field_name] = part_metadata[field_name]
                evidence = (part.get("metadataEvidence") or {}).get(field_name)
                if evidence:
                    metadata_evidence[field_name] = evidence
        for warning in part.get("warnings", []):
            if warning not in warnings:
                warnings.append(warning)
        overlap = overlaps[part_index] if overlaps and part_index < len(overlaps) else None
        current: dict[str, list[int]] = {}
        claimed: set[int] = set()
        for line in part.get("paymentLines", []):
            pro = str(line.get("proInvoiceNumber") or "").strip()
            match = None
            if pro and part_index > 0 and (overlap is None or pro in overlap):
                amount = _amount_key(line)
                for index in previous.get(pro, []):
                    other = _amount_key(merged_lines[index])
                    if index not in claimed and (amount is None or other is None or amount == other):
                        match = index
                        break
            if match is not None:
                claimed.add(match)
                if _better_copy(line, merged_lines[match]):
                    merged_lines[match] = line
                current.setdefault(pro, []).append(match)
                continue
            merged_lines.append(line)
            if pro:
                current.setdefault(pro, []).append(len(merged_lines) - 1)
        previous = current
    ungrounded = sum(1 for line in merged_lines if (line.get("source") or {}).get("grounded") is False)
    return {
        "candidateId": candidate_id,
        "sourceType": source_type,
        "remittanceMetadata": metadata,
        "metadataEvidence": metadata_evidence,
        "paymentLines": merged_lines,
        "warnings": warnings,
        "ungroundedLineCount": ungrounded,
    }


def analyze_candidate_content(
    *,
    analyze,
    endpoint: str,
    analyzer_id: str,
    content: bytes,
    content_type: str,
    candidate_id: str,
    source_type: str,
    max_chars: int = _CHUNK_MAX_CHARS,
    max_parallel: int = _CHUNK_MAX_PARALLEL,
) -> dict:
    """Analyze a candidate, chunking oversized text so CU gets digestible input, then merging the
    per-chunk results. Non-text or small content is analyzed in a single pass, unchanged. Chunks are
    analyzed concurrently (bounded) and a failed chunk degrades to an empty part rather than failing
    the whole candidate."""
    is_text = (content_type or "").lower().startswith("text")
    if not is_text or len(content) <= max_chars:
        return analyze(
            endpoint=endpoint, analyzer_id=analyzer_id, content=content, content_type=content_type,
            candidate_id=candidate_id, source_type=source_type,
        )
    chunks = chunk_text_for_analysis(content.decode("utf-8", errors="replace"), max_chars)

    def run_chunk(chunk: str) -> dict:
        try:
            return analyze(
                endpoint=endpoint, analyzer_id=analyzer_id, content=chunk.encode("utf-8"), content_type=content_type,
                candidate_id=candidate_id, source_type=source_type,
            )
        except Exception as exc:  # one bad chunk should not lose the rest of the extraction
            return {
                "operationId": None,
                "normalized": {"remittanceMetadata": {}, "metadataEvidence": {}, "paymentLines": [], "warnings": [f"chunk_extraction_failed_{type(exc).__name__}"]},
                "raw": {"error": type(exc).__name__},
            }

    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, min(max_parallel, len(chunks)))) as pool:
        analyses = list(pool.map(run_chunk, chunks))  # map preserves order, so the header chunk stays first
    overlaps = [""] + [_shared_prefix(chunks[i - 1], chunks[i]) for i in range(1, len(chunks))]
    merged = merge_normalized_analyses(
        [a["normalized"] for a in analyses], candidate_id=candidate_id, source_type=source_type, overlaps=overlaps
    )
    merged["chunkCount"] = len(chunks)  # recorded for audit, not surfaced as a review warning
    return {"operationId": None, "normalized": merged, "raw": {"chunked": True, "chunkCount": len(chunks), "parts": [a.get("raw") for a in analyses]},
            "usage": usage.merge(*(a.get("usage") for a in analyses)) or None}  # every chunk is billed