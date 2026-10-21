"""ProcessRemittance orchestration (Durable Functions, Python generator).

    Service Bus message -> starter -> this orchestrator:
      1. record Received
      2. intake_parse            (archive, hash check, duplicate check, MSG/EML decomposition)
      3. extract_candidate x N   (fan-out: Content Understanding / deterministic spreadsheet parser)
      4. validate_reconcile      (deterministic merge, dedupe, $ tolerance, review gating)
      5. GATE: only ReviewRequired runs -> run_assurance_expert x experts (fan-out) -> fuse
      6. publish_results         (CSV only when clean, final JSON, manifest, assurance JSON)
      7. record Completed | ReviewRequired (finalize -> audit blob)

Orchestrator rules: deterministic code only. All I/O happens in activities; time comes from
``context.current_utc_datetime``; the Recorder activity is the only writer of ledger/audit state.
"""

from __future__ import annotations

from datetime import datetime, timezone

from assurance.case import build_case, thread_excerpt
from assurance.fusion import fuse
from assurance.triage import triage
from resolution.resolver import apply_resolution, needs_resolution, skip_reason
from shared import run_metrics, usage

ASSURANCE_EXPERTS = ["policy"]


def _utc(value: datetime) -> datetime:
    """Durable's replay clock may be naive; treat naive values as UTC so math and ISO strings agree."""
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def _iso(value: datetime) -> str:
    return _utc(value).isoformat()


def _parse_iso(value):
    if not value:
        return None
    try:
        return _utc(datetime.fromisoformat(str(value).replace("Z", "+00:00")))
    except ValueError:
        return None


class _Run:
    """Builds sequenced Recorder payloads for one run (pure; no I/O)."""

    def __init__(self, context, message: dict):
        self.context = context
        self.processing_id = message["processingId"]
        self.run_id = message["runId"]
        self.correlation_id = message.get("correlationId") or message["runId"]
        self.state = "Queued"
        self.seq = 0

    def now(self) -> str:
        return _iso(self.context.current_utc_datetime)

    def payload(self, transitions: list[tuple[str, str | None, dict]], ledger: dict | None = None, **extra) -> dict:
        events = []
        for new_state, stage, details in transitions:
            self.seq += 1
            events.append(
                {
                    "seq": self.seq,
                    "event": "state_transition",
                    "timeUtc": self.now(),
                    "previousState": self.state,
                    "newState": new_state,
                    "stage": stage,
                    "details": details or {},
                }
            )
            self.state = new_state
        return {
            "processingId": self.processing_id,
            "runId": self.run_id,
            "correlationId": self.correlation_id,
            "events": events,
            "ledger": ledger or {},
            **extra,
        }

    def base_input(self) -> dict:
        return {"processingId": self.processing_id, "runId": self.run_id, "correlationId": self.correlation_id}


def orchestrate(context, df):
    """Generator body of the orchestrator. ``df`` is the azure.durable_functions module."""
    message = context.get_input()
    run = _Run(context, message)
    started = _utc(context.current_utc_datetime)
    activity_retry = df.RetryOptions(first_retry_interval_in_milliseconds=5000, max_number_of_attempts=3)
    record_retry = df.RetryOptions(first_retry_interval_in_milliseconds=2000, max_number_of_attempts=5)

    def record(payload):
        return context.call_activity_with_retry("record", record_retry, payload)

    def call(name, payload):
        return context.call_activity_with_retry(name, activity_retry, payload)

    submitted = _parse_iso(message.get("submittedAtUtc"))
    queue_latency_ms = int((started - submitted).total_seconds() * 1000) if submitted else None
    stage = "intake"

    # The run event: facts gathered along the way, step durations, and the outcome.
    facts = {"stream": message.get("stream"), "submittedAtUtc": message.get("submittedAtUtc"), "queueLatencyMs": queue_latency_ms}
    steps: dict = {}
    extractions: list = []

    def finished(status: str, ledger: dict, *, resolution=None, cost=None) -> tuple[dict, dict]:
        """The terminal ledger with class and outcome, and the run event for the Recorder."""
        ledger = {**ledger, "outcome": run_metrics.outcome(status, resolution)}
        if extractions:
            ledger["messageClass"] = run_metrics.message_class(extractions)
        event = run_metrics.run_event(run.processing_id, run.run_id, run.correlation_id,
                                      {**facts, **ledger, "status": status}, steps=steps, cost=cost)
        return ledger, event

    try:
        yield record(
            run.payload(
                [("Received", "intake", {"stream": message.get("stream"), "queueLatencyMs": queue_latency_ms})],
                ledger={
                    "sourceName": message.get("sourceName"),
                    "stream": message.get("stream"),
                    "submittedAtUtc": message.get("submittedAtUtc"),
                    "startedAtUtc": _iso(started),
                    "createdAtUtc": message.get("submittedAtUtc") or _iso(started),
                    "queueLatencyMs": queue_latency_ms,
                    "durableInstanceId": context.instance_id,
                },
            )
        )

        intake = yield call(
            "intake_parse",
            {
                **run.base_input(),
                "sourceContainer": message.get("sourceContainer") or "incoming",
                "sourceBlobName": message["sourceBlobName"],
                "sourceName": message.get("sourceName"),
                "force": bool(message.get("force")),
            },
        )

        steps["intake"] = intake.get("durationMs")
        if intake["outcome"] == "rejected":
            ledger, event = finished("Failed", _terminal_ledger(context, run, started, errorCode=intake["reason"], stage="intake"))
            yield record(
                run.payload(
                    [("Failed", "intake", {"reason": intake["reason"]})],
                    ledger=ledger,
                    finalize=True,
                    error={"stage": "intake", "errorCode": intake["reason"], "occurredAt": run.now()},
                    runEvent=event,
                )
            )
            return {"status": "Failed", "reason": intake["reason"]}

        if intake["outcome"] == "duplicate":
            ledger, event = finished("DuplicateSkipped", _terminal_ledger(context, run, started, sourceSha256=intake.get("sourceSha256")))
            yield record(
                run.payload(
                    [("DuplicateSkipped", "intake", {"priorRunId": intake.get("priorRunId")})],
                    ledger=ledger,
                    finalize=True,
                    runEvent=event,
                )
            )
            return {"status": "DuplicateSkipped"}

        facts.update({"sourceSizeBytes": intake.get("sourceSizeBytes"), "attachmentCount": intake.get("attachmentCount")})

        candidates = intake["candidates"]
        eligible = [c for c in candidates if c.get("classification") == "remittance_candidate"]
        recommended = intake["recommendedProcessing"]
        yield record(
            run.payload(
                [
                    ("Archived", "intake", {"sourceSizeBytes": intake["sourceSizeBytes"]}),
                    ("Parsed", "msg_parse", {"attachmentCount": intake["attachmentCount"], "durationMs": intake["parseDurationMs"]}),
                    (
                        "CandidatesIdentified",
                        "candidate_selection",
                        {
                            "candidateCount": len(candidates),
                            "eligibleCount": len(eligible),
                            "primaryCandidateId": recommended.get("primaryCandidateId"),
                            "strategy": recommended.get("strategy"),
                            "durationMs": intake["durationMs"],
                        },
                    ),
                    ("Extracting", "candidate_extraction", {"candidateCount": len(eligible)}),
                ],
                ledger={
                    "sourceName": intake["sourceName"],
                    "sourceSha256": intake["sourceSha256"],
                    "sourceSizeBytes": intake["sourceSizeBytes"],
                    "attachmentCount": intake["attachmentCount"],
                    "candidateCount": len(eligible),
                    "subject": (intake.get("message") or {}).get("subject"),
                    "senderEmail": (intake.get("message") or {}).get("senderEmail"),
                    "manifestBlobUri": f"manifests/{run.processing_id}.json",
                },
            )
        )

        stage = "candidate_extraction"
        extractions = []
        if eligible:
            extractions = yield context.task_all(
                [call("extract_candidate", {**run.base_input(), "candidate": candidate}) for candidate in eligible]
            )
        throttle_retries = sum(item.get("throttleRetries", 0) for item in extractions)
        facts.update({"candidateCount": len(eligible), "throttleRetries": throttle_retries})
        steps["extraction"] = max((item.get("durationMs") or 0 for item in extractions), default=0)
        # AI usage per run (cost per message): each activity reports what it consumed.
        run_usage = usage.merge(*(item.get("usage") for item in extractions))

        stage = "validate_and_reconcile"
        yield record(
            run.payload(
                [
                    (
                        "Extracted",
                        "candidate_extraction",
                        {
                            "candidates": [
                                {
                                    "candidateId": item["candidateId"],
                                    "name": item.get("name"),
                                    "strategy": item.get("strategy"),
                                    "mappedBy": item.get("mappedBy"),
                                    "lineCount": item.get("lineCount"),
                                    "durationMs": item.get("durationMs"),
                                    "warningCount": len(item.get("warnings") or []),
                                    "throttleRetries": item.get("throttleRetries", 0),
                                }
                                for item in extractions
                            ],
                            "durationMs": max((item.get("durationMs") or 0 for item in extractions), default=0),
                            "throttleRetries": throttle_retries,
                        },
                    ),
                    ("Validating", "validate_and_reconcile", {}),
                ],
                ledger={"throttleRetries": throttle_retries},
            )
        )

        intake_summary = {k: v for k, v in intake.items() if k not in ("manifest", "candidates")}
        validated = yield call(
            "validate_reconcile",
            {**run.base_input(), "intake": intake_summary, "extractions": extractions},
        )
        final_result = validated["finalResult"]
        validation_ms = validated.get("durationMs") or 0
        steps["validation"] = validation_ms
        run_usage = usage.merge(run_usage, validated.get("usage"))  # Decision-1

        # Verification failed in a way the ACH Resolution Expert can work on (lines not grounded,
        # total off, nothing found...): one reasoned second attempt, accepted only if grounded and
        # reconciled. Business review reasons skip this and go straight to assurance.
        resolution = None
        if needs_resolution(final_result["quality"]["warnings"], requires_review=bool(final_result["quality"].get("requiresReview"))):
            stage = "resolution"
            resolved = yield call(
                "resolve_extraction",
                {**run.base_input(), "intake": intake_summary, "extractions": extractions, "finalResult": final_result},
            )
            run_usage = usage.merge(run_usage, resolved.get("usage"))  # the ACH Resolution Expert
            resolution = {k: v for k, v in resolved.items() if k not in ("validated", "usage")}
            validation_ms += resolved.get("durationMs") or 0
            steps["resolution"] = resolved.get("durationMs")
            if resolved.get("status") == "accepted" and resolved.get("validated"):
                validated = resolved["validated"]
            final_result = apply_resolution(final_result, resolved)
            stage = "validate_and_reconcile"

        quality = final_result["quality"]
        validation_details = {
            "status": final_result["status"],
            "paymentLineCount": quality["lineCount"],
            "isReconciled": quality.get("isReconciled"),
            "warningCount": len(quality["warnings"]),
            "durationMs": validation_ms,
        }
        if validated.get("classification"):
            validation_details["classification"] = validated["classification"]
        if resolution:
            validation_details["resolution"] = {k: resolution.get(k) for k in ("status", "reason", "triggers", "durationMs", "toolCalls")}
            details = resolution.get("details") or {}
            validation_details["resolution"].update(
                {"detailsApplied": sorted(details.get("applied") or {}), "detailsRecommended": sorted(details.get("recommended") or {})}
            )
        elif skip_reason(quality["warnings"]):
            # Audit trail only (timeline + stepper tooltip): why the expert was not called on a run
            # whose total is off. Not part of the result, so no card in Run Details.
            validation_details["resolution"] = {
                "status": "skipped", "reason": skip_reason(quality["warnings"]),
                "paymentCount": len(final_result.get("paymentGroups") or []) or None,
            }

        assurance = None
        if quality["requiresReview"]:
            # GATE: only exceptions reach the agent layer; clean runs never invoke an agent.
            stage = "assurance"
            # "Validated" carries the validation timing; "AssuranceReview" starts the agent step.
            yield record(
                run.payload(
                    [
                        ("Validated", "validate_and_reconcile", validation_details),
                        ("AssuranceReview", "assurance", {"experts": ASSURANCE_EXPERTS}),
                    ],
                    ledger={"requiresReview": True, "warnings": quality["warnings"]},
                )
            )
            assurance_started = _utc(context.current_utc_datetime)
            # Triage splits the run into owned issues; only payer issues go to the policy agent.
            issues = triage(final_result)
            case = build_case(final_result, issues, excerpt=thread_excerpt(extractions))
            if case["issues"]:
                findings = yield context.task_all(
                    [call("run_assurance_expert", {"expert": expert, "case": case}) for expert in ASSURANCE_EXPERTS]
                )
            else:
                findings = [{"expert": expert, "status": "skipped", "reason": "no_payer_issues"} for expert in ASSURANCE_EXPERTS]
            run_usage = usage.merge(run_usage, *(finding.get("usage") for finding in findings))  # the ACH Assurance Expert
            findings = [{k: v for k, v in finding.items() if k != "usage"} for finding in findings]
            assurance = fuse(findings, final_result, issues)
            assurance["assessedAtUtc"] = run.now()
            assurance_ms = int((_utc(context.current_utc_datetime) - assurance_started).total_seconds() * 1000)
            steps["assurance"] = assurance_ms
        else:
            yield record(run.payload([("Reconciled", "validate_and_reconcile", validation_details)]))

        stage = "persist_results"
        final_result["usage"] = run_usage  # priced in publish_results (the price table is read there)
        published = yield call(
            "publish_results",
            {
                **run.base_input(),
                "finalResult": final_result,
                "manifest": intake["manifest"],
                "validationBlobUri": validated.get("validationBlobUri"),
                "assurance": assurance,
            },
        )

        terminal = "ReviewRequired" if quality["requiresReview"] else "Completed"
        metadata = final_result["remittanceMetadata"]
        terminal_details = {"csvPublished": published.get("csvBlobUri") is not None}
        transitions = []
        if assurance:
            assessed = {
                "assuranceHeadline": assurance.get("headline"),
                "issueCount": assurance.get("issueCount"),
                "assuranceDecision": assurance.get("decision"),
                "assuranceConfidence": assurance.get("confidence"),
                "expertStatuses": {f.get("expert"): f.get("status") for f in assurance.get("experts") or []},
            }
            transitions.append(("Assessed", "assurance", {**assessed, "durationMs": assurance_ms}))
            terminal_details.update(assessed)
        transitions.append((terminal, "persist_results", terminal_details))
        steps["publish"] = published.get("durationMs")
        ledger, event = finished(
            terminal,
            _terminal_ledger(
                    context,
                    run,
                    started,
                    status=terminal,
                    resultStatus=final_result["status"],
                    requiresReview=quality["requiresReview"],
                    lineCount=quality["lineCount"],
                    calculatedPaymentTotal=quality["calculatedPaymentTotal"],
                    totalDifference=quality.get("totalDifference"),
                    isReconciled=quality.get("isReconciled"),
                    warnings=quality["warnings"],
                    reviewReasons=quality.get("reviewReasons"),
                    payerName=metadata.get("payerName"),
                    remittanceDate=metadata.get("remittanceDate"),
                    paymentTotal=metadata.get("paymentTotal"),
                    paymentReference=metadata.get("paymentReference"),
                    currency=metadata.get("currency"),
                    csvFileName=published.get("csvFileName"),
                    csvBlobUri=published.get("csvBlobUri"),
                    finalResultBlobUri=published.get("finalResultBlobUri"),
                    runFinalResultBlobUri=published.get("runFinalResultBlobUri"),
                    validationBlobUri=validated.get("validationBlobUri"),
                    assuranceBlobUri=published.get("assuranceBlobUri"),
                    assuranceDecision=(assurance or {}).get("decision"),
                    assuranceCategory=(assurance or {}).get("category"),
                    assuranceHeadline=(assurance or {}).get("headline"),
                    assuranceIssueCount=(assurance or {}).get("issueCount"),
                    assuranceConfidence=(assurance or {}).get("confidence"),
                    amountAtRisk=(assurance or {}).get("amountAtRisk"),
                    **(published.get("usageTotals") or {}),
                ),
            resolution=resolution,
            cost=published.get("usageEstimate"),
        )
        yield record(run.payload(transitions, ledger=ledger, finalize=True, runEvent=event))
        return {"status": terminal, "processingId": run.processing_id, "runId": run.run_id}

    except Exception as exc:  # activity exhausted its retries
        error_code = f"{stage}_failed"
        try:
            ledger, event = finished("Failed", _terminal_ledger(context, run, started, errorCode=error_code, stage=stage))
            yield record(
                run.payload(
                    [("Failed", stage, {"errorType": type(exc).__name__})],
                    ledger=ledger,
                    finalize=True,
                    error={"stage": stage, "errorCode": error_code, "occurredAt": run.now()},
                    runEvent=event,
                )
            )
        except Exception:
            pass
        return {"status": "Failed", "stage": stage}


def _terminal_ledger(context, run: _Run, started: datetime, **fields) -> dict:
    completed = _utc(context.current_utc_datetime)
    iso_year, iso_week, _ = completed.isocalendar()
    return {
        "processedAtUtc": _iso(completed),
        "processedDate": completed.date().isoformat(),
        "processedWeek": f"{iso_year}-W{iso_week:02d}",
        "durationMs": int((completed - started).total_seconds() * 1000),
        "auditBlobUri": f"audit/{run.processing_id}/{run.run_id}.json",
        **{key: value for key, value in fields.items()},
    }
