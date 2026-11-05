import json
import os
import sys
import types
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

try:
    from app.repository import LedgerRepository, QueueUnavailable, ReviewConflict
except ImportError:  # Azure SDKs not installed locally
    LedgerRepository = None


class FakeBlobClient:
    def __init__(self, store, container, name):
        self.store, self.container, self.name = store, container, name

    def upload_blob(self, data, overwrite=False, **kwargs):
        self.store[(self.container, self.name)] = data

    def delete_blob(self):
        self.store.pop((self.container, self.name), None)

    def download_blob(self):
        if (self.container, self.name) not in self.store:
            raise KeyError(self.name)
        data = self.store[(self.container, self.name)]
        return types.SimpleNamespace(readall=lambda: data)


class FakeContainer:
    def __init__(self, store, name):
        self.store, self.name = store, name

    def get_blob_client(self, blob):
        return FakeBlobClient(self.store, self.name, blob)

    def list_blobs(self, name_starts_with=""):
        return [
            types.SimpleNamespace(name=n, size=len(v))
            for (c, n), v in self.store.items()
            if c == self.name and n.startswith(name_starts_with)
        ]


class FakeBlobService:
    def __init__(self):
        self.store = {}

    def get_container_client(self, name):
        return FakeContainer(self.store, name)

    def get_blob_client(self, container, blob):
        return FakeBlobClient(self.store, container, blob)


class FakeTable:
    def __init__(self, rows=()):
        self.rows = list(rows)

    def query_entities(self, query_filter, parameters=None):
        if parameters and "pk" in parameters:
            return [r for r in self.rows if r["PartitionKey"] == parameters["pk"]]
        return list(self.rows)

    def list_entities(self):
        return list(self.rows)

    def delete_entity(self, partition_key, row_key):
        self.rows = [r for r in self.rows if not (r["PartitionKey"] == partition_key and r["RowKey"] == row_key)]

    def update_entity(self, entity, mode=None, **_conditions):
        row = next(r for r in self.rows if r["PartitionKey"] == entity["PartitionKey"] and r["RowKey"] == entity["RowKey"])
        row.update(entity)

    def upsert_entity(self, entity):
        self.delete_entity(entity["PartitionKey"], entity["RowKey"])
        self.rows.append(dict(entity))


class FakeBus:
    def __init__(self):
        self.sent = []

    def send(self, body, *, message_id, correlation_id):
        self.sent.append({"body": json.loads(body), "message_id": message_id, "correlation_id": correlation_id})


@unittest.skipIf(LedgerRepository is None, "Azure SDKs not installed")
class RepositoryTests(unittest.TestCase):
    def _repo(self, rows=(), bus=True):
        self.blob = FakeBlobService()
        self.bus = FakeBus() if bus else None
        repo = LedgerRepository(table=FakeTable(rows), blob=self.blob, events=FakeTable(), bus=self.bus or object(), incoming_container="incoming")
        if not bus:
            repo._bus = None
        return repo

    def test_upload_writes_blob_under_processing_id_and_enqueues_reference(self):
        repo = self._repo()
        result = repo.upload("..\\ACH REMIT.msg", b"hello")
        pid = result["processingId"]
        self.assertTrue(pid.startswith("REM-"))
        self.assertIn(("incoming", f"{pid}/ACH REMIT.msg"), self.blob.store)
        message = self.bus.sent[0]
        self.assertEqual(message["message_id"], pid)
        self.assertEqual(message["body"]["sourceBlobName"], f"{pid}/ACH REMIT.msg")
        self.assertNotIn("content", message["body"])  # claim-check: never the bytes
        self.assertEqual(result["status"], "Queued")

    def test_upload_without_bus_raises(self):
        repo = self._repo(bus=False)
        with self.assertRaises(QueueUnavailable):
            repo.upload("a.msg", b"x")

    def test_get_run_reports_queued_before_function_writes_ledger_row(self):
        repo = self._repo()
        pid = repo.upload("a.msg", b"hello")["processingId"]
        run = repo.get_run(pid)
        self.assertEqual(run["status"], "Queued")
        self.assertEqual(run["sourceName"], "a.msg")
        self.assertIsNone(run["runId"])

    def test_get_run_unknown_id_is_none(self):
        self.assertIsNone(self._repo().get_run("REM-0123456789ABCDEF0123"))

    def test_delete_removes_rows_events_and_all_blobs(self):
        pid = "REM-0123456789ABCDEF0123"
        other = "REM-FEDCBA98765432100000"
        repo = self._repo(rows=[
            {"PartitionKey": pid, "RowKey": "r1"},
            {"PartitionKey": pid, "RowKey": "r2"},
            {"PartitionKey": other, "RowKey": "r1"},
        ])
        repo._events.rows = [
            {"PartitionKey": pid, "RowKey": "r1_0001"},
            {"PartitionKey": other, "RowKey": "r1_0001"},
        ]
        self.blob.store[("incoming", f"{pid}/a.msg")] = b"x"
        self.blob.store[("analysis", f"{pid}/r1/doc-001/norm.json")] = b"{}"
        self.blob.store[("manifests", f"{pid}.json")] = b"{}"
        self.blob.store[("incoming", f"{other}/a.msg")] = b"keep"

        result = repo.delete(pid)

        self.assertEqual(result["deleted"], {"runs": 2, "events": 1, "blobs": 3})
        # The target's rows, events and blobs are gone...
        self.assertEqual([r for r in repo._table.rows if r["PartitionKey"] == pid], [])
        self.assertEqual([r for r in repo._events.rows if r["PartitionKey"] == pid], [])
        self.assertNotIn(("incoming", f"{pid}/a.msg"), self.blob.store)
        self.assertNotIn(("manifests", f"{pid}.json"), self.blob.store)
        # ...but a different remittance is untouched.
        self.assertIn(("incoming", f"{other}/a.msg"), self.blob.store)
        self.assertTrue(any(r["PartitionKey"] == other for r in repo._table.rows))

    def test_delete_unknown_id_returns_none(self):
        self.assertIsNone(self._repo().delete("REM-0123456789ABCDEF0123"))

    def test_delete_rejects_malformed_id(self):
        self.assertIsNone(self._repo().delete("not-a-valid-id"))

    def test_reprocess_uses_unique_message_id_and_force(self):
        pid = "REM-0123456789ABCDEF0123"
        repo = self._repo(rows=[{"PartitionKey": pid, "RowKey": "r1", "status": "ReviewRequired", "sourceName": "a.msg", "updatedAtUtc": "x"}])
        self.blob.store[("incoming", f"{pid}/a.msg")] = b"bytes"
        result = repo.reprocess(pid)
        self.assertEqual(result["processingId"], pid)
        sent = self.bus.sent[0]
        self.assertTrue(sent["message_id"].startswith(f"{pid}:"))
        self.assertTrue(sent["body"]["force"])
        self.assertEqual(sent["body"]["stream"], "reprocess")

    # --- analyst review --------------------------------------------------------

    PID = "REM-0123456789ABCDEF0123"

    def _review_repo(self, **row):
        run = {"PartitionKey": self.PID, "RowKey": "r2", "status": "ReviewRequired", "updatedAtUtc": "2026-01-02T00:00:00",
               "processedDate": "2026-01-02", "finalResultBlobUri": f"results/{self.PID}/r2/final-remittance.json", **row}
        repo = self._repo(rows=[{"PartitionKey": self.PID, "RowKey": "r1", "status": "Failed", "updatedAtUtc": "2026-01-01T00:00:00"}, run])
        repo._events.rows = [{"PartitionKey": self.PID, "RowKey": "r2_0003", "runId": "r2", "seq": 3}]
        return repo

    def _final_result(self, repo, lines):
        final = {"runId": "r2", "paymentLines": lines,
                 "remittanceMetadata": {"payerName": "Acme Freight, Inc.", "remittanceDate": "2026-01-02", "paymentTotal": "112.5"}}
        repo._blob.store[("results", f"{self.PID}/r2/final-remittance.json")] = json.dumps(final).encode()

    def test_complete_review_merges_fields_and_appends_event(self):
        repo = self._review_repo()
        self._final_result(repo, [{"proInvoiceNumber": "001234567", "amountPaid": "100"},
                                  {"proInvoiceNumber": "001234568", "amountPaid": "12.5"}])
        result = repo.complete_review(self.PID, reviewer="Analyst One", outcome="approved", notes="Lines match the advice.")
        self.assertEqual(result["runId"], "r2")
        row = next(r for r in repo._table.rows if r["RowKey"] == "r2")
        self.assertEqual(row["reviewStatus"], "Completed")
        self.assertEqual(row["reviewedBy"], "Analyst One")
        self.assertEqual(row["status"], "ReviewRequired")  # pipeline status is untouched
        event = next(e for e in repo._events.rows if e.get("event") == "review_completed")
        self.assertEqual((event["RowKey"], event["seq"]), ("r2_0004", 4))
        self.assertEqual(json.loads(event["detailsJson"])["outcome"], "approved")
        # Approving releases the downstream CSV the pipeline held back.
        self.assertEqual(row["csvFileName"], "Acme-Freight-Inc_2026-01-02_112.50.csv")
        self.assertEqual(row["csvBlobUri"], f"results/{self.PID}/r2/Acme-Freight-Inc_2026-01-02_112.50.csv")
        self.assertEqual(repo.get_csv(self.PID), (row["csvFileName"], b"001234567,100.00\n001234568,12.50\n"))

    def test_complete_review_handled_manually_writes_no_csv(self):
        repo = self._review_repo()
        self._final_result(repo, [{"proInvoiceNumber": "001234567", "amountPaid": "100"}])
        repo.complete_review(self.PID, reviewer="Analyst One", outcome="handled_manually", notes="Fixed a PRO in the ERP.")
        row = next(r for r in repo._table.rows if r["RowKey"] == "r2")
        self.assertEqual(row["reviewOutcome"], "handled_manually")
        self.assertNotIn("csvBlobUri", row)

    def test_complete_review_approved_needs_complete_lines(self):
        repo = self._review_repo()
        self._final_result(repo, [{"proInvoiceNumber": "", "amountPaid": "100"}])
        with self.assertRaises(ReviewConflict):
            repo.complete_review(self.PID, reviewer="Analyst One", outcome="approved", notes="ok then")
        row = next(r for r in repo._table.rows if r["RowKey"] == "r2")
        self.assertNotIn("reviewStatus", row)

    def test_complete_review_twice_conflicts(self):
        repo = self._review_repo(reviewStatus="Completed", reviewedBy="Analyst One", reviewedAtUtc="t")
        with self.assertRaises(ReviewConflict):
            repo.complete_review(self.PID, reviewer="Analyst Two", outcome="rejected", notes="dup")

    def test_complete_review_requires_review_required_status(self):
        repo = self._review_repo(status="Completed")
        with self.assertRaises(ReviewConflict):
            repo.complete_review(self.PID, reviewer="Analyst One", outcome="rejected", notes="n/a")
        self.assertIsNone(repo.complete_review("REM-FEDCBA98765432100000", reviewer="A B", outcome="rejected", notes="n/a"))

    def test_list_review_runs_filters_open_and_completed(self):
        other = "REM-FEDCBA98765432100000"
        repo = self._review_repo()
        repo._table.rows.append({"PartitionKey": other, "RowKey": "r1", "status": "ReviewRequired", "reviewStatus": "Completed",
                                 "updatedAtUtc": "2026-01-03T00:00:00"})
        self.assertEqual([r["processingId"] for r in repo.list_review_runs(review="open")], [self.PID])
        self.assertEqual([r["processingId"] for r in repo.list_review_runs(review="completed")], [other])

    def test_source_text_reads_body_with_header_and_document_markdown(self):
        repo = self._review_repo()
        self.blob.store[("manifests", f"{self.PID}.json")] = json.dumps(
            {"message": {"subject": "Remittance 1001", "senderEmail": "ap@example.com"}}).encode()
        self.blob.store[("bodies", f"{self.PID}/message-body.txt")] = b"Please see attached."
        self.blob.store[("results", f"{self.PID}/r2/final-remittance.json")] = json.dumps(
            {"candidateResults": [{"candidateId": "doc-001", "rawAnalysisBlobUri": f"analysis/{self.PID}/r2/doc-001/raw.json"}]}).encode()
        self.blob.store[("analysis", f"{self.PID}/r2/doc-001/raw.json")] = json.dumps(
            {"result": {"contents": [{"markdown": "| PRO | Paid |\n| 1234567 | 10.00 |"}]}}).encode()

        body = repo.get_source_text(self.PID, "message-body", 0)
        self.assertTrue(body["text"].startswith("Subject: Remittance 1001"))
        self.assertIn("Please see attached.", body["text"])
        doc = repo.get_source_text(self.PID, "doc-001", 0)
        self.assertIn("1234567", doc["text"])
        self.assertIsNone(doc["nextOffset"])
        self.assertIsNone(repo.get_source_text(self.PID, "doc-009", 0))


if __name__ == "__main__":
    unittest.main()
