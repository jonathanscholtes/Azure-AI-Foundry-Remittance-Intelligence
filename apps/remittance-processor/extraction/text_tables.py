"""Deterministic, self-verifying extraction of a payment table that a mail client flattened to plain
text - one cell per line, in fixed column order (how Outlook renders an HTML table in the text body).

Reading the MSG's HTML body would need its compressed-RTF body de-encapsulated, which can take
minutes on large remittances, so this works from the plain-text body we already have.

Not per-payer logic. A table is used only when it proves itself:
  * a run of consecutive label lines names a PRO/invoice/document column and an amount-paid column
    (the same header rules as the spreadsheet parser); its length is the column count;
  * every record that follows has an identifier, a parseable amount, and the same value type in every
    other column as the first record (a dropped or extra cell shifts the types, so misalignment fails);
  * the records end at a Total row whose amount-paid value equals the sum of the lines exactly.
Anything else returns ``accepted=False`` and the caller falls back to Content Understanding.
"""

from __future__ import annotations

import re
from decimal import Decimal

from extraction.column_mapping import resolve_columns
from extraction.spreadsheet_parser import _parse_decimal_text, is_total_label

_DATE_RE = re.compile(r"^(\d{1,2}[/-]\d{1,2}[/-]\d{2,4}|\d{4}-\d{2}-\d{2})$")
_MAX_COLUMNS = 15


def _cell_type(value: str) -> str:
    if _DATE_RE.match(value):
        return "date"
    if _parse_decimal_text(value) is not None:
        return "number"
    return "text"


def _is_label(value: str) -> bool:
    return bool(value) and not re.search(r"\d", value) and len(value) <= 60


def _header_blocks(cells: list[str]):
    """Yield (start, width, id_col, paid_col) for label runs that read as a payment-table header."""
    index = 0
    while index < len(cells):
        if not _is_label(cells[index]):
            index += 1
            continue
        run_end = index
        while run_end < len(cells) and _is_label(cells[run_end]):
            run_end += 1
        # The run may start with title lines ("Remittance Detail"). The real column count is the width at
        # which the following values repeat with a consistent type per column - decided structurally,
        # so the column mapping is asked about a correctly aligned header and sample rows.
        for start in range(index, run_end):
            width = run_end - start
            if not 2 <= width <= _MAX_COLUMNS:
                continue
            records = _leading_records(cells, run_end, width)
            if not _aligned(records, width):
                continue
            mapping = _map_columns(cells[start:run_end], records)
            if mapping is not None:
                yield start, width, mapping[0], mapping[1], mapping[2]
            break
        index = run_end


_ALIGN_RECORDS = 3


def _leading_records(cells: list[str], position: int, width: int) -> list[list[str]]:
    """Up to three complete records after a header, stopping at a Total row (a sub-table may hold one)."""
    records = []
    while len(records) < _ALIGN_RECORDS and position + width <= len(cells):
        record = cells[position : position + width]
        if any(is_total_label(value) and not re.search(r"\d{4,}", value) for value in record):
            break
        records.append(record)
        position += width
    return records


def _aligned(records: list[list[str]], width: int) -> bool:
    """True when the leading records carry an amount and keep one value type per column."""
    if not records or any(len(record) < width for record in records):
        return False
    signatures = [[_cell_type(value) for value in record] for record in records]
    if not any(kind == "number" for kind in signatures[0]):
        return False
    # The identifier column may vary (digits vs. a suffix), so compare all but the most varied column.
    columns = list(zip(*signatures))
    mismatched = sum(1 for column in columns if len(set(column)) > 1)
    return mismatched <= 1


def _map_columns(header: list[str], records: list[list[str]]) -> tuple[int, int, str] | None:
    """(identifier column, amount-paid column, "agent" | "rules") for an aligned header, from the ACH
    Extraction Expert or the header rules."""
    mapping = resolve_columns([header] + records, scan_rows=1)
    if mapping["identifierColumn"] is None or mapping["amountPaidColumn"] is None:
        return None
    if mapping["tableKind"] not in (None, "payment_detail"):
        return None
    return mapping["identifierColumn"], mapping["amountPaidColumn"], mapping["source"]


def _read_records(cells: list[str], start: int, width: int, id_col: int, paid_col: int) -> dict:
    position = start + width
    lines, record_spans, signature, total = [], [], None, None
    while position < len(cells):
        if is_total_label(cells[position]) and not re.search(r"\d{4,}", cells[position]):
            # Total row: a label then the trailing numeric cells, right-aligned to the columns.
            numbers = []
            look = position + 1
            while look < len(cells) and _cell_type(cells[look]) == "number" and len(numbers) < width:
                numbers.append(cells[look])
                look += 1
            from_right = width - paid_col
            if len(numbers) >= from_right:
                total = _parse_decimal_text(numbers[-from_right])
            return {"lines": lines, "spans": record_spans, "total": total, "totalLabel": cells[position], "end": look}
        record = cells[position : position + width]
        if len(record) < width:
            break
        identifier, amount = record[id_col], _parse_decimal_text(record[paid_col])
        if not re.search(r"\d", identifier) or amount is None:
            return {"reason": f"unparseable_record_at_cell_{position}"}
        types = [_cell_type(value) if column != id_col else "id" for column, value in enumerate(record)]
        if signature is None:
            signature = types
        elif types != signature:
            return {"reason": f"column_types_shifted_at_cell_{position}"}
        lines.append(
            {
                "proInvoiceNumber": identifier,
                "amountPaid": amount,
                "source": {"method": "text_table", "record": len(lines) + 1},
                "confidence": None,
            }
        )
        record_spans.append((position, position + width))
        position += width
    return {"reason": "no_total_row"}


def extract_text_payment_tables(text: str | None) -> dict:
    """Return accepted payment lines from a cell-per-line table in ``text`` plus a condensed text for
    CU (the records removed, header and Total kept). ``accepted`` is False when no table proves itself
    or when any payment-looking table fails its checks."""
    if not text:
        return {"accepted": False, "tables": []}
    raw_lines = text.splitlines()
    cell_line = [number for number, line in enumerate(raw_lines) if line.strip()]
    cells = [raw_lines[number].strip() for number in cell_line]

    summaries, accepted, elided = [], [], set()
    consumed_until = -1
    for start, width, id_col, paid_col, mapped_by in _header_blocks(cells):
        if start < consumed_until:
            continue
        result = _read_records(cells, start, width, id_col, paid_col)
        if "reason" in result or not result["lines"]:
            # A header mapped as payment detail that cannot be read: never skip it and accept the rest
            # (that would silently drop its lines) - send the whole body to Content Understanding.
            summaries.append({"headerCell": start, "columns": cells[start : start + width], "accepted": False,
                              "reason": result.get("reason", "no_records")})
            return {"accepted": False, "tables": summaries}
        line_sum = sum(Decimal(line["amountPaid"]) for line in result["lines"])
        summary = {"headerCell": start, "columns": cells[start : start + width], "records": len(result["lines"]),
                   "tableTotal": result["total"], "totalLabel": result.get("totalLabel"), "lineSum": format(line_sum, "f"),
                   "mappedBy": mapped_by}
        if result["total"] is None or Decimal(result["total"]) != line_sum:
            summaries.append({**summary, "accepted": False, "reason": "table_total_mismatch"})
            return {"accepted": False, "tables": summaries}
        summaries.append({**summary, "accepted": True})
        # Each sub-table that closes with its own Total is one payment; tag its lines so the same PRO
        # in two payments (an invoice paid in parts) is not mistaken for a conflict.
        for line in result["lines"]:
            line["source"]["paymentGroup"] = result.get("totalLabel") or f"table at line {start}"
        accepted.extend(result["lines"])
        for first, last in result["spans"]:
            elided.update(cell_line[first:last])
        consumed_until = result["end"]

    if not accepted:
        return {"accepted": False, "tables": summaries}
    kept, noted = [], False
    for number, line in enumerate(raw_lines):
        if number in elided:
            if not noted:
                kept.append(f"[{len(accepted)} payment detail rows were read directly from the table and are omitted here.]")
                noted = True
            continue
        kept.append(line)
    condensed = "\n".join(line for line in kept if line.strip())
    return {"accepted": True, "paymentLines": accepted, "condensedText": condensed, "tables": summaries}
