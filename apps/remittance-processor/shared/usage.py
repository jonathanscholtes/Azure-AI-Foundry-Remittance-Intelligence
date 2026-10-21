"""AI usage per run: what each component consumed, for cost per message.

Raw counts only; a price table (config/pricing.json) turns them into an estimated cost, so a rate
change never needs reprocessing. The block is additive - nothing in the result, the review outcome or
the payment CSV depends on it - and every helper tolerates missing usage (older runs, cache hits,
components that don't report it).

Shape of a usage block::

    {"contentUnderstanding": {"calls", "pages": {"minimal", "basic", "standard"},
                              "contextualizationTokens", "tokens": {"gpt-5.5-input": n, ...}},
     "agents": {"ach-resolution-expert": {"calls", "model", "inputTokens", "cachedInputTokens",
                                          "outputTokens", "reasoningTokens"}, ...},
     "decision": {"calls", "model", "inputTokens", "outputTokens"}}

Input token counts include their cached part, and output token counts include reasoning tokens
(both as the services report them), so cost uses input - cached at the input rate, cached at the
cached-input rate, and output at the output rate.

Agent and Decision-1 calls are recorded into the activity's collector (``collect``) where they
happen; Content Understanding usage travels with each analysis (its chunks run on worker threads)
and is added with ``record_cu``.
"""

from __future__ import annotations

import contextlib
import contextvars
import json
from pathlib import Path

_collector: contextvars.ContextVar[dict | None] = contextvars.ContextVar("usage_collector", default=None)

PRICING_PATH = Path(__file__).resolve().parent.parent / "config" / "pricing.json"
_TOKEN_KEYS = ("inputTokens", "cachedInputTokens", "outputTokens", "reasoningTokens")


def empty() -> dict:
    return {}


def merge(*blocks: dict | None) -> dict:
    """Add usage blocks together (numbers summed, labels such as the model kept)."""
    total: dict = {}
    for block in blocks:
        if block:
            _add(total, block)
    return total


def _add(target: dict, source: dict) -> None:
    for key, value in source.items():
        if isinstance(value, dict):
            _add(target.setdefault(key, {}), value)
        elif isinstance(value, bool) or value is None:
            continue
        elif isinstance(value, (int, float)):
            target[key] = target.get(key, 0) + value
        else:
            target.setdefault(key, value)


@contextlib.contextmanager
def collect():
    """Collect usage recorded inside the block; on exit it is also added to an enclosing collector."""
    block: dict = {}
    parent = _collector.get()
    token = _collector.set(block)
    try:
        yield block
    finally:
        _collector.reset(token)
        if parent is not None and block:
            _add(parent, block)


def _record(block: dict) -> None:
    current = _collector.get()
    if current is not None and block:
        _add(current, block)


# --- Content Understanding ------------------------------------------------------------------------


def cu_from_operation(raw: dict | None) -> dict | None:
    """Our shape from the analyze operation's ``usage`` (documentPages<Tier>, contextualizationTokens,
    tokens{<model>-input|-cached-input|-output}); None when the operation didn't report it."""
    if not isinstance(raw, dict):
        return None
    pages = {
        key[len("documentPages"):].lower(): int(value)
        for key, value in raw.items()
        if key.startswith("documentPages") and isinstance(value, (int, float))
    }
    tokens = {key: int(value) for key, value in (raw.get("tokens") or {}).items() if isinstance(value, (int, float))}
    block = {"calls": 1, "pages": pages, "tokens": tokens}
    if isinstance(raw.get("contextualizationTokens"), (int, float)):
        block["contextualizationTokens"] = int(raw["contextualizationTokens"])
    return block


def record_cu(cu_usage: dict | None) -> None:
    """Add one source's Content Understanding usage (already summed over its chunks)."""
    if cu_usage:
        _record({"contentUnderstanding": cu_usage})


# --- Foundry agents (Responses API) and Decision-1 ------------------------------------------------


def _attr(obj, name, default=None):
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def record_agent_response(agent: str, response) -> None:
    """Record one Responses API call of ``agent`` (``response.usage``; reasoning tokens bill as output)."""
    usage = _attr(response, "usage")
    entry = {"calls": 1}
    model = _attr(response, "model")
    if model:
        entry["model"] = str(model)
    if usage is not None:
        entry.update({
            "inputTokens": int(_attr(usage, "input_tokens", 0) or 0),
            "cachedInputTokens": int(_attr(_attr(usage, "input_tokens_details"), "cached_tokens", 0) or 0),
            "outputTokens": int(_attr(usage, "output_tokens", 0) or 0),
            "reasoningTokens": int(_attr(_attr(usage, "output_tokens_details"), "reasoning_tokens", 0) or 0),
        })
    _record({"agents": {agent.split(":", 1)[0]: entry}})


def record_decision_response(response: dict, deployment: str | None) -> None:
    """Record one Microsoft-Decision-1 call (its response carries usage.input_tokens / output_tokens)."""
    usage = (response or {}).get("usage") or {}
    entry = {"calls": 1, "model": (response or {}).get("model") or deployment,
             "inputTokens": int(usage.get("input_tokens") or 0), "outputTokens": int(usage.get("output_tokens") or 0)}
    _record({"decision": {k: v for k, v in entry.items() if v is not None}})


# --- Totals and estimated cost --------------------------------------------------------------------


def totals(block: dict | None) -> dict:
    """Flat per-run totals for the ledger row and the run event."""
    block = block or {}
    cu = block.get("contentUnderstanding") or {}
    agents = block.get("agents") or {}
    decision = block.get("decision") or {}
    model_tokens = sum(n for key, n in (cu.get("tokens") or {}).items() if not key.endswith("-cached-input"))
    agent_tokens = sum(a.get("inputTokens", 0) + a.get("outputTokens", 0) for a in agents.values())
    estimate = block.get("estimate") or {}
    return {
        "cuCalls": cu.get("calls", 0),
        "cuPages": sum((cu.get("pages") or {}).values()),
        "agentCalls": sum(a.get("calls", 0) for a in agents.values()),
        "decisionCalls": decision.get("calls", 0),
        "aiTokens": model_tokens + agent_tokens + decision.get("inputTokens", 0) + decision.get("outputTokens", 0),
        "estimatedCost": estimate.get("total"),
        "estimatedCostComplete": estimate.get("complete"),
    }


def load_pricing(path: Path = PRICING_PATH) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _model_rates(pricing: dict, model: str | None) -> tuple[str | None, dict]:
    """The price-table entry for a model or deployment name (exact name or one of its aliases)."""
    if not model:
        return None, {}
    lowered = model.lower()
    for name, rates in (pricing.get("models") or {}).items():
        aliases = [name] + list(rates.get("aliases") or [])
        if lowered in (alias.lower() for alias in aliases):
            return name, rates
    return None, {}


def estimate(block: dict | None, pricing: dict) -> dict:
    """Estimated cost per component from raw counts and the price table. Meters without a rate are
    listed in ``missingRates`` and count as zero; ``complete`` says whether every meter was priced."""
    block = block or {}
    missing: set[str] = set()
    per_million = 1_000_000

    def rate(rates: dict, key: str, meter: str) -> float:
        value = rates.get(key)
        if value is None:
            missing.add(meter)
            return 0.0
        return float(value)

    by_component: dict[str, float] = {}

    cu = block.get("contentUnderstanding") or {}
    if cu:
        cu_rates = pricing.get("contentUnderstanding") or {}
        cost = 0.0
        for tier, count in (cu.get("pages") or {}).items():
            cost += count / 1000 * rate(cu_rates.get("pagesPer1000") or {}, tier, f"cu.pages.{tier}")
        if cu.get("contextualizationTokens"):
            cost += cu["contextualizationTokens"] / per_million * rate(cu_rates, "contextualizationPer1M", "cu.contextualization")
        # "<model>-input" (includes the cached part) | "<model>-cached-input" | "<model>-output"
        per_model: dict[str, dict[str, int]] = {}
        for key, count in (cu.get("tokens") or {}).items():
            for suffix, kind in (("-cached-input", "cached"), ("-input", "input"), ("-output", "output")):
                if key.endswith(suffix):
                    per_model.setdefault(key[: -len(suffix)], {})[kind] = count
                    break
        for model, counts in per_model.items():
            name, rates = _model_rates(pricing, model)
            label = name or model
            cached = counts.get("cached", 0)
            cost += (
                max(counts.get("input", 0) - cached, 0) / per_million * rate(rates, "inputPer1M", f"model.{label}.inputPer1M")
                + cached / per_million * rate(rates, "cachedInputPer1M", f"model.{label}.cachedInputPer1M")
                + counts.get("output", 0) / per_million * rate(rates, "outputPer1M", f"model.{label}.outputPer1M")
            )
        by_component["contentUnderstanding"] = cost

    for agent, entry in (block.get("agents") or {}).items():
        name, rates = _model_rates(pricing, entry.get("model"))
        label = name or entry.get("model") or "unknown-model"
        cached = entry.get("cachedInputTokens", 0)
        cost = (
            (entry.get("inputTokens", 0) - cached) / per_million * rate(rates, "inputPer1M", f"model.{label}.inputPer1M")
            + cached / per_million * rate(rates, "cachedInputPer1M", f"model.{label}.cachedInputPer1M")
            + entry.get("outputTokens", 0) / per_million * rate(rates, "outputPer1M", f"model.{label}.outputPer1M")
        )
        by_component[agent] = cost

    decision = block.get("decision") or {}
    if decision:
        name, rates = _model_rates(pricing, decision.get("model"))
        label = name or decision.get("model") or "unknown-model"
        by_component["decision"] = (
            decision.get("inputTokens", 0) / per_million * rate(rates, "inputPer1M", f"model.{label}.inputPer1M")
            + decision.get("outputTokens", 0) / per_million * rate(rates, "outputPer1M", f"model.{label}.outputPer1M")
        )

    return {
        "currency": pricing.get("currency", "USD"),
        "pricingVersion": pricing.get("version"),
        # Shown with every estimate so it isn't read as the bill (e.g. "Azure list prices ...").
        "basis": pricing.get("basis"),
        "disclaimer": pricing.get("disclaimer"),
        "byComponent": {key: round(value, 6) for key, value in by_component.items()},
        "total": round(sum(by_component.values()), 6) if by_component else 0.0,
        "complete": not missing,
        "missingRates": sorted(missing),
    }
