"""Column mapping for payment tables by the ACH Extraction Expert (Foundry agent): the agent reads,
the code verifies.

Header wording varies without end ("Amount Paid", "Tot Amt Pd", "Importe pagado", no header at all),
so maintaining synonym patterns means a code change for every new layout. Instead, the ACH Extraction
Expert (agents/extraction_agent.py, deployed to Foundry) is asked once per table: which row is the
header, which column is the PRO / invoice, which is the amount actually paid, and is this payment
detail or a statement of amounts owed. Its instructions and output schema live in Foundry. The table
readers apply the answer to every row in code and keep their checks (every row parses, column types
stay consistent, rows sum to the table's own Total), so a wrong answer is caught, not published.

The original header patterns stay frozen as the fallback when the agent is not configured or fails,
and as a cross-check (``rulesAgree``). New layouts are handled by the agent - do not add synonyms.

Setting: EXTRACTION_AGENT_NAME (with FOUNDRY_PROJECT_ENDPOINT). Unset -> header rules.
"""

from __future__ import annotations

import datetime
import hashlib
import json
import logging
import re
import threading
import time

from shared.foundry_agents import agent_configured, invoke_agent, parse_agent_json

logger = logging.getLogger("remittance_intake")

AGENT_SETTING = "EXTRACTION_AGENT_NAME"
MAX_PROMPT_ROWS = 12
MAX_TAIL_ROWS = 3
MAX_COLUMNS = 60
MAX_CELL_CHARS = 40
TABLE_KINDS = ("payment_detail", "statement_or_open_items", "other")
CACHE_TTL_SECONDS = 3600

_cache: dict[str, dict] = {}
_cache_lock = threading.Lock()

# Replaceable for tests and for the golden runner's response cache: (agent, prompt) -> (text, annotations).
invoke = invoke_agent


def is_configured() -> bool:
    return agent_configured(AGENT_SETTING) is not None


def _prompt_rows(rows: list[list]) -> list[dict]:
    """The first rows (header and samples) and the last few (a Total row), cells trimmed."""
    picked = list(range(min(len(rows), MAX_PROMPT_ROWS)))
    picked += [i for i in range(max(len(rows) - MAX_TAIL_ROWS, 0), len(rows)) if i not in picked]
    return [
        {"index": i, "cells": [str(cell if cell is not None else "")[:MAX_CELL_CHARS] for cell in rows[i][:MAX_COLUMNS]]}
        for i in picked
    ]


def _valid(answer: dict, rows: list[list]) -> bool:
    width = max((len(row) for row in rows), default=0)
    header = answer.get("headerRow")
    columns = [answer.get("identifierColumn"), answer.get("amountPaidColumn")]
    return (
        answer.get("tableKind") in TABLE_KINDS
        and (header is None or (isinstance(header, int) and 0 <= header < len(rows)))
        and all(c is None or (isinstance(c, int) and 0 <= c < width) for c in columns)
    )


_DATE_RE = re.compile(r"^(\d{1,2}[/-]\d{1,2}[/-]\d{2,4}|\d{4}-\d{2}-\d{2})")


def _cell_kind(cell) -> str:
    """A cell's layout role: its text when it is a label, else only its value type."""
    from extraction.spreadsheet_parser import _parse_decimal_text

    text = str(cell if cell is not None else "").strip()
    if not text:
        return "<empty>"
    if _DATE_RE.match(text) or isinstance(cell, (datetime.date, datetime.datetime)):
        return "<date>"
    if _parse_decimal_text(text) is not None:
        return "<number>"
    if re.search(r"\d", text):
        return "<code>"  # identifiers with letters or separators (03700000201-X1)
    return " ".join(text.lower().split())[:MAX_CELL_CHARS]


def layout_key(agent: str, rows: list[list]) -> tuple[str, list]:
    """Cache key from the table's *layout*, never its values or payer: label text, column count and
    each data cell's value type, with repeated record layouts collapsed. A changed format (renamed,
    added, moved columns, new date style) is a new key; identical layouts share one agent answer.
    Returns (key, the layout rows) - the header row's layout is re-checked on a cache hit."""
    layout = []
    for row in rows[:MAX_PROMPT_ROWS]:
        signature = [_cell_kind(cell) for cell in row[:MAX_COLUMNS]]
        if not layout or layout[-1] != signature:
            layout.append(signature)
    width = max((len(row) for row in rows), default=0)
    key = hashlib.sha256(json.dumps([agent, width, layout]).encode("utf-8")).hexdigest()
    return key, layout


def map_columns(rows: list[list]) -> dict | None:
    """Ask the ACH Extraction Expert for the table's column mapping. None when not configured or on
    any failure, so the caller falls back to the header rules. Answers are cached by table layout for
    CACHE_TTL_SECONDS (so a re-deployed agent version takes over without a Function restart)."""
    agent = agent_configured(AGENT_SETTING)
    if not rows or not agent:
        return None
    key, _ = layout_key(agent, rows)
    now = time.monotonic()
    with _cache_lock:
        cached = _cache.get(key)
    if cached and now - cached["at"] < CACHE_TTL_SECONDS:
        result = cached["result"]
        header = (result or {}).get("headerRow")
        # The same layout puts the header on the same row; re-check it before reusing the answer.
        if result is None or header is None or (
            header < len(rows) and [_cell_kind(c) for c in rows[header][:MAX_COLUMNS]] == cached["header"]
        ):
            return result
    table = json.dumps({"rowCount": len(rows), "rows": _prompt_rows(rows)}, sort_keys=True, default=str)
    try:
        text, _ = invoke(agent, table)
        answer = parse_agent_json(text)
        result = {**answer, "source": "agent", "agent": agent} if _valid(answer, rows) else None
        if result is not None and invoke is invoke_agent:  # audit: agent version + model (cached lookup)
            from shared.foundry_agents import describe_agent

            result.update({k: v for k, v in describe_agent(agent).items() if k != "agent"})
        if result is None:
            logger.warning("extraction expert answer rejected: %s", text[:300])
    except Exception as exc:  # the rules fallback keeps the pipeline running
        logger.warning("extraction expert failed error=%s detail=%s", type(exc).__name__, str(exc)[:300])
        return None
    header = (result or {}).get("headerRow")
    header_layout = [_cell_kind(c) for c in rows[header][:MAX_COLUMNS]] if isinstance(header, int) else None
    with _cache_lock:
        _cache[key] = {"at": now, "result": result, "header": header_layout}
    return result


def resolve_columns(rows: list[list], scan_rows: int = 25) -> dict:
    """The column mapping to use: the ACH Extraction Expert's when available, else the frozen rules.

    Returns headerRow / identifierColumn / amountPaidColumn / tableKind / source ("agent" | "rules"),
    plus rulesAgree (None when the rules found no header) so a disagreement is visible in the audit."""
    from extraction.spreadsheet_parser import find_header

    rule_header, rule_id, rule_paid = find_header(rows, scan_rows=scan_rows)
    mapped = map_columns(rows)
    if mapped is None:
        return {
            "headerRow": rule_header,
            "identifierColumn": rule_id,
            "amountPaidColumn": rule_paid,
            "tableKind": "payment_detail" if rule_header is not None else None,
            "source": "rules",
            "rulesAgree": None,
        }
    agree = None
    if rule_header is not None:
        agree = (mapped.get("identifierColumn"), mapped.get("amountPaidColumn")) == (rule_id, rule_paid)
    return {**mapped, "rulesAgree": agree}
