"""Invoke the deployed Foundry assurance experts (the ACH Assurance Expert today; WebIQ later).

Each expert is a deployed Foundry prompt agent referenced by ``name[:version]`` and invoked through the
OpenAI Responses API with an ``agent_reference`` (shared/foundry_agents.py). The agent runs its own
tools server-side (the ACH Assurance Expert queries the AI Search policy index) and returns JSON with
cited evidence.

When the project endpoint or agent name is not configured, the expert returns a ``not_configured``
finding so the orchestration still completes and the run lands in human review unchanged.
"""

from __future__ import annotations

import json
import logging
import os
import time

from shared.foundry_agents import agent_reference, invoke_agent, parse_agent_json  # noqa: F401 (re-exported)

logger = logging.getLogger("remittance_intake")

EXPERTS = {
    "policy": {
        "label": "ACH Assurance Expert",
        "agentSetting": "ASSURANCE_AGENT_NAME",
        "task": (
            "Assess EACH payer issue in this remittance case against the customer contract terms "
            "and the deduction / short-pay policy. Search the policy index, cite the specific policy "
            "sections you rely on, and recommend a disposition per issue that names the PROs and "
            "amounts involved. The 'handledInternally' items are resolved by the cash application "
            "team - do not assess them or recommend contacting the payer about them."
        ),
    },
}

DECISIONS = {
    "approve_deduction",
    "dispute_short_pay",
    "request_remittance_detail",
    "hold_overpayment",
    "confirm_with_payer",
    "route_to_billing",
    "manual_review",
}

OUTPUT_CONTRACT = """\
Respond with ONE JSON object only (no markdown fences) using exactly this schema, with one finding per
issue in the case's "issues" list (same issueId):
{
  "findings": [
    {
      "issueId": "<id of the issue from the case>",
      "decision": "approve_deduction | dispute_short_pay | request_remittance_detail | hold_overpayment | confirm_with_payer | route_to_billing | manual_review",
      "category": "<short snake_case category, e.g. early_payment_discount, freight_claim, accessorial_dispute, debit_memo, duplicate_payment, overpayment, conflicting_sources, unexplained_short_pay, missing_remittance_detail>",
      "amountAtRisk": <number, USD that could be misapplied, lost or disputed (a gap, an excess, disputed lines); null when every dollar is accounted for>,
      "confidence": <number 0..1>,
      "rationale": "<two or three sentences grounded in the cited policy, naming the PROs/amounts involved>",
      "recommendedAction": "<one concrete next step for the analyst, naming the PROs/amounts involved>",
      "citations": [{"policyId": "<id>", "title": "<section title>", "excerpt": "<short quote>"}]
    }
  ]
}
Never change or invent remittance amounts or PRO numbers. Use "manual_review" only when no policy section applies."""

def _number(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        return round(float(value), 2)
    except (TypeError, ValueError):
        return None


def _normalize_issue_finding(item: dict, issue_id: str | None) -> dict:
    decision = str(item.get("decision") or "manual_review").strip().lower()
    if decision not in DECISIONS:
        decision = "manual_review"
    confidence = _number(item.get("confidence"))
    if confidence is not None:
        confidence = max(0.0, min(1.0, confidence))
    citations = [
        {
            "policyId": str(citation.get("policyId") or "")[:80] or None,
            "title": str(citation.get("title") or "")[:200] or None,
            "excerpt": str(citation.get("excerpt") or "")[:600] or None,
        }
        for citation in item.get("citations") or []
        if isinstance(citation, dict)
    ]
    return {
        "issueId": issue_id,
        "decision": decision,
        "category": str(item.get("category") or "other")[:60],
        "amountAtRisk": _number(item.get("amountAtRisk")),
        "confidence": confidence,
        "rationale": str(item.get("rationale") or "")[:2000],
        "recommendedAction": str(item.get("recommendedAction") or "")[:600],
        "citations": citations[:10],
    }


def normalize_finding(expert: str, agent: str, parsed: dict, annotations: list[dict], issue_ids: list[str] | None = None) -> dict:
    """Normalize the agent's per-issue findings. Findings for unknown issues are dropped; a legacy
    single-decision answer (no ``findings`` list) applies to every issue in the case."""
    issue_ids = list(issue_ids or [])
    findings = []
    if isinstance(parsed.get("findings"), list):
        for item in parsed["findings"]:
            if not isinstance(item, dict):
                continue
            issue_id = str(item.get("issueId") or "") or None
            if issue_ids and issue_id not in issue_ids:
                if len(issue_ids) == 1 and issue_id is None:
                    issue_id = issue_ids[0]
                else:
                    continue
            findings.append(_normalize_issue_finding(item, issue_id))
    elif parsed.get("decision"):
        findings = [_normalize_issue_finding(parsed, issue_id) for issue_id in (issue_ids or [None])]
    sources = [{"policyId": None, "title": a.get("title"), "excerpt": None, "url": a.get("url")} for a in annotations]
    return {
        "expert": expert,
        "label": EXPERTS[expert]["label"],
        "agent": agent,
        "status": "completed",
        "findings": findings,
        "sources": sources[:10],
    }


def run_expert(expert: str, case: dict, *, invoke=None) -> dict:
    """Run one expert over a case. Never raises - failures become a 'failed' finding."""
    started = time.perf_counter()
    spec = EXPERTS.get(expert)
    if spec is None:
        return {"expert": expert, "status": "failed", "error": "unknown_expert"}
    agent = os.environ.get(spec["agentSetting"], "").strip()
    base = {"expert": expert, "label": spec["label"], "agent": agent or None}
    if invoke is None and (not agent or not os.environ.get("FOUNDRY_PROJECT_ENDPOINT")):
        return {**base, "status": "not_configured", "durationMs": 0}

    prompt = f"{spec['task']}\n\nRemittance case:\n{json.dumps(case, default=str)}\n\n{OUTPUT_CONTRACT}"
    try:
        text, annotations = (invoke or invoke_agent)(agent, prompt)
        parsed = parse_agent_json(text)
        if not parsed:
            raise ValueError("agent returned no JSON finding")
        finding = normalize_finding(expert, agent, parsed, annotations, [issue["id"] for issue in case.get("issues") or []])
        if invoke is None:  # audit: the exact agent version and model that answered
            from shared.foundry_agents import describe_agent

            finding.update({k: v for k, v in describe_agent(agent).items() if k != "agent"})
    except Exception as exc:
        logger.warning("assurance expert failed expert=%s error=%s detail=%s", expert, type(exc).__name__, str(exc)[:300])
        finding = {**base, "status": "failed", "error": type(exc).__name__, "errorDetail": str(exc)[:500]}
    finding["durationMs"] = int((time.perf_counter() - started) * 1000)
    return finding
