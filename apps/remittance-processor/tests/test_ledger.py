import unittest

from shared.ledger import build_entity, upsert_run


class FakeTableClient:
    def __init__(self, fail=False):
        self.fail = fail
        self.entities = []

    def upsert_entity(self, entity):
        if self.fail:
            raise RuntimeError("table unavailable")
        self.entities.append(entity)


class LedgerTests(unittest.TestCase):
    def test_build_entity_keys_and_flattening(self):
        entity = build_entity(
            "REM-ABC",
            "run-1",
            {
                "status": "Completed",
                "warnings": ["a", "b"],
                "requiresReview": False,
                "lineCount": 3,
                "paymentTotal": 100.5,
                "totalDifference": None,
            },
        )
        self.assertEqual(entity["PartitionKey"], "REM-ABC")
        self.assertEqual(entity["RowKey"], "run-1")
        self.assertEqual(entity["warnings"], "a, b")
        self.assertEqual(entity["status"], "Completed")
        self.assertIs(entity["requiresReview"], False)
        self.assertEqual(entity["lineCount"], 3)
        # None values are omitted so a merge upsert never clears a prior field.
        self.assertNotIn("totalDifference", entity)

    def test_upsert_run_writes_entity(self):
        client = FakeTableClient()
        ok = upsert_run(client, "REM-ABC", "run-1", status="Parsed")
        self.assertTrue(ok)
        self.assertEqual(len(client.entities), 1)
        self.assertEqual(client.entities[0]["status"], "Parsed")

    def test_upsert_run_is_best_effort(self):
        self.assertFalse(upsert_run(None, "REM-ABC", "run-1", status="Parsed"))
        self.assertFalse(upsert_run(FakeTableClient(), "", "run-1", status="Parsed"))
        self.assertFalse(upsert_run(FakeTableClient(fail=True), "REM-ABC", "run-1", status="Parsed"))


if __name__ == "__main__":
    unittest.main()
