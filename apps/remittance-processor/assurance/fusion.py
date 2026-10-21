"""Deterministic fusion of triaged issues and expert findings into one assurance recommendation.

Runs inside the orchestrator, so it must stay pure (no I/O, no clock). The recommendation is
advisory: it never changes the run status, the extracted amounts, or CSV publication - a human in
payment processing still dispositions every ReviewRequired run.

Every issue from triage gets exactly one finding:
  * extraction / data_quality issues -> a rules-based finding (no agent);
  * payer issues -> the agent's finding for that issue; when experts disagree on an issue it is
    downgraded to manual review rather than silently picking one.
The top-level decision/category/rationale mirror the *primary* finding (extraction first, because
nothing else can be trusted until it is resolved; then payer issues by amount at risk; then data
fixes), so list views and older readers keep working.
"""

from __future__ import annotations

from assurance.case import REVIEW_REASONS
from assurance.triage import rule_finding, triage

_KIND_ORDER = {"extraction": 0, "payer": 1, "data_quality": 2}


def _payer_finding(issue: dict, completed: list[dict], statuses: set) -> dict:
    per_expert = [
        {**finding, "expert": result.get("expert")}
        for result in completed
        for finding in result.get("findings") or []
        if finding.get("issueId") == issue["id"]
    ]
    base = {
        "issueId": issue["id"],
        "kind": "payer",
        "headline": issue["headline"],
        "title": issue["title"],
        "lineCount": issue["lineCount"],
        "lines": issue["lines"][:10],
    }
    if not per_expert:
        reason = (
            "Assurance agents are not configured in this environment."
            if statuses == {"not_configured"}
            else "The agent returned no finding for this issue."
        )
        return {
            **base, "source": "none", "decision": "manual_review", "category": issue["code"],
            "amountAtRisk": abs(issue["amount"]) if issue["amount"] is not None else None, "confidence": None,
            "rationale": f"{issue['title']}. {reason}", "recommendedAction": "Review these lines against the source documents.",
            "citations": [],
        }
    lead = max(per_expert, key=lambda f: f.get("confidence") or 0.0)
    agree = len({f.get("decision") for f in per_expert}) == 1
    citations = []
    for finding in per_expert:
        for citation in finding.get("citations") or []:
            entry = {**citation, "expert": finding.get("expert")}
            if entry not in citations:
                citations.append(entry)
    amount = lead.get("amountAtRisk")
    return {
        **base,
        "source": "agent",
        "decision": lead.get("decision") if agree else "manual_review",
        "category": lead.get("category") or issue["code"],
        "amountAtRisk": amount if amount is not None else (abs(issue["amount"]) if issue["amount"] is not None else None),
        "confidence": lead.get("confidence"),
        "rationale": lead.get("rationale"),
        "recommendedAction": lead.get("recommendedAction"),
        "citations": citations,
        "expertsAgree": agree,
    }


def _priority(finding: dict):
    actionable = finding["decision"] != "manual_review"
    return (_KIND_ORDER.get(finding["kind"], 9), not actionable, -(finding.get("amountAtRisk") or 0.0))


def fuse(expert_results: list[dict], final_result: dict, issues: list[dict] | None = None) -> dict:
    issues = triage(final_result) if issues is None else issues
    quality = final_result.get("quality") or {}
    warnings = quality.get("warnings") or []
    difference = quality.get("totalDifference")
    completed = [r for r in expert_results if r.get("status") == "completed"]
    statuses = {r.get("status") for r in expert_results}

    findings = [
        rule_finding(issue) if issue["kind"] != "payer" else _payer_finding(issue, completed, statuses)
        for issue in issues
    ]
    if not findings:
        default_at_risk = round(abs(float(difference)), 2) if isinstance(difference, (int, float)) else None
        findings = [{
            "issueId": "review", "kind": "data_quality", "headline": "Validation flagged the run", "title": "Validation flagged the run for review",
            "lineCount": 0, "lines": [], "source": "rules", "decision": "manual_review", "category": "review",
            "amountAtRisk": default_at_risk, "confidence": None, "rationale": "Validation flagged the run for review.",
            "recommendedAction": "Review the remittance against the source documents.", "citations": [],
        }]
    findings.sort(key=_priority)
    primary = findings[0]

    citations = []
    for finding in findings:
        for citation in finding.get("citations") or []:
            if citation not in citations:
                citations.append(citation)
    agent_findings = [f for f in findings if f["source"] == "agent"]
    return {
        "gated": True,
        "gateReasons": [REVIEW_REASONS.get(w, w) for w in warnings if w in REVIEW_REASONS] or ["Validation flagged the run for review"],
        "experts": [{k: v for k, v in r.items() if k != "findings"} for r in expert_results],
        "findings": findings,
        "issueCount": len(findings),
        "headline": primary["headline"],
        "decision": primary["decision"],
        "category": primary["category"],
        "amountAtRisk": primary.get("amountAtRisk"),
        "confidence": primary.get("confidence"),
        "rationale": primary.get("rationale"),
        "recommendedAction": primary.get("recommendedAction"),
        "citations": citations,
        "expertsAgree": all(f.get("expertsAgree", True) for f in agent_findings),
    }
