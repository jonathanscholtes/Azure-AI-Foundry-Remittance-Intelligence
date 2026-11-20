"""Deterministic, self-verifying extraction of a remittance written as a text report of stacked
records - each payment takes several lines instead of one table row:

    00900000101        PO 4410021         $312.40      record line: identifier first, an amount last
    Adjustment         $27.35                          0..n modifier lines: a label, then an amount
    $339.75                                           net line: an amount alone - the amount paid

Not per-payer logic. The layout is used only when it proves itself:
  * every record line is followed by its modifier lines and a net line, and no amount line sits
    loose between records (a record that breaks the shape rejects the whole body - never skip it
    and accept the rest, which would silently drop its line);
  * every record balances: its amount plus its modifiers equals its net, to the cent;
  * the nets sum exactly to the remittance total the body states ("Total Amount: $X"), and every
    total it states agrees.
Anything else returns ``accepted=False`` and the caller falls back to Content Understanding.
"""

from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation

_AMOUNT = r"\(?-?\$?\s?-?\d[\d,]*\.\d{2}\)?"
# Identifier: one token with a digit and no amount punctuation ("00900000101", "INV-20431").
_RECORD_RE = re.compile(rf"^(?P<id>(?=[A-Za-z0-9-]*\d)[A-Za-z0-9][A-Za-z0-9-]{{4,}})\s+(?:(?P<ref>.*?)\s+)?(?P<amount>{_AMOUNT})$")
# Modifier: a label whose first word has no digit ("Adjustment", "Check 50000001"), then an amount.
_MODIFIER_RE = re.compile(rf"^(?P<label>[A-Za-z][^\s\d]*(?:\s+\S+)*?)\s*:?\s+(?P<amount>{_AMOUNT})$")
_NET_RE = re.compile(rf"^(?P<amount>{_AMOUNT})$")
_STATED_TOTAL_RE = re.compile(
    rf"(?i)^(?:total(?:\s+(?:amount|payment|paid|remitted))?|(?:payment|check|remittance|eft|ach)\s+(?:amount|total)"
    rf"|amount\s+(?:paid|remitted))\s*:?\s*(?P<amount>{_AMOUNT})$"
)
_MIN_RECORDS = 2


def _money(text: str) -> Decimal | None:
    negative = "(" in text or "-" in text
    try:
        value = Decimal(re.sub(r"[^\d.]", "", text))
    except InvalidOperation:
        return None
    return -value if negative else value


def _reject(reason: str, **summary) -> dict:
    return {"accepted": False, "tables": [{"layout": "stacked_records", "accepted": False, "reason": reason, **summary}]}


def _read_records(cells: list[tuple[int, str]]) -> tuple[list[dict], str | None]:
    """Records in ``cells`` ((raw line number, stripped text) pairs), or the reason the shape broke."""
    records, index = [], 0
    while index < len(cells):
        match = _RECORD_RE.match(cells[index][1])
        if not match:
            index += 1
            continue
        start, modifiers = index, []
        index += 1
        while index < len(cells) and not _NET_RE.match(cells[index][1]):
            modifier = _MODIFIER_RE.match(cells[index][1])
            if not modifier or _STATED_TOTAL_RE.match(cells[index][1]):
                break
            modifiers.append(_money(modifier.group("amount")))
            index += 1
        net = _NET_RE.match(cells[index][1]) if index < len(cells) else None
        if not net:
            return records, f"record_without_net_line_at_line_{cells[start][0] + 1}"
        gross, paid = _money(match.group("amount")), _money(net.group("amount"))
        if gross + sum(modifiers, Decimal("0")) != paid:
            return records, f"record_does_not_balance_at_line_{cells[start][0] + 1}"
        records.append({
            "id": match.group("id"),
            "reference": (match.group("ref") or "").strip() or None,
            "gross": gross,
            "net": paid,
            "modified": bool(modifiers),
            "span": (start, index),
        })
        index += 1
    return records, None


def extract_stacked_records(text: str | None) -> dict:
    """Return accepted payment lines from a stacked-record text report in ``text`` plus a condensed
    text for CU (the records removed, everything else kept). ``accepted`` is False when the body is
    not this layout or the layout fails any of its checks."""
    if not text:
        return {"accepted": False, "tables": []}
    raw_lines = text.splitlines()
    cells = [(number, line.strip()) for number, line in enumerate(raw_lines) if line.strip()]

    records, reason = _read_records(cells)
    if reason:
        return _reject(reason, records=len(records))
    if len(records) < _MIN_RECORDS:
        return {"accepted": False, "tables": []}  # not this layout

    in_record = set()
    for start, end in (record["span"] for record in records):
        in_record.update(range(start, end + 1))
    first, last = records[0]["span"][0], records[-1]["span"][1]
    for index in range(first, last + 1):
        if index not in in_record and (_NET_RE.match(cells[index][1]) or _MODIFIER_RE.match(cells[index][1])):
            return _reject(f"unattached_amount_at_line_{cells[index][0] + 1}", records=len(records))

    stated = {_money(m.group("amount")) for _, line in cells if (m := _STATED_TOTAL_RE.match(line))}
    line_sum = sum((record["net"] for record in records), Decimal("0"))
    summary = {"records": len(records), "lineSum": format(line_sum, "f"),
               "statedTotal": format(next(iter(stated)), "f") if len(stated) == 1 else None}
    if not stated:
        return _reject("no_stated_total", **summary)
    if len(stated) > 1:
        return _reject("conflicting_stated_totals", **summary)
    if next(iter(stated)) != line_sum:
        return _reject("stated_total_mismatch", **summary)

    lines = [
        {
            "proInvoiceNumber": record["id"],
            "amountPaid": format(record["net"], "f"),
            "originalInvoiceAmount": float(record["gross"]),
            "adjustmentAmount": float(record["net"] - record["gross"]) if record["modified"] else None,
            "lineReference": record["reference"],
            "source": {"method": "stacked_records", "record": number},
            "confidence": None,
        }
        for number, record in enumerate(records, start=1)
    ]
    elided = {cells[index][0] for index in in_record}
    kept, noted = [], False
    for number, line in enumerate(raw_lines):
        if number in elided:
            if not noted:
                kept.append(f"[{len(lines)} payment detail records were read directly from the text and are omitted here.]")
                noted = True
            continue
        kept.append(line)
    condensed = "\n".join(line for line in kept if line.strip())
    return {"accepted": True, "paymentLines": lines, "condensedText": condensed,
            "tables": [{"layout": "stacked_records", "accepted": True, **summary}]}
