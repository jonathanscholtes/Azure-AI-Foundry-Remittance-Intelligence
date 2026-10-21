"""Deterministic, self-verifying extraction of payment-detail tables from an HTML email body.

Content Understanding is good at variety but unreliable on long, regular tables: on a 1,662-row
remittance it silently truncated chunks and paired PROs with the neighbouring row's amount. When the
email body carries a real HTML table, the rows are already structured, so we read them directly.

This is NOT per-payer logic. A table is used only when it proves itself:
  * its header row names a PRO/invoice/document column and an amount-paid column (the same header
    rules as the spreadsheet parser), and
  * every data row has an identifier and a parseable amount, and
  * when the table has a Total row, the parsed lines sum to it exactly.
Anything else returns ``accepted=False`` and the caller falls back to Content Understanding.

When a table is accepted, ``condensedText`` is the body rendered row-per-line with that table's data
rows elided, so Content Understanding still reads the metadata (payer, date, total, reference) without
re-extracting the rows.
"""

from __future__ import annotations

import re
from decimal import Decimal
from html.parser import HTMLParser

from extraction.column_mapping import resolve_columns
from extraction.spreadsheet_parser import _parse_decimal_text, is_total_label

_BLOCK_TAGS = {"br", "p", "div", "li", "h1", "h2", "h3", "h4", "h5", "h6"}
_SUPPRESSED_TAGS = {"script", "style", "head", "title"}
_HEADER_SCAN_ROWS = 40


class _Table:
    def __init__(self, index: int):
        self.index = index
        self.rows: list[dict] = []  # {"cells": [str], "start": int, "end": int}


class _HtmlTableParser(HTMLParser):
    """Renders the body as text (one table row per line, cells joined by " | ") and keeps each table's
    rows as cell lists, with the character span of every row in the rendered text."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.out: list[str] = []
        self.size = 0
        self.tables: list[_Table] = []
        self._table_stack: list[_Table] = []
        self._row: dict | None = None
        self._row_stack: list[dict | None] = []
        self._cell: list[str] | None = None
        self._cell_span = 1
        self._cell_stack: list[tuple[list[str] | None, int]] = []
        self._suppressed: str | None = None

    def _emit(self, text: str) -> None:
        self.out.append(text)
        self.size += len(text)

    def handle_starttag(self, tag, attrs):
        if self._suppressed:
            return
        if tag in _SUPPRESSED_TAGS:
            self._suppressed = tag
        elif tag == "table":
            table = _Table(len(self.tables))
            self.tables.append(table)
            self._table_stack.append(table)
            self._row_stack.append(self._row)
            self._cell_stack.append((self._cell, self._cell_span))
            self._row, self._cell = None, None
            self._emit("\n")
        elif tag == "tr" and self._table_stack:
            self._emit("\n")
            self._row = {"cells": [], "start": self.size}
        elif tag in ("td", "th") and self._row is not None:
            if self._row["cells"]:
                self._emit(" | ")
            self._cell = []
            try:
                self._cell_span = max(1, min(50, int(dict(attrs).get("colspan") or 1)))
            except ValueError:
                self._cell_span = 1
        elif tag in _BLOCK_TAGS:
            # Inside a cell a paragraph break must not split the row's line.
            self._emit(" " if self._cell is not None else "\n")

    def handle_endtag(self, tag):
        if self._suppressed:
            if tag == self._suppressed:
                self._suppressed = None
            return
        if tag in ("td", "th") and self._row is not None and self._cell is not None:
            self._row["cells"].append(" ".join("".join(self._cell).split()))
            self._row["cells"].extend([""] * (self._cell_span - 1))
            self._cell = None
        elif tag == "tr" and self._row is not None and self._table_stack:
            self._row["end"] = self.size
            self._table_stack[-1].rows.append(self._row)
            self._row = None
            self._emit("\n")
        elif tag == "table" and self._table_stack:
            self._table_stack.pop()
            self._row = self._row_stack.pop()
            self._cell, self._cell_span = self._cell_stack.pop()
            self._emit("\n")
        elif tag in ("p", "div", "li") and self._cell is None:
            self._emit("\n")

    def handle_data(self, data):
        if self._suppressed:
            return
        if self._cell is not None:
            self._cell.append(data)
        self._emit(data)


def _clean_text(text: str) -> str:
    lines = []
    for line in text.splitlines():
        line = " ".join(line.split()).strip(" |")
        if line:
            lines.append(line)
    return "\n".join(lines)


def _read_table(table: _Table) -> dict:
    """Parse one table into payment lines, or explain why it cannot be trusted."""
    rows = [row["cells"] for row in table.rows]
    # Layout tables (one row, or no numbers at all) are not payment tables - skip without a model call.
    if len(rows) < 2 or not any(_parse_decimal_text(cell) is not None for row in rows for cell in row):
        return {"accepted": False, "reason": "no_payment_header"}
    mapping = resolve_columns(rows, scan_rows=_HEADER_SCAN_ROWS)
    header_row, id_col, paid_col = mapping["headerRow"], mapping["identifierColumn"], mapping["amountPaidColumn"]
    if id_col is None or paid_col is None or mapping["tableKind"] not in (None, "payment_detail"):
        return {"accepted": False, "reason": "no_payment_header"}
    if header_row is None:
        header_row = -1  # the model found a headerless payment table

    lines, data_rows, total, total_label = [], [], None, None
    for row_index in range(header_row + 1, len(rows)):
        cells = rows[row_index]
        identifier = cells[id_col].strip() if id_col < len(cells) else ""
        amount_text = cells[paid_col].strip() if paid_col < len(cells) else ""
        if not any(cell.strip() for cell in cells):
            continue
        if any(is_total_label(cell) for cell in cells[: paid_col]) and not re.search(r"\d{4,}", identifier):
            total = _parse_decimal_text(amount_text)
            total_label = next((cell for cell in cells[:paid_col] if is_total_label(cell)), None)
            continue
        amount = _parse_decimal_text(amount_text)
        if not identifier or amount is None:
            return {"accepted": False, "reason": f"unparseable_row_{row_index + 1}"}
        lines.append(
            {
                "proInvoiceNumber": identifier,
                "amountPaid": amount,
                "source": {"method": "html_table", "table": table.index, "row": row_index + 1},
                "confidence": None,
            }
        )
        data_rows.append(row_index)

    if not lines:
        return {"accepted": False, "reason": "no_data_rows"}
    line_sum = sum(Decimal(line["amountPaid"]) for line in lines)
    if total is not None and Decimal(total) != line_sum:
        return {"accepted": False, "reason": "table_total_mismatch", "tableTotal": total, "lineSum": format(line_sum, "f")}
    if total is None and mapping.get("rulesAgree") is False:
        # The agent and the frozen rules disagree and no Total row settles it: don't trust the table.
        return {"accepted": False, "reason": "column_mapping_unverified", "lineSum": format(line_sum, "f")}
    # This table (closed by its own Total, if any) is one payment: tag its lines with it.
    for line in lines:
        line["source"]["paymentGroup"] = total_label or f"table {table.index}"
    return {
        "accepted": True,
        "paymentLines": lines,
        "dataRows": data_rows,
        "tableTotal": total,
        "totalLabel": total_label,
        "lineSum": format(line_sum, "f"),
        "columns": {"header": rows[header_row] if header_row >= 0 else [], "identifier": id_col, "amountPaid": paid_col,
                    "mappedBy": mapping["source"], "rulesAgree": mapping.get("rulesAgree")},
    }


def extract_html_payment_tables(html: str | None, *, prefix: str = "") -> dict:
    """Return accepted payment lines from the body's HTML tables plus a condensed text for CU.

    ``accepted`` is True only when at least one table passed every check and no payment-looking
    table failed - a partially trusted body goes to Content Understanding as a whole.
    """
    if not html:
        return {"accepted": False, "tables": []}
    parser = _HtmlTableParser()
    parser.feed(html)
    parser.close()
    rendered = "".join(parser.out)

    summaries, accepted, elide = [], [], []
    for table in parser.tables:
        result = _read_table(table)
        if result.get("reason") == "no_payment_header":
            continue  # layout or metadata table, not payment detail
        summary = {"table": table.index, "rows": len(table.rows), **{k: v for k, v in result.items() if k not in ("paymentLines", "dataRows")}}
        summaries.append(summary)
        if not result["accepted"]:
            return {"accepted": False, "tables": summaries}
        accepted.extend(result["paymentLines"])
        elide.append((table, result["dataRows"], result["columns"]["header"]))

    if not accepted:
        return {"accepted": False, "tables": summaries}

    # Render the body with the accepted tables' data rows removed (header and Total rows stay).
    spans = sorted((table.rows[i]["start"], table.rows[i]["end"]) for table, rows, _ in elide for i in rows)
    pieces, cursor = [], 0
    for start, end in spans:
        pieces.append(rendered[cursor:start])
        cursor = end
    pieces.append(rendered[cursor:])
    note = f"[{len(accepted)} payment detail rows were read directly from the HTML table and are omitted here.]"
    condensed = _clean_text("".join(pieces))
    for _, _, header in elide:
        header_line = " ".join(" | ".join(header).split()).strip(" |")
        if header_line and header_line in condensed:
            condensed = condensed.replace(header_line, f"{header_line}\n{note}", 1)
            note = ""
    if note:
        condensed = f"{condensed}\n{note}"
    return {
        "accepted": True,
        "paymentLines": accepted,
        "condensedText": f"{prefix}{condensed}".strip(),
        "tables": summaries,
    }
