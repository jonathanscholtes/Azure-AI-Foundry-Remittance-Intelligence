import json
import os
import sys
import types
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

try:
    from app.repository import LedgerRepository, QueueUnavailable
except ImportError:  # Azure SDKs not installed locally
    LedgerRepository = None


class FakeBlobClient:
    def __init__(self, store, container, name):
        self.store, self.container, self.name = store, container, name

    def upload_blob(self, data, overwrite=False):
        self.store[(self.container, self.name)] = data

    def delete_blob(self):
        self.store.pop((self.container, self.name), None)


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


class FakeTable:
    def __init__(self, rows=()):
        self.rows = list(rows)

    def query_entities(self, query_filter, parameters=None):
        if parameters and "pk" in parameters:
            return [r for r in self.rows if r["PartitionKey"] == parameters["pk"]]
        return list(self.rows)

    def delete_entity(self, partition_key, row_key):
        self.rows = [r for r in self.rows if not (r["PartitionKey"] == partition_key and r["RowKey"] == row_key)]


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


if __name__ == "__main__":
    unittest.main()
