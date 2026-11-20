from __future__ import annotations

import csv
import io
import re
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any


_HEADER_SCAN_ROWS = 25
_EXCLUDED_ROW = re.compile(r"\b(sub-?totals?|grand totals?|payment totals?|remittance totals?|totals?)\b", re.IGNORECASE)
_DUE_HEADER = re.compile(r"\b(due|balance|original|invoice total|gross)\b", re.IGNORECASE)
# Abbreviated forms (AR/ERP exports): "Tot Amt Pd", "Amt Pd", "Pd Amt", "Pmt Amt", "Amt Paid".
_PAID_HEADER = re.compile(
    r"\b(amount paid|paid amount|payment amount|amount applied|net paid|remit amount"
    r"|(?:tot(?:al)?\.?\s+)?amt\.?\s+(?:pd|paid)|(?:pd|paid)\.?\s+amt|pmt\.?\s+amt)\b",
    re.IGNORECASE,
)
_ID_HEADER = re.compile(
    r"\b(pro|invoice|freight bill|document (?:reference )?(?:number|no|#)|doc (?:ref|no|#))",
    re.IGNORECASE,
)
_NON_INVOICE_HEADER = re.compile(
    r"\b(po|load|bol|bill of lading|customer reference|your reference|payment reference|check|invoice total|invoice amount|amount due|balance)\b",
    re.IGNORECASE,
)


def _text(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _header_indexes(rows: list[list[Any]], scan_rows: int = _HEADER_SCAN_ROWS) -> tuple[int | None, int | None, int | None]:
    """(header row, PRO/invoice column, amount-paid column) by header meaning, or (None, None, None)."""
    best = None
    best_score = -1
    for row_index, row in enumerate(rows[:scan_rows]):
        headers = [_text(value) for value in row]
        invoice_indexes = [
            index
            for index, header in enumerate(headers)
            if _ID_HEADER.search(header) and not _NON_INVOICE_HEADER.search(header)
        ]
        paid_indexes = [
            index for index, header in enumerate(headers) if _PAID_HEADER.search(header)
        ]
        if not paid_indexes:
            paid_indexes = [
                index
                for index, header in enumerate(headers)
                if header
                and re.fullmatch(r"amount|paid|net", header, re.IGNORECASE)
                and not _DUE_HEADER.search(header)
            ]
        if invoice_indexes and paid_indexes:
            score = 10 * bool(_PAID_HEADER.search(headers[paid_indexes[0]])) + len(headers)
            if score > best_score:
                best = (row_index, invoice_indexes[0], paid_indexes[0])
                best_score = score
    if best:
        return best
    return None, None, None


# Shared with the HTML table parser so both detect columns by the same header rules.
find_header = _header_indexes
is_total_label = _EXCLUDED_ROW.search


def _parse_decimal_text(value: Any) -> str | None:
    text = _text(value)
    if not text or text.startswith("="):
        return None
    text = text.replace("$", "").replace(",", "").strip()
    if text.startswith("(") and text.endswith(")"):
        text = "-" + text[1:-1].strip()
    try:
        parsed = Decimal(text)
    except (InvalidOperation, ValueError):
        return None
    if not parsed.is_finite():
        return None
    return format(parsed, "f")


def _format_excel_identifier(value: Any, number_format: str | None = None) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return str(value)
    if isinstance(value, int):
        if number_format and re.fullmatch(r"0+", number_format):
            return str(value).zfill(len(number_format))
        return str(value)
    if isinstance(value, float) and value.is_integer():
        return _format_excel_identifier(int(value), number_format)
    return str(value).strip()


def _normalize_rows(
    rows: list[list[Any]],
    *,
    filename: str,
    worksheet: str | None = None,
    identifier_formats: dict[tuple[int, int], str] | None = None,
    cell_errors: set[tuple[int, int]] | None = None,
) -> dict:
    from extraction.column_mapping import resolve_columns

    mapping = resolve_columns(rows)
    header_row, invoice_column, amount_column = mapping["headerRow"], mapping["identifierColumn"], mapping["amountPaidColumn"]
    warnings: list[str] = []
    if mapping["source"] == "agent":
        # The ACH Extraction Expert maps any header wording (or no header); the row checks below still apply.
        if mapping["tableKind"] != "payment_detail" or invoice_column is None or amount_column is None:
            kind = "spreadsheet_no_paid_amounts" if mapping["tableKind"] == "statement_or_open_items" else "spreadsheet_not_payment_detail"
            columns = (_amount_columns(rows, header_row, invoice_column, worksheet, filename, identifier_formats or {})
                       if invoice_column is not None else [])
            return {"paymentLines": [], "warnings": [kind], "columnMapping": mapping, "amountColumns": columns}
        if header_row is None:
            header_row = -1
        if mapping["rulesAgree"] is False:
            warnings.append("column_mapping_differs_from_rules")
    if header_row is None:
        if rows and len(rows[0]) >= 2:
            # A first row with no numbers is an unrecognized header, not a payment line.
            first = [_text(value) for value in rows[0][:2]]
            looks_like_header = all(first) and not any(_parse_decimal_text(value) is not None for value in first)
            header_row, invoice_column, amount_column = (0 if looks_like_header else -1), 0, 1
            warnings.append("header_row_not_detected; first two columns used and require review")
        else:
            return {"paymentLines": [], "warnings": ["no usable invoice/amount columns found"]}
    else:
        # A sheet whose amount-paid column is empty on every row is a statement or open-items list
        # (amounts owed), not payment detail: it contributes no payment lines.
        data_rows = [
            row for row in rows[header_row + 1 :]
            if invoice_column < len(row) and _text(row[invoice_column]) and not _EXCLUDED_ROW.search(_text(row[invoice_column]))
        ]
        if data_rows and all(amount_column >= len(row) or not _text(row[amount_column]) for row in data_rows):
            return {"paymentLines": [], "warnings": ["spreadsheet_no_paid_amounts"], "columnMapping": mapping}

    lines = []
    totals = []
    total_rows: set[int] = set()
    cell_errors = cell_errors or set()
    identifier_formats = identifier_formats or {}
    for row_index, row in enumerate(rows):
        if row_index <= header_row:
            continue
        if invoice_column >= len(row) or amount_column >= len(row):
            continue
        invoice_number = _format_excel_identifier(
            row[invoice_column],
            identifier_formats.get((row_index, invoice_column)),
        )
        amount_text = _text(row[amount_column])
        if not invoice_number and not amount_text:
            continue
        if _EXCLUDED_ROW.search(invoice_number):
            warnings.append(f"row_{row_index + 1}:total_or_subtotal_row_skipped")
            totals.append(_parse_decimal_text(amount_text))
            total_rows.add(row_index)
            continue

        paid_amount = _parse_decimal_text(amount_text)
        if paid_amount is None and invoice_number and not re.search(r"\d", invoice_number):
            continue  # a label row (a repeated header such as "Invoice Number", a section title): no identifier, no amount
        if not invoice_number and paid_amount is not None and len(lines) >= 2:  # one line: can't tell a total from a line missing its PRO
            # No identifier, and the amount is exactly the sum of the lines above: the table's total, labelled
            # or not (a bare =SUM row). A repeat of it below (e.g. "Settled: =D30") is the same total.
            running = sum((Decimal(line["amountPaid"]) for line in lines if line["amountPaid"] is not None), Decimal("0"))
            if Decimal(paid_amount) == running:
                warnings.append(f"row_{row_index + 1}:total_or_subtotal_row_skipped")
                totals.append(paid_amount)
                total_rows.add(row_index)
                continue

        line_warnings = []
        if (row_index, invoice_column) in cell_errors or (row_index, amount_column) in cell_errors:
            line_warnings.append("spreadsheet_error_cell")
        if not invoice_number:
            line_warnings.append("missing_invoice_number")
        if paid_amount is None:
            line_warnings.append("missing_or_invalid_amount_paid")

        line = {
            "proInvoiceNumber": invoice_number or None,
            "amountPaid": paid_amount,
            "source": {
                "row": row_index + 1,
                "worksheet": worksheet,
                "documentName": filename,
            },
            "confidence": None,
        }
        if line_warnings:
            line["warnings"] = line_warnings
        lines.append(line)

    # One Total row in the paid column is the sheet's own proof of its amounts. With subtotals (several
    # total rows) the check is skipped rather than guessed; reconciliation to the stated total remains.
    verified = False
    totals = list(dict.fromkeys(totals))  # the same total repeated is one total, not subtotals
    if len(totals) == 1 and totals[0] is not None and lines:
        line_sum = sum((Decimal(line["amountPaid"]) for line in lines if line["amountPaid"] is not None), Decimal("0"))
        verified = line_sum == Decimal(totals[0])
        if not verified:
            warnings.append("spreadsheet_total_mismatch")
    # The agent and the frozen rules chose different columns, and nothing in the sheet confirms either:
    # flagged so the validator sends the run to review when the email states no total either.
    if mapping.get("rulesAgree") is False and not verified:
        warnings.append("column_mapping_unverified")

    return {"paymentLines": lines, "warnings": warnings, "columnMapping": mapping, "totalVerified": verified,
            "contextText": _context_text(rows, {line["source"]["row"] - 1 for line in lines}, header_row, total_rows)}


def _amount_columns(rows, header_row, id_col, worksheet, filename, formats) -> list[dict]:
    """Each fully numeric column of a table read as a statement, as candidate lines (identifier + that
    column's amount). Nothing is decided here: validation uses one only if it adds up exactly to the
    payment the email states (remittance_validator.validate_and_merge)."""
    start = (header_row if header_row is not None else -1) + 1
    data = []
    for index in range(max(start, 0), len(rows)):
        row = rows[index]
        identifier = _format_excel_identifier(row[id_col], formats.get((index, id_col))) if id_col < len(row) else ""
        if identifier and not _EXCLUDED_ROW.search(identifier):
            data.append((index, row, identifier))
    if len(data) < 2:
        return []
    header = rows[header_row] if header_row is not None and 0 <= header_row < len(rows) else []
    columns = []
    for column in range(max(len(row) for _, row, _ in data)):
        if column == id_col:
            continue
        amounts = [_parse_decimal_text(row[column]) if column < len(row) and not isinstance(row[column], (date, datetime)) else None
                   for _, row, _ in data]
        if any(amount is None for amount in amounts):
            continue
        columns.append({
            "column": column, "header": _text(header[column]) if column < len(header) else None, "worksheet": worksheet,
            "lines": [{"proInvoiceNumber": identifier, "amountPaid": amount, "confidence": None,
                       "source": {"row": index + 1, "worksheet": worksheet, "documentName": filename, "column": column}}
                      for (index, _, identifier), amount in zip(data, amounts)],
        })
    return columns


_CONTEXT_MAX_CHARS = 20000  # one Content Understanding call, never chunked


def _context_cell(value: Any) -> str:
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, float):
        return f"{value:.15g}"
    return _text(value)


def _context_text(rows: list[list[Any]], line_rows: set[int], header_row: int, total_rows: set[int]) -> str:
    """The sheet without its payment lines - title block, payment details, header and total rows - for
    Content Understanding to read the payer, date, total and reference. Empty when the sheet holds
    nothing beyond the table itself (no call is worth making)."""
    rendered = [(index, " | ".join(text for text in map(_context_cell, row) if text)) for index, row in enumerate(rows)
                if index not in line_rows]
    if not any(text for index, text in rendered if index != header_row and index not in total_rows):
        return ""
    return "\n".join(text for _, text in rendered if text)[:_CONTEXT_MAX_CHARS]


def parse_csv(content: bytes, filename: str) -> dict:
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = content.decode("cp1252", errors="replace")
    try:
        dialect = csv.Sniffer().sniff(text[:8192], delimiters=",;\t|")
    except csv.Error:
        dialect = csv.excel
    rows = list(csv.reader(io.StringIO(text), dialect))
    return _normalize_rows(rows, filename=filename)


def parse_xlsx(content: bytes, filename: str) -> dict:
    from openpyxl import load_workbook
    from openpyxl.cell.cell import TYPE_ERROR

    workbook = load_workbook(io.BytesIO(content), data_only=False, read_only=False)
    # Excel saves each formula's last computed value; read it rather than the formula text. A formula is
    # unresolved only when no value was saved (a file written by a tool that doesn't calculate).
    cached = load_workbook(io.BytesIO(content), data_only=True, read_only=False)
    all_lines = []
    warnings = []
    mappings = []
    contexts: list[tuple[str, str]] = []
    amount_columns: list[dict] = []
    for worksheet in workbook.worksheets:
        values_sheet = cached[worksheet.title]
        rows = []
        formats = {}
        errors = set()
        for row_index, row in enumerate(worksheet.iter_rows(), start=0):
            values = []
            for column_index, cell in enumerate(row):
                value = cell.value
                if isinstance(value, str) and value.startswith("="):
                    value = values_sheet[cell.coordinate].value
                    if isinstance(value, float):
                        value = float(f"{value:.15g}")  # Excel's 15-digit precision: =SUM's 0.30000000000000004 is 0.3
                    if value is None:
                        errors.add((row_index, column_index))
                        warnings.append(f"{worksheet.title}!{cell.coordinate}:formula_unresolved")
                if cell.data_type == TYPE_ERROR:
                    errors.add((row_index, column_index))
                    warnings.append(f"{worksheet.title}!{cell.coordinate}:spreadsheet_error")
                formats[(row_index, column_index)] = cell.number_format
                values.append(value)
            rows.append(values)
        parsed = _normalize_rows(
            rows,
            filename=filename,
            worksheet=worksheet.title,
            identifier_formats=formats,
            cell_errors=errors,
        )
        all_lines.extend(parsed["paymentLines"])
        warnings.extend(parsed["warnings"])
        if parsed.get("columnMapping"):
            mappings.append({"worksheet": worksheet.title, **parsed["columnMapping"]})
        contexts.append((worksheet.title, parsed.get("contextText") or ""))
        amount_columns.extend(parsed.get("amountColumns") or [])
    workbook.close()
    cached.close()
    return {"paymentLines": all_lines, "warnings": warnings, "columnMappings": mappings, "contextText": _join_contexts(contexts),
            "amountColumns": amount_columns}


def _join_contexts(contexts: list[tuple[str, str]]) -> str:
    present = [(name, text) for name, text in contexts if text]
    if len(present) == 1:
        return present[0][1]
    return "\n\n".join(f"[Worksheet: {name}]\n{text}" for name, text in present)[:_CONTEXT_MAX_CHARS]


def parse_xls(content: bytes, filename: str) -> dict:
    import xlrd

    # formatting_info loads each cell's number format (e.g. "000000000"), so leading zeros survive.
    # ignore_workbook_corruption: some reporting tools write a compound-file table xlrd's strict check
    # rejects ("Workbook corruption: seen[2] == 4") although Excel opens the file fine.
    workbook = xlrd.open_workbook(file_contents=content, formatting_info=True, ignore_workbook_corruption=True)
    all_lines = []
    warnings = []
    mappings = []
    contexts: list[tuple[str, str]] = []
    amount_columns: list[dict] = []
    for worksheet in workbook.sheets():
        rows = []
        formats = {}
        errors = set()
        for row_index in range(worksheet.nrows):
            values = []
            for column_index in range(worksheet.ncols):
                cell = worksheet.cell(row_index, column_index)
                value = cell.value
                if cell.ctype == xlrd.XL_CELL_ERROR:
                    errors.add((row_index, column_index))
                    warnings.append(f"{worksheet.name}!R{row_index + 1}C{column_index + 1}:spreadsheet_error")
                elif cell.ctype == xlrd.XL_CELL_NUMBER and float(value).is_integer() and cell.xf_index is not None:
                    xf = workbook.xf_list[cell.xf_index]
                    cell_format = workbook.format_map.get(xf.format_key)
                    if cell_format:
                        formats[(row_index, column_index)] = cell_format.format_str
                elif cell.ctype == xlrd.XL_CELL_DATE:
                    try:  # .xls stores dates as day serials
                        value = xlrd.xldate_as_datetime(value, workbook.datemode)
                    except (ValueError, OverflowError, xlrd.xldate.XLDateError):
                        pass
                values.append(value)
            rows.append(values)
        parsed = _normalize_rows(
            rows,
            filename=filename,
            worksheet=worksheet.name,
            identifier_formats=formats,
            cell_errors=errors,
        )
        all_lines.extend(parsed["paymentLines"])
        warnings.extend(parsed["warnings"])
        if parsed.get("columnMapping"):
            mappings.append({"worksheet": worksheet.name, **parsed["columnMapping"]})
        contexts.append((worksheet.name, parsed.get("contextText") or ""))
        amount_columns.extend(parsed.get("amountColumns") or [])
    return {"paymentLines": all_lines, "warnings": warnings, "columnMappings": mappings, "contextText": _join_contexts(contexts),
            "amountColumns": amount_columns}


_ZIP_SIGNATURE = b"PK\x03\x04"  # .xlsx (Office Open XML)
_OLE_SIGNATURE = bytes.fromhex("d0cf11e0a1b11ae1")  # .xls (Excel 97-2003)


def parse_spreadsheet(content: bytes, filename: str) -> dict:
    extension = Path(filename).suffix.lower()
    if extension == ".csv":
        return parse_csv(content, filename)
    # The extension is the sender's label; the bytes decide (ERP exports often name a legacy .xls ".xlsx").
    if extension in (".xlsx", ".xls") and content.startswith(_ZIP_SIGNATURE):
        return parse_xlsx(content, filename)
    if extension in (".xlsx", ".xls") and content.startswith(_OLE_SIGNATURE):
        return parse_xls(content, filename)
    if extension == ".xlsx":
        return parse_xlsx(content, filename)
    if extension == ".xls":
        return parse_xls(content, filename)
    raise ValueError(f"Unsupported spreadsheet format: {extension or '(missing extension)'}")