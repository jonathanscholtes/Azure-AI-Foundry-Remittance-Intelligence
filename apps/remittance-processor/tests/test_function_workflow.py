"""End-to-end orchestration test: drives the Durable orchestrator generator against the real
activity implementations with in-memory Blob/Table fakes (no Functions host, no Azure)."""

import json
import sys
import types
import unittest
from datetime import datetime, timedelta, timezone


def _install_azure_stubs_if_needed():
    try:
        import azure.identity  # noqa: F401
        import azure.storage.blob  # noqa: F401
        return
    except ImportError:
        pass
    azure_package = sys.modules.setdefault("azure", types.ModuleType("azure"))
    azure_package.__path__ = []
    identity_module = types.ModuleType("azure.identity")
    identity_module.DefaultAzureCredential = type("DefaultAzureCredential", (), {})
    storage_package = types.ModuleType("azure.storage")
    storage_package.__path__ = []
    blob_module = types.ModuleType("azure.storage.blob")
    blob_module.BlobServiceClient = type("BlobServiceClient", (), {})
    blob_module.ContentSettings = lambda **kwargs: kwargs
    sys.modules["azure.identity"] = identity_module
    sys.modules["azure.storage"] = storage_package
    sys.modules["azure.storage.blob"] = blob_module


_install_azure_stubs_if_needed()

from msg_parser.parser import ParsedAttachment  # noqa: E402
from orchestration import activities  # noqa: E402
from orchestration.orchestrator import orchestrate  # noqa: E402
from shared.recorder import record  # noqa: E402

SOURCE_BYTES = b"test message bytes"
PROCESSING_ID = activities.processing_id_for(SOURCE_BYTES)


class FakeBlobClient:
    def __init__(self, storage, container, name):
        self.storage, self.container, self.name = storage, container, name

    def exists(self):
        return (self.container, self.name) in self.storage.blobs

    def upload_blob(self, data, overwrite=False, content_settings=None):
        self.storage.blobs[(self.container, self.name)] = bytes(data)

    def download_blob(self):
        return types.SimpleNamespace(readall=lambda: self.storage.blobs[(self.container, self.name)])


class FakeBlobService:
    def __init__(self):
        self.blobs = {("incoming", f"{PROCESSING_ID}/ACH REMITTANCE.msg"): SOURCE_BYTES}

    def get_blob_client(self, container, name):
        return FakeBlobClient(self, container, name)


class FakeTable:
    def __init__(self):
        self.rows = {}

    def upsert_entity(self, entity):
        key = (entity["PartitionKey"], entity["RowKey"])
        self.rows[key] = {**self.rows.get(key, {}), **entity}

    def query_entities(self, query_filter, parameters):
        return [row for (pk, _), row in self.rows.items() if pk == parameters["pk"]]


class FakeRetryOptions:
    def __init__(self, first_retry_interval_in_milliseconds, max_number_of_attempts):
        pass


FAKE_DF = types.SimpleNamespace(RetryOptions=FakeRetryOptions)


class FakeContext:
    instance_id = PROCESSING_ID

    def __init__(self, message):
        self._message = message
        self.current_utc_datetime = datetime(2026, 10, 16, 15, 0, tzinfo=timezone.utc)

    def get_input(self):
        return self._message

    def call_activity_with_retry(self, name, _retry, payload):
        return ("activity", name, payload)

    def task_all(self, tasks):
        return ("all", tasks)


def _parsed_message(body):
    attachment = ParsedAttachment(
        name="REMITTANCE.pdf",
        extension=".pdf",
        content_type="application/pdf",
        content=b"%PDF-1.7" + b"x" * 1024,
        is_inline=False,
    )
    return types.SimpleNamespace(
        subject="ACH Remittance",
        sender_name="Acme Transport",
        sender_email="billing@example.com",
        sent_date="2026-10-05",
        message_id="<message-1@example.com>",
        in_reply_to=None,
        references=[],
        body=body,
        html_body=None,
        attachments=[attachment],
        warnings=[],
    )


def _route(candidates, _payloads, **_kwargs):
    candidate = candidates[0]
    if candidate["sourceType"] == "message_body":
        item = {
            "candidateId": candidate["candidateId"],
            "sourceType": "message_body",
            "score": candidate["score"],
            "remittanceMetadata": {
                "payerName": "Acme Transport",
                "remittanceDate": "2026-10-05",
                "paymentTotal": 100.0,
                "currency": "USD",
                "paymentReference": None,
            },
            "metadataEvidence": {},
            "paymentLines": [],
            "warnings": [],
        }
    else:
        item = {
            "candidateId": candidate["candidateId"],
            "sourceType": "attachment",
            "score": candidate["score"],
            "remittanceMetadata": {},
            "metadataEvidence": {},
            "paymentLines": [
                {"proInvoiceNumber": "001234567", "amountPaid": 100.0, "source": {"page": 1}, "confidence": 0.98}
            ],
            "warnings": [],
        }
    return [item], [{"candidateId": candidate["candidateId"], "result": {"test": True}}]


class Harness:
    def __init__(self, body, expert=None, route=None, resolution=None):
        self.blob = FakeBlobService()
        self.ledger = FakeTable()
        self.events = FakeTable()
        self.body = body
        self.expert = expert
        self.route = route or _route
        self.resolution = resolution  # fake ACH Resolution Expert: (agent, case, tools) -> (text, trace)
        self.activity_calls = []

    def _execute(self, name, payload):
        self.activity_calls.append(name)
        payload = json.loads(json.dumps(payload))  # Durable serializes inputs
        if name == "record":
            return record(payload, ledger_client=self.ledger, events_client=self.events, blob_service=self.blob)
        if name == "intake_parse":
            return activities.intake_parse(payload, blob_service=self.blob, parse=lambda *_: _parsed_message(self.body))
        if name == "extract_candidate":
            original = activities.route_candidates
            activities.route_candidates = self.route
            try:
                return activities.extract_candidate(payload, blob_service=self.blob)
            finally:
                activities.route_candidates = original
        if name == "validate_reconcile":
            return activities.validate_reconcile(payload, blob_service=self.blob)
        if name == "resolve_extraction":
            from unittest.mock import patch

            from resolution import resolver

            env = {"RESOLUTION_AGENT_NAME": "ach-resolution-expert" if self.resolution else "", "FOUNDRY_PROJECT_ENDPOINT": "https://p.example"}
            with patch.dict("os.environ", env), patch.object(resolver, "run_agent", self.resolution or (lambda *a: ("", []))), \
                    patch.object(resolver, "describe", lambda agent: {"agent": agent, "agentVersion": "7", "model": "reasoning-gpt-56-terra"}):
                return activities.resolve_extraction(payload, blob_service=self.blob)
        if name == "run_assurance_expert":
            return activities.run_assurance_expert(payload, run_expert=self.expert)
        if name == "publish_results":
            return activities.publish_results(payload, blob_service=self.blob)
        raise AssertionError(f"unexpected activity {name}")

    def run(self, **overrides):
        message = {
            "processingId": PROCESSING_ID,
            "runId": "run-1",
            "correlationId": "corr-1",
            "sourceContainer": "incoming",
            "sourceBlobName": f"{PROCESSING_ID}/ACH REMITTANCE.msg",
            "sourceName": "ACH REMITTANCE.msg",
            "stream": "upload",
            "submittedAtUtc": "2026-10-16T14:59:58+00:00",
            **overrides,
        }
        context = FakeContext(message)
        generator = orchestrate(context, FAKE_DF)
        result = None
        try:
            task = next(generator)
            while True:
                context.current_utc_datetime += timedelta(seconds=1)
                try:
                    if task[0] == "all":
                        result = [self._execute(name, payload) for _, name, payload in task[1]]
                    else:
                        result = self._execute(task[1], task[2])
                except Exception as exc:  # Durable surfaces a failed activity inside the orchestrator
                    task = generator.throw(exc)
                    continue
                task = generator.send(result)
        except StopIteration as stop:
            return stop.value

    def final_result(self):
        return json.loads(self.blob.blobs[("results", f"{PROCESSING_ID}/final-remittance.json")])

    def ledger_row(self):
        return self.ledger.rows[(PROCESSING_ID, "run-1")]


class OrchestrationWorkflowTests(unittest.TestCase):
    def test_clean_run_completes_publishes_csv_and_skips_agents(self):
        harness = Harness("Customer remittance payment for invoice 001234567 attached.")
        with self.assertLogs("remittance_intake", level="INFO") as logs:
            outcome = harness.run()
        run_events = [json.loads(line.split("remittance_run_completed ", 1)[1]) for line in logs.output if "remittance_run_completed " in line]
        self.assertEqual(len(run_events), 1)  # one run event per finished run
        self.assertEqual(run_events[0]["outcome"], "handled_by_rules")
        self.assertEqual(run_events[0]["status"], "Completed")
        self.assertEqual(run_events[0]["stream"], "upload")
        self.assertIn("intake", run_events[0]["stepDurationsMs"])

        self.assertEqual(outcome["status"], "Completed")
        result = harness.final_result()
        self.assertEqual(result["status"], "Extracted")
        self.assertEqual(result["paymentLines"][0]["proInvoiceNumber"], "001234567")
        self.assertFalse(result["outputFile"]["hasHeader"])
        self.assertTrue(any(name.endswith(".csv") for _, name in harness.blob.blobs))
        self.assertNotIn("run_assurance_expert", harness.activity_calls)

        row = harness.ledger_row()
        self.assertEqual(row["status"], "Completed")
        self.assertEqual(row["payerName"], "Acme Transport")
        self.assertEqual(row["lineCount"], 1)
        self.assertEqual(row["processedDate"], "2026-10-16")
        self.assertEqual(row["queueLatencyMs"], 2000)

        states = [r["newState"] for r in sorted(harness.events.rows.values(), key=lambda r: r["RowKey"])]
        self.assertEqual(
            states,
            ["Received", "Archived", "Parsed", "CandidatesIdentified", "Extracting", "Extracted", "Validating", "Reconciled", "Completed"],
        )
        audit = json.loads(harness.blob.blobs[("audit", f"{PROCESSING_ID}/run-1.json")])
        self.assertEqual(len(audit["events"]), 9)
        # Usage per run is additive: present and priced, the CSV and result unchanged.
        self.assertIn("estimate", result["usage"])
        self.assertIn("cuCalls", row)
        self.assertEqual(row["outcome"], "handled_by_rules")
        self.assertIn(row["messageClass"], ("A", "C"))  # a body read by the table readers or by CU

    def test_agent_usage_reaches_the_run_usage_and_ledger_totals(self):
        from types import SimpleNamespace

        from shared import usage

        def expert(name, case):
            # As shared.foundry_agents does for every Responses API call.
            usage.record_agent_response("ach-assurance-expert", SimpleNamespace(
                model="gpt-5.5", usage=SimpleNamespace(input_tokens=4000, output_tokens=600,
                                                       input_tokens_details=SimpleNamespace(cached_tokens=0),
                                                       output_tokens_details=SimpleNamespace(reasoning_tokens=0))))
            return {"expert": name, "status": "completed", "findings": []}

        harness = Harness("Remittance details: invoice 001234567 https://portal.example.com/login", expert=expert)
        harness.run()

        result = harness.final_result()
        agent = result["usage"]["agents"]["ach-assurance-expert"]
        self.assertEqual((agent["calls"], agent["inputTokens"], agent["outputTokens"]), (1, 4000, 600))
        self.assertNotIn("usage", result["assurance"]["experts"][0])  # kept in the run's usage block only
        self.assertEqual(harness.ledger_row()["agentCalls"], 1)

    def test_review_run_is_gated_to_assurance_without_csv(self):
        def expert(name, case):
            self.assertEqual(case["payerName"], "Acme Transport")
            self.assertIn("payer:external_link_not_fetched", [issue["id"] for issue in case["issues"]])
            return {
                "expert": name,
                "status": "completed",
                "findings": [
                    {
                        "issueId": "payer:external_link_not_fetched",
                        "decision": "request_remittance_detail",
                        "category": "missing_remittance_detail",
                        "amountAtRisk": None,
                        "confidence": 0.8,
                        "rationale": "Detail is behind a portal link.",
                        "recommendedAction": "Ask the payer for the remittance detail file.",
                        "citations": [{"policyId": "POL-RD-01", "title": "Remittance detail", "excerpt": "..."}],
                    }
                ],
            }

        harness = Harness("Remittance details: invoice 001234567 https://portal.example.com/login", expert=expert)
        outcome = harness.run()

        self.assertEqual(outcome["status"], "ReviewRequired")
        result = harness.final_result()
        self.assertIsNone(result["outputFile"]["blobUri"])
        self.assertFalse(any(name.endswith(".csv") for _, name in harness.blob.blobs))
        self.assertEqual(result["assurance"]["decision"], "request_remittance_detail")
        self.assertEqual(result["assurance"]["citations"][0]["policyId"], "POL-RD-01")
        self.assertIn("run_assurance_expert", harness.activity_calls)

        row = harness.ledger_row()
        self.assertEqual(row["status"], "ReviewRequired")
        self.assertEqual(row["assuranceDecision"], "request_remittance_detail")
        self.assertEqual(row["assuranceHeadline"], "Remittance detail behind a link")
        self.assertEqual(row["assuranceIssueCount"], len(result["assurance"]["findings"]))

        # Validation and agent timings land on their own steps (not the validation time on the agent step).
        events = sorted(harness.events.rows.values(), key=lambda r: r["RowKey"])
        states = [r["newState"] for r in events]
        self.assertEqual(states[-4:], ["Validated", "AssuranceReview", "Assessed", "ReviewRequired"])
        details = {r["newState"]: json.loads(r.get("detailsJson") or "{}") for r in events}
        self.assertIn("durationMs", details["Validated"])
        self.assertNotIn("durationMs", details["AssuranceReview"])
        self.assertIn("durationMs", details["Assessed"])

    def test_resolution_expert_fixes_a_run_that_fails_verification(self):
        # Extraction read the wrong amount (100) from the attachment; the stated total is 150 and the
        # body says "Invoice 001234567 paid $150.00". The expert proposes the body's line: grounded and
        # reconciled, so it is accepted and the run completes.
        def route(candidates, payloads, **kwargs):
            items, raws = _route(candidates, payloads, **kwargs)
            if items[0]["sourceType"] == "message_body":
                items[0]["remittanceMetadata"]["paymentTotal"] = 150.0
            return items, raws

        def expert(agent, case, tools):
            self.assertIn("payment_total_mismatch", case["problems"])
            self.assertTrue(tools["check_lines"]([{"sourceId": "message-body", "pro": "001234567", "amount": 150}])["allGrounded"])
            proposal = {"outcome": "resolved", "threadKind": "single_remittance", "confidence": 0.9,
                        "explanation": "The body states the amount paid; the attachment line was misread.",
                        "lines": [{"sourceId": "message-body", "pro": "001234567", "amount": 150.0, "evidence": "Invoice 001234567 paid $150.00"}]}
            return json.dumps(proposal), [{"tool": "check_lines", "arguments": {}, "ok": True}]

        harness = Harness("Invoice 001234567 paid $150.00", route=route, resolution=expert)
        outcome = harness.run()

        self.assertEqual(outcome["status"], "Completed")
        result = harness.final_result()
        self.assertEqual([(l["proInvoiceNumber"], l["amountPaid"]) for l in result["paymentLines"]], [("001234567", 150.0)])
        self.assertEqual(result["resolution"]["status"], "accepted")
        self.assertIn("resolved_by_expert", result["quality"]["warnings"])
        # Audit: what the expert proposed, how it worked, and which agent version answered.
        self.assertEqual(result["resolution"]["proposal"][0]["pro"], "001234567")
        self.assertEqual(result["resolution"]["trace"][0]["tool"], "check_lines")
        self.assertEqual((result["resolution"]["agentVersion"], result["resolution"]["model"]), ("7", "reasoning-gpt-56-terra"))
        self.assertEqual(result["paymentLines"][0]["source"]["method"], "resolution_expert")
        self.assertIn("resolve_extraction", harness.activity_calls)

    def test_unresolved_run_keeps_its_lines_and_explains(self):
        def route(candidates, payloads, **kwargs):
            items, raws = _route(candidates, payloads, **kwargs)
            if items[0]["sourceType"] == "message_body":
                items[0]["remittanceMetadata"]["paymentTotal"] = 150.0
            return items, raws

        def expert(agent, case, tools):
            # Proposes an amount that appears nowhere in the sources: the gate must refuse it.
            return json.dumps({"outcome": "resolved", "threadKind": "unclear", "confidence": 0.4, "explanation": "Guess.",
                               "lines": [{"sourceId": "message-body", "pro": "001234567", "amount": 150.0, "evidence": "-"}]}), []

        harness = Harness("Remittance for invoice 001234567 attached.", route=route, resolution=expert,
                          expert=lambda name, case: {"expert": name, "status": "completed", "findings": []})
        outcome = harness.run()

        self.assertEqual(outcome["status"], "ReviewRequired")
        result = harness.final_result()
        self.assertEqual(result["paymentLines"][0]["amountPaid"], 100.0)  # original extraction kept
        self.assertEqual(result["resolution"]["status"], "rejected")
        self.assertTrue(result["resolution"]["reason"].startswith("line_not_grounded"))
        self.assertIn("extraction:resolution_unresolved", [f["issueId"] for f in result["assurance"]["findings"]])

    def test_duplicate_of_extracted_result_is_skipped(self):
        harness = Harness("Customer remittance payment for invoice 001234567 attached.")
        harness.blob.blobs[("results", f"{PROCESSING_ID}/final-remittance.json")] = json.dumps(
            {"status": "Extracted", "runId": "run-0"}
        ).encode()
        outcome = harness.run()
        self.assertEqual(outcome["status"], "DuplicateSkipped")
        self.assertEqual(harness.ledger_row()["status"], "DuplicateSkipped")

    def test_hash_mismatch_is_rejected_as_failed(self):
        harness = Harness("body")
        outcome = harness.run(processingId="REM-00000000000000000000")
        self.assertEqual(outcome["status"], "Failed")
        row = harness.ledger.rows[("REM-00000000000000000000", "run-1")]
        self.assertEqual(row["errorCode"], "source_hash_mismatch")
        self.assertTrue(any(container == "errors" for container, _ in harness.blob.blobs))

    def test_activity_failure_records_failed_state(self):
        harness = Harness("body")
        original = harness._execute

        def failing(name, payload):
            if name == "validate_reconcile":
                raise RuntimeError("boom")
            return original(name, payload)

        harness._execute = failing
        with self.assertLogs("remittance_intake", level="INFO") as logs:
            outcome = harness.run()
        self.assertEqual(outcome, {"status": "Failed", "stage": "validate_and_reconcile"})
        self.assertEqual(harness.ledger_row()["errorCode"], "validate_and_reconcile_failed")
        self.assertEqual(harness.ledger_row()["outcome"], "failed")
        events = [json.loads(line.split("remittance_run_completed ", 1)[1]) for line in logs.output if "remittance_run_completed " in line]
        self.assertEqual([(e["outcome"], e["stage"]) for e in events], [("failed", "validate_and_reconcile")])


class ThrottleTests(unittest.TestCase):
    def test_retries_429_then_succeeds(self):
        calls = {"n": 0}

        class Throttled(Exception):
            status_code = 429

        def analyze(**_kwargs):
            calls["n"] += 1
            if calls["n"] < 3:
                raise Throttled()
            return "ok"

        stats = {}
        wrapped = activities.throttled_analyze(analyze, sleep=lambda _s: None, stats=stats)
        self.assertEqual(wrapped(), "ok")
        self.assertEqual(stats["throttled429"], 2)

    def test_non_retryable_error_raises_immediately(self):
        def analyze(**_kwargs):
            raise ValueError("bad input")

        with self.assertRaises(ValueError):
            activities.throttled_analyze(analyze, sleep=lambda _s: None)()


if __name__ == "__main__":
    unittest.main()
