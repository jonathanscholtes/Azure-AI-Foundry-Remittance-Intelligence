"""Source and thread classification with Microsoft-Decision-1 (typed answers + probabilities).

Runs after extraction, only when the rules cannot settle an email: two or more Content Understanding
sources produced payment lines (e.g. a forwarded thread with transfer proofs, a body discussing
payments, and an embedded remittance). One call per email asks:
  * each such source's role - payment detail, payment proof, statement, correspondence, other;
  * the thread's kind - one remittance, several payments, a dispute / inquiry, no remittance.

The questions and the probability threshold are business-owned configuration
(config/intake_decision.json). Answers are applied conservatively in ``apply`` and recorded in the
result for audit. Not configured, throttled, failed or uncertain -> nothing changes (fail-safe).

Setting: INTAKE_DECISION_DEPLOYMENT (a Microsoft-Decision-1 deployment on the Foundry account whose
endpoint is CONTENT_UNDERSTANDING_ENDPOINT, or INTAKE_DECISION_ENDPOINT).
"""

from __future__ import annotations

import json
import logging
import os
import time
import urllib.error
import urllib.request
from decimal import Decimal, InvalidOperation
from pathlib import Path

from shared import usage

logger = logging.getLogger("remittance_intake")

CONFIG_PATH = Path(__file__).resolve().parents[1] / "config" / "intake_decision.json"
THROTTLE_CODES = {408, 429, 500, 502, 503, 504}
EXCLUDED_ROLES = {"statement", "correspondence", "other"}

_config = None
_credential = None


def config() -> dict:
    global _config
    if _config is None:
        _config = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    return _config


def _settings() -> tuple[str | None, str | None]:
    deployment = os.environ.get("INTAKE_DECISION_DEPLOYMENT", "").strip() or None
    endpoint = (os.environ.get("INTAKE_DECISION_ENDPOINT") or os.environ.get("CONTENT_UNDERSTANDING_ENDPOINT") or "").strip() or None
    return deployment, endpoint


def call(body: dict, *, attempts: int = 3) -> dict:
    """POST to the Decision API (/providers/microsoft/v1/systemone) with Entra auth and bounded backoff.
    Replaceable for tests and for the golden runner's response cache."""
    global _credential
    deployment, endpoint = _settings()
    if _credential is None:
        from azure.identity import DefaultAzureCredential

        _credential = DefaultAzureCredential()
    payload = json.dumps({"model": deployment, **body}).encode("utf-8")
    for attempt in range(attempts):
        token = _credential.get_token("https://cognitiveservices.azure.com/.default").token
        request = urllib.request.Request(
            f"{endpoint.rstrip('/')}/providers/microsoft/v1/systemone",
            data=payload,
            method="POST",
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json", "Accept": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                return json.loads(response.read())
        except urllib.error.HTTPError as exc:
            if exc.code not in THROTTLE_CODES or attempt == attempts - 1:
                raise
            retry_after = exc.headers.get("Retry-After") if exc.headers else None
            time.sleep(min(float(retry_after or 2 ** (attempt + 1)), 30.0))
    raise RuntimeError("unreachable")


def _amount(value) -> Decimal | None:
    try:
        return Decimal(str(value).replace(",", "").replace("$", "")).quantize(Decimal("0.01"))
    except (InvalidOperation, ValueError):
        return None


def needs_classification(extractions: list[dict]) -> list[dict]:
    """The sources the rules cannot settle: CU-read sources with lines, when there are two or more."""
    ambiguous = [
        item for item in extractions
        if item.get("strategy") == "content_understanding" and (item.get("normalized") or {}).get("paymentLines")
    ]
    return ambiguous if len(ambiguous) >= 2 else []


def _choice(answer: dict) -> tuple[str | None, float | None]:
    """(selected option, its probability) from a 'choice' answer, tolerant of the probability shape."""
    if not isinstance(answer, dict):
        return None, None
    choice = answer.get("choice")
    probabilities = answer.get("probabilities") or answer.get("scores") or answer.get("options") or {}
    probability = None
    if isinstance(probabilities, dict):
        probability = probabilities.get(choice)
    elif isinstance(probabilities, list):
        for item in probabilities:
            if isinstance(item, dict) and (item.get("option") or item.get("label") or item.get("name")) == choice:
                probability = item.get("probability") or item.get("score")
    if probability is None:
        probability = answer.get("probability") or answer.get("confidence")
    try:
        return choice, float(probability) if probability is not None else None
    except (TypeError, ValueError):
        return choice, None


def classify(extractions: list[dict], subject: str | None = None) -> dict | None:
    """One Decision-1 call for the email. None when not needed, not configured, or on failure."""
    sources = needs_classification(extractions)
    deployment, endpoint = _settings()
    if not sources or not (deployment and endpoint):
        return None
    cfg = config()
    state = {
        "subject": subject,
        "sources": [
            {
                "id": item["candidateId"],
                "name": item.get("name"),
                "kind": (item.get("normalized") or {}).get("sourceType"),
                "embeddedIn": item.get("embeddedIn"),
                "excerpt": (item.get("excerpt") or "")[: cfg["maxExcerptChars"]],
                "paymentLines": [
                    {"pro": line.get("proInvoiceNumber"), "amount": line.get("amountPaid")}
                    for line in (item.get("normalized") or {}).get("paymentLines", [])[: cfg["maxLinesPerSource"]]
                ],
            }
            for item in sources
        ],
    }
    questions = {
        f"role_{index}": {
            "type": "choice",
            "instructions": cfg["sourceRole"]["instructions"].format(id=item["candidateId"], name=item.get("name") or ""),
            "criteria": cfg["sourceRole"]["criteria"],
        }
        for index, item in enumerate(sources)
    }
    questions["thread_kind"] = {"type": "choice", "instructions": cfg["threadKind"]["instructions"], "criteria": cfg["threadKind"]["criteria"]}
    try:
        response = call({"state": state, "questions": questions})
    except Exception as exc:  # fail-safe: classification never blocks a run
        logger.warning("intake decision failed error=%s detail=%s", type(exc).__name__, str(exc)[:300])
        return {"status": "failed", "error": type(exc).__name__, "deployment": deployment}
    usage.record_decision_response(response, deployment)
    answers = response.get("answers") or {}
    roles = {}
    for index, item in enumerate(sources):
        choice, probability = _choice(answers.get(f"role_{index}"))
        roles[item["candidateId"]] = {"role": choice, "probability": probability}
    thread, thread_probability = _choice(answers.get("thread_kind"))
    return {
        "status": "completed",
        "deployment": deployment,
        "model": response.get("model"),
        "roles": roles,
        "thread": {"kind": thread, "probability": thread_probability},
    }


def apply(extractions: list[dict], result: dict | None) -> dict:
    """Apply confident answers to the extracted lines (in place) and return the audit record.

    * statement / correspondence / other sources: their lines are excluded;
    * payment-proof lines are excluded as corroboration when a payment-detail source lists the lines
      (they count only when no detail source exists);
    * if that would leave no lines at all, nothing is excluded (fail-safe).
    Returns {"applied", "excluded": [...], "threadKind"} for the result and triage."""
    record = {**(result or {}), "applied": False, "excluded": [], "threadKind": None}
    if not result or result.get("status") != "completed":
        return record
    threshold = config()["minProbability"]
    confident = {
        cid: answer["role"]
        for cid, answer in result["roles"].items()
        if answer.get("role") and answer.get("probability") is not None and answer["probability"] >= threshold
    }
    by_id = {item["candidateId"]: item for item in extractions}
    excluded: list[tuple[str, int, str]] = []
    for cid, role in confident.items():
        if role in EXCLUDED_ROLES:
            excluded += [(cid, i, role) for i in range(len(by_id[cid]["normalized"].get("paymentLines") or []))]
    # A proof confirms that a transfer happened; it does not list what was paid. When a confident
    # payment-detail source lists the lines, a proof's "lines" are evidence (and what CU reads off a
    # bank receipt as a PRO is often an account or reference number) - they count only when no
    # detail source exists.
    has_detail = any(
        role == "payment_detail" and by_id[cid]["normalized"].get("paymentLines") for cid, role in confident.items()
    )
    if has_detail:
        for cid, role in confident.items():
            if role == "payment_proof":
                excluded += [(cid, i, "payment_proof_corroborates") for i in range(len(by_id[cid]["normalized"].get("paymentLines") or []))]

    total_lines = sum(len(item["normalized"].get("paymentLines") or []) for item in extractions)
    if excluded and len(excluded) < total_lines:
        drop = {(cid, i) for cid, i, _ in excluded}
        for item in extractions:
            lines = item["normalized"].get("paymentLines") or []
            kept = [line for i, line in enumerate(lines) if (item["candidateId"], i) not in drop]
            for i, line in enumerate(lines):
                if (item["candidateId"], i) in drop:
                    reason = next(r for c, j, r in excluded if c == item["candidateId"] and j == i)
                    record["excluded"].append({"candidateId": item["candidateId"], "proInvoiceNumber": line.get("proInvoiceNumber"),
                                               "amountPaid": line.get("amountPaid"), "reason": reason})
            item["normalized"]["paymentLines"] = kept
        record["applied"] = True
    thread = result.get("thread") or {}
    if thread.get("kind") and thread.get("probability") is not None and thread["probability"] >= threshold:
        record["threadKind"] = thread["kind"]
    return record
