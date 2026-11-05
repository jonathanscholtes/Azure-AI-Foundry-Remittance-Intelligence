import importlib.util
import os
import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

try:
    from fastapi.testclient import TestClient

    from app import main
except ImportError:  # FastAPI / Azure SDKs not installed locally
    main = None

REVIEW_AGENT = Path(__file__).resolve().parents[3] / "agents" / "review_agent.py"


def _review_agent():
    spec = importlib.util.spec_from_file_location("review_agent", REVIEW_AGENT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@unittest.skipIf(main is None, "FastAPI not installed")
class ReviewApiTests(unittest.TestCase):
    def setUp(self):
        # No storage in unit tests: the routes get a placeholder repository.
        main.app.dependency_overrides[main.repo] = lambda: None
        self.client = TestClient(main.app)

    def tearDown(self):
        main.app.dependency_overrides.clear()

    def test_complete_review_validates_outcome(self):
        response = self.client.post("/api/review/runs/REM-0123456789ABCDEF0123/complete",
                                    json={"reviewer": "Analyst One", "outcome": "done", "notes": "ok."})
        self.assertEqual(response.status_code, 422)

    def test_complete_review_accepts_json_string_body(self):
        # APIM's MCP export forwards the body as a JSON string holding the object.
        calls = []

        class Repo:
            def complete_review(self, pid, **fields):
                calls.append((pid, fields))
                return {"processingId": pid, "reviewStatus": "Completed"}

        main.app.dependency_overrides[main.repo] = lambda: Repo()
        body = '{"reviewer": " Analyst One ", "outcome": "approved", "notes": "Lines match the advice."}'
        response = self.client.post("/api/review/runs/REM-0123456789ABCDEF0123/complete", json=body)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(calls, [("REM-0123456789ABCDEF0123", {
            "reviewer": "Analyst One", "outcome": "approved", "notes": "Lines match the advice."})])

        invalid = '{"reviewer": "Analyst One", "outcome": "done", "notes": "ok."}'
        response = self.client.post("/api/review/runs/REM-0123456789ABCDEF0123/complete", json=invalid)
        self.assertEqual(response.status_code, 422)
        self.assertEqual(len(calls), 1)

    def test_agent_allowed_tools_match_review_operation_ids(self):
        # APIM declares one operation per route (infra/apim/review-mcp.bicep), named after its operation id...
        spec = self.client.get("/openapi.json").json()
        operation_ids = {
            operation["operationId"]
            for path, operations in spec["paths"].items() if path.startswith("/api/review/")
            for operation in operations.values()
        }
        bicep = (REVIEW_AGENT.parents[1] / "infra" / "apim" / "review-mcp.bicep").read_text(encoding="utf-8")
        for name in operation_ids:
            self.assertIn(f"name: '{name}'", bicep)
        # ...and APIM's MCP export camel-cases the operation name into the tool name (list_runs -> listRuns),
        # which is what the agent's ALLOWED_TOOLS filter has to match.
        tool_names = {re.sub(r"_([a-z])", lambda m: m.group(1).upper(), name) for name in operation_ids}
        self.assertEqual(set(_review_agent().ALLOWED_TOOLS), tool_names)


if __name__ == "__main__":
    unittest.main()
