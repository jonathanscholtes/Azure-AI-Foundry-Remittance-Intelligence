"""ACH remittance load report: run, token and AI cost metrics by message class for a set of runs.

Reads each run from the deployed API (the same data as the run page) for the processing ids a load
test submitted (load_test.py --results) or that you list, optionally waiting until every run has
finished. It sends nothing and calls no AI service, so it can be re-run at no cost.

Message classes (set by the pipeline per run):
    A  table in the email body      B  spreadsheet      C  small document
    D  large document (chunked, >100 lines or >5 pages)  none  no payment lines

Output, per class and overall:
    runs     messages, outcome (first pass / agent-resolved / review / failed), lines, duration, queue wait
    tokens   per model: average input / cached / output / reasoning tokens per message; CU pages
    cost     average and total estimated cost, cost by component, Content Understanding share
    shares   messages that used any AI, called an agent (the Extraction Expert's column mapping counts),
             or need human review

Averages are per message over the messages that recorded usage (shown as "with usage"). Cost is the
pipeline's estimate from its price table (list prices), not the bill. Runs are reported by
processing id only - no file or payer names - so the report can be shared.

Examples:
    # After a load test (wait until every run has finished, keep the JSON)
    python test/load/load_report.py --api https://<ui-fqdn> --results test/load/run-20261121.json --wait --out test/load/report-20261121.json

    # Specific runs
    python test/load/load_report.py --api https://<ui-fqdn> --ids REM-0123456789ABCDEF0123,REM-...
"""

from __future__ import annotations

import argparse
import json
import math
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

TERMINAL = {"Completed", "ReviewRequired", "Failed", "DuplicateSkipped"}
CLASS_LABELS = {"A": "A body table", "B": "B spreadsheet", "C": "C small doc", "D": "D large doc", "none": "none"}
OUTCOMES = (("handled_by_rules", "first pass"), ("handled_by_agents", "agent-resolved"),
            ("flagged_for_review", "review"), ("failed", "failed"))
COMPONENT_LABELS = {"contentUnderstanding": "CU", "decision": "Decision-1", "ach-extraction-expert": "Extraction Expert",
                    "ach-resolution-expert": "Resolution Expert", "ach-assurance-expert": "Assurance Expert"}
PRICING = Path(__file__).resolve().parents[2] / "apps" / "remittance-processor" / "config" / "pricing.json"


# --- collection ----------------------------------------------------------------------------------


def processing_ids(results: list[Path], ids: str | None) -> list[str]:
    found: list[str] = []
    for path in results:
        for row in json.loads(path.read_text()):
            if row.get("processingId"):
                found.append(row["processingId"])
    found += [value.strip() for value in (ids or "").split(",") if value.strip()]
    return list(dict.fromkeys(found))  # a file sent unchanged twice is one message


def fetch(api_base: str, processing_id: str, timeout: float) -> dict:
    url = f"{api_base.rstrip('/')}/api/remittances/{processing_id}"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            return json.loads(response.read())
    except urllib.error.HTTPError as exc:
        return {"processingId": processing_id, "status": f"http_{exc.code}"}
    except Exception as exc:
        return {"processingId": processing_id, "status": f"error_{type(exc).__name__}"}


def collect(api_base: str, ids: list[str], *, wait: bool, poll_seconds: float, max_wait_minutes: float,
            timeout: float, concurrency: int) -> dict[str, dict]:
    runs: dict[str, dict] = {}
    pending = list(ids)
    deadline = time.monotonic() + max_wait_minutes * 60
    with ThreadPoolExecutor(max_workers=max(1, concurrency)) as pool:
        while pending:
            for run in pool.map(lambda pid: fetch(api_base, pid, timeout), pending):
                runs[run.get("processingId")] = run
            pending = [pid for pid in pending if runs[pid].get("status") not in TERMINAL]
            print(f"[report] {len(ids) - len(pending)}/{len(ids)} finished")
            if not pending or not wait or time.monotonic() > deadline:
                break
            time.sleep(poll_seconds)
    return runs


# --- metrics -------------------------------------------------------------------------------------


def load_aliases() -> dict[str, str]:
    """Lower-cased deployment or model name -> model name from the price table (e.g. cu-gpt-55 -> gpt-5.5)."""
    try:
        models = json.loads(PRICING.read_text()).get("models") or {}
    except (OSError, ValueError):
        return {}
    names = {name.lower(): name for name in models}
    names.update({alias.lower(): name for name, entry in models.items() for alias in entry.get("aliases") or []})
    return names


def model_tokens(usage: dict, aliases: dict[str, str]) -> dict[str, dict[str, int]]:
    """Tokens per model for one run, across CU, the agents and Decision-1."""
    totals: dict[str, dict[str, int]] = {}

    def add(model, kind, count):
        if count:
            name = aliases.get(str(model or "").lower(), model or "unknown")
            totals.setdefault(name, {}).setdefault(kind, 0)
            totals[name][kind] += int(count)

    for key, count in ((usage.get("contentUnderstanding") or {}).get("tokens") or {}).items():
        for suffix, kind in (("-cached-input", "cached"), ("-input", "input"), ("-output", "output")):
            if key.endswith(suffix):
                add(key[: -len(suffix)], kind, count)
                break
    for entry in list((usage.get("agents") or {}).values()) + [usage.get("decision") or {}]:
        for field, kind in (("inputTokens", "input"), ("cachedInputTokens", "cached"),
                            ("outputTokens", "output"), ("reasoningTokens", "reasoning")):
            add(entry.get("model"), kind, entry.get(field))
    return totals


def percentile(values: list[float], pct: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return ordered[max(0, math.ceil(len(ordered) * pct) - 1)]  # nearest rank: p50 of 2 runs is the lower one


def number(value) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def summarize(runs: list[dict], aliases: dict[str, str]) -> dict:
    count = len(runs)
    with_usage = [r for r in runs if r.get("usage")]
    outcomes = {label: sum(1 for r in runs if r.get("outcome") == key) for key, label in OUTCOMES}
    durations = [number(r.get("durationMs")) / 1000 for r in runs if r.get("durationMs") is not None]
    waits = [number(r.get("queueLatencyMs")) / 1000 for r in runs if r.get("queueLatencyMs") is not None]

    models: dict[str, dict[str, float]] = {}
    pages: dict[str, float] = {}
    context = 0.0
    components: dict[str, float] = {}
    for run in with_usage:
        usage = run["usage"]
        for model, kinds in model_tokens(usage, aliases).items():
            entry = models.setdefault(model, {"messages": 0})
            entry["messages"] += 1
            for kind, value in kinds.items():
                entry[kind] = entry.get(kind, 0) + value
        cu = usage.get("contentUnderstanding") or {}
        for tier, billed in (cu.get("pages") or {}).items():
            pages[tier] = pages.get(tier, 0) + number(billed)
        context += number(cu.get("contextualizationTokens"))
        for component, cost in ((usage.get("estimate") or {}).get("byComponent") or {}).items():
            components[component] = components.get(component, 0) + number(cost)

    def cost_of(run):
        estimate = (run.get("usage") or {}).get("estimate") or {}
        return number(estimate.get("total") if estimate.get("total") is not None else run.get("estimatedCost"))

    costed = [r for r in runs if r.get("usage") or r.get("estimatedCost") is not None]
    total_cost = sum(cost_of(r) for r in costed)
    calls = lambda r, key: number(r.get(key))  # noqa: E731
    share = lambda n: round(100.0 * n / count, 1) if count else None  # noqa: E731
    per = lambda total: round(total / len(with_usage), 1) if with_usage else None  # noqa: E731
    agent_used = lambda r, agent: (((r.get("usage") or {}).get("agents") or {}).get(agent) or {}).get("calls")  # noqa: E731
    share_counts = {
        "anyAi": sum(1 for r in runs if calls(r, "cuCalls") + calls(r, "agentCalls") + calls(r, "decisionCalls") > 0),
        "agentCalls": sum(1 for r in runs if calls(r, "agentCalls") > 0),
        # Per agent, from the run's usage: shows *why* agents ran (column mapping vs. a run in trouble).
        "extraction": sum(1 for r in runs if agent_used(r, "ach-extraction-expert")),
        "resolution": sum(1 for r in runs if agent_used(r, "ach-resolution-expert")),
        "assurance": sum(1 for r in runs if agent_used(r, "ach-assurance-expert")),
        "decision": sum(1 for r in runs if calls(r, "decisionCalls") > 0),
        "humanReview": outcomes["review"],
    }
    return {
        "messages": count,
        "withUsage": len(with_usage),
        "outcomes": outcomes,
        "lines": {"total": int(sum(number(r.get("lineCount")) for r in runs)),
                  "avg": round(sum(number(r.get("lineCount")) for r in runs) / count, 1) if count else None},
        "durationSeconds": {"p50": percentile(durations, 0.5), "p95": percentile(durations, 0.95)},
        "queueWaitSeconds": {"p50": percentile(waits, 0.5), "p95": percentile(waits, 0.95)},
        # Billed pages by tier: "standard" = PDFs and images (layout analysis); "minimal" = plain text (email
        # bodies, .txt), counted by length and billed at a far lower rate.
        "cu": {"pagesPerMessage": {tier: per(total) for tier, total in sorted(pages.items())},
               "pagesTotal": {tier: int(total) for tier, total in sorted(pages.items())},
               "contextualizationPerMessage": per(context)},
        "tokensPerMessage": {
            model: {"usedBy": int(entry.pop("messages")),
                    **{kind: round(value / len(with_usage)) for kind, value in entry.items()}}
            for model, entry in sorted(models.items())
        },
        "cost": {
            "total": round(total_cost, 4),
            "perMessage": round(total_cost / len(costed), 4) if costed else None,
            "byComponent": {name: round(value, 4) for name, value in sorted(components.items(), key=lambda kv: -kv[1])},
            "cuSharePct": round(100.0 * components.get("contentUnderstanding", 0) / sum(components.values()), 1)
            if sum(components.values()) else None,
        },
        "shareCounts": share_counts,
        "shares": {f"{key}Pct": share(value) for key, value in share_counts.items()},
        "outcomePcts": {label: share(value) for label, value in outcomes.items()},
    }


# --- output --------------------------------------------------------------------------------------


def table(title: str, header: list[str], rows: list[list]) -> None:
    cells = [[("-" if value is None else str(value)) for value in row] for row in rows]  # ASCII: Windows consoles
    widths = [max(len(str(h)), *(len(row[i]) for row in cells)) for i, h in enumerate(header)]
    print(f"\n{title}")
    print("  ".join(str(h).ljust(w) for h, w in zip(header, widths)))
    print("  ".join("-" * w for w in widths))
    for row in cells:
        print("  ".join(value.ljust(w) for value, w in zip(row, widths)))


def money(value) -> str | None:
    return None if value is None else f"${value:,.4f}"


def thousands(value) -> str | None:
    return None if value is None else f"{value / 1000:,.1f}K"


def both(count, pct) -> str:
    """'12 (70.6%)': a count with its percentage."""
    return f"{count} ({pct}%)" if pct is not None else str(count)


def print_report(groups: dict[str, dict]) -> None:
    total = groups["All"]["messages"]
    table("Run metrics (Msgs: % of all messages; outcomes: % of the class)",
          ["Class", "Msgs", "First pass", "Agent-resolved", "Review", "Failed", "Avg lines", "p50 s", "p95 s", "Queue p50 s"],
          [[name, both(g["messages"], round(100.0 * g["messages"] / total, 1) if total else None),
            *(both(count, g["outcomePcts"][label]) for label, count in g["outcomes"].items()), g["lines"]["avg"],
            *(None if v is None else round(v, 1) for v in (g["durationSeconds"]["p50"], g["durationSeconds"]["p95"], g["queueWaitSeconds"]["p50"]))]
           for name, g in groups.items()])
    keys = ("anyAi", "agentCalls", "extraction", "resolution", "assurance", "decision", "humanReview")
    table("Shares (messages, % of the class)",
          ["Class", "Msgs", "Any AI", "Any agent", "Extraction Expert", "Resolution Expert", "Assurance Expert", "Decision-1", "Human review"],
          [[name, g["messages"], *(both(g["shareCounts"][key], g["shares"][f"{key}Pct"]) for key in keys)]
           for name, g in groups.items()])
    print("Extraction Expert maps table columns (answers cached by layout); Resolution and Assurance Experts run only "
          "for a message headed to review, so with no agent-resolved runs, 'Any agent' tracks 'Human review'.")
    tiers = sorted({tier for g in groups.values() for tier in g["cu"]["pagesTotal"]}, key=lambda t: (t != "standard", t))
    table("Content Understanding billed pages (only what CU analyzed; table and spreadsheet rows read in code are not pages)",
          ["Class", "With usage", *(f"{tier.capitalize()} pages/msg" for tier in tiers),
           *(f"{tier.capitalize()} total" for tier in tiers), "Contextualization tokens/msg"],
          [[name, g["withUsage"], *(g["cu"]["pagesPerMessage"].get(tier) for tier in tiers),
            *(g["cu"]["pagesTotal"].get(tier) for tier in tiers), thousands(g["cu"]["contextualizationPerMessage"])]
           for name, g in groups.items()])
    print("Standard = PDFs and images (one image = one page). Minimal = plain text such as email bodies, counted by "
          "length (roughly 3,000 characters per page) and billed at a much lower rate.")
    rows = []
    for name, g in groups.items():
        for index, (model, t) in enumerate(g["tokensPerMessage"].items()):
            rows.append([name if index == 0 else "", model, t["usedBy"], thousands(t.get("input")), thousands(t.get("cached")),
                         thousands(t.get("output")), thousands(t.get("reasoning"))])
    table("Model tokens (average per message with usage; input includes cached, output includes reasoning)",
          ["Class", "Model", "Used by", "Input", "Cached", "Output", "Reasoning"], rows)
    components = sorted({c for g in groups.values() for c in g["cost"]["byComponent"]},
                        key=lambda c: -groups["All"]["cost"]["byComponent"].get(c, 0))
    table("AI cost (estimate, list prices)",
          ["Class", "With usage", "Per message", "Total", *(COMPONENT_LABELS.get(c, c) for c in components), "CU share %"],
          [[name, g["withUsage"], money(g["cost"]["perMessage"]), money(g["cost"]["total"]),
            *(money(g["cost"]["byComponent"].get(c)) for c in components), g["cost"]["cuSharePct"]]
           for name, g in groups.items()])


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--api", required=True, help="Base URL of the UI (its nginx proxies /api) or the API.")
    parser.add_argument("--results", type=Path, action="append", default=[], help="load_test.py --results file (repeatable).")
    parser.add_argument("--ids", help="Comma-separated processing ids, in addition to --results.")
    parser.add_argument("--wait", action="store_true", help="Poll until every run has finished (or --max-wait-minutes).")
    parser.add_argument("--poll-seconds", type=float, default=20, help="Wait between polls with --wait (default 20).")
    parser.add_argument("--max-wait-minutes", type=float, default=60, help="Stop waiting after this long (default 60).")
    parser.add_argument("--concurrency", type=int, default=8, help="Parallel API reads (default 8).")
    parser.add_argument("--timeout", type=float, default=30, help="HTTP timeout per read in seconds (default 30).")
    parser.add_argument("--out", type=Path, help="Write the report (and per-run metrics) as JSON to this file.")
    args = parser.parse_args()

    ids = processing_ids(args.results, args.ids)
    if not ids:
        parser.error("no processing ids: pass --results <load_test results file> and/or --ids")
    runs = collect(args.api, ids, wait=args.wait, poll_seconds=args.poll_seconds, max_wait_minutes=args.max_wait_minutes,
                   timeout=args.timeout, concurrency=args.concurrency)

    finished = [r for r in runs.values() if r.get("status") in TERMINAL and r.get("status") != "DuplicateSkipped"]
    duplicates = sum(1 for r in runs.values() if r.get("status") == "DuplicateSkipped")
    unfinished = [pid for pid, r in runs.items() if r.get("status") not in TERMINAL]
    aliases = load_aliases()
    by_class: dict[str, list[dict]] = {}
    for run in finished:
        by_class.setdefault(run.get("messageClass") or "none", []).append(run)
    groups = {CLASS_LABELS.get(key, key): summarize(by_class[key], aliases) for key in sorted(by_class)}
    groups["All"] = summarize(finished, aliases)

    print(f"[report] {len(finished)} finished message(s) reported | {duplicates} duplicate(s) excluded | "
          f"{len(unfinished)} not finished{' (re-run, or add --wait)' if unfinished else ''}")
    if finished:
        print_report(groups)
    estimate = next((((r.get("usage") or {}).get("estimate") or {}) for r in finished if (r.get("usage") or {}).get("estimate")), {})
    if estimate.get("basis"):
        print(f"\nCost basis: {estimate['basis']}. {estimate.get('disclaimer') or ''}".rstrip())

    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        per_run = [{key: run.get(key) for key in ("processingId", "status", "outcome", "messageClass", "lineCount", "durationMs",
                                                  "queueLatencyMs", "cuCalls", "cuPages", "agentCalls", "decisionCalls",
                                                  "aiTokens", "estimatedCost")}
                   | {"tokensByModel": model_tokens(run.get("usage") or {}, aliases)} for run in runs.values()]
        args.out.write_text(json.dumps({"groups": groups, "unfinished": unfinished, "duplicates": duplicates, "runs": per_run}, indent=2))
        print(f"[report] -> {args.out}")
    return 0 if not unfinished else 3


if __name__ == "__main__":
    sys.exit(main())
