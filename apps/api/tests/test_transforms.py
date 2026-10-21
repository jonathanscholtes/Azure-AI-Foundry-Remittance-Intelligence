import os
import sys
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.transforms import (
    aggregate,
    artifact_allowed,
    by_payer,
    entity_to_summary,
    event_entity_to_event,
    latest_per_processing,
    pipeline_snapshot,
    to_float,
)


class TransformTests(unittest.TestCase):
    def test_to_float_handles_currency_text(self):
        self.assertEqual(to_float("$1,234.50"), 1234.50)
        self.assertEqual(to_float(42), 42.0)
        self.assertIsNone(to_float(None))
        self.assertIsNone(to_float("n/a"))
        self.assertIsNone(to_float(True))

    def test_entity_to_summary_splits_warnings_and_casts(self):
        summary = entity_to_summary(
            {
                "PartitionKey": "REM-0123456789ABCDEF0123",
                "RowKey": "run-1",
                "status": "ReviewRequired",
                "requiresReview": True,
                "warnings": "missing_payer_name, payment_total_mismatch",
                "lineCount": 5,
                "calculatedPaymentTotal": 100.0,
            }
        )
        self.assertEqual(summary["processingId"], "REM-0123456789ABCDEF0123")
        self.assertEqual(summary["warnings"], ["missing_payer_name", "payment_total_mismatch"])
        self.assertTrue(summary["requiresReview"])
        self.assertEqual(summary["lineCount"], 5)

    def test_latest_per_processing_keeps_newest_run(self):
        entities = [
            {"PartitionKey": "A", "RowKey": "r1", "updatedAtUtc": "2026-10-16T01:00:00+00:00"},
            {"PartitionKey": "A", "RowKey": "r2", "updatedAtUtc": "2026-10-16T02:00:00+00:00"},
            {"PartitionKey": "B", "RowKey": "r1", "updatedAtUtc": "2026-10-16T00:30:00+00:00"},
        ]
        latest = {e["PartitionKey"]: e for e in latest_per_processing(entities)}
        self.assertEqual(latest["A"]["RowKey"], "r2")
        self.assertEqual(len(latest), 2)

    def test_aggregate_computes_kpis(self):
        runs = [
            {"status": "Completed", "requiresReview": False, "calculatedPaymentTotal": 100.0, "lineCount": 2, "durationMs": 1000, "warnings": []},
            {"status": "ReviewRequired", "requiresReview": True, "calculatedPaymentTotal": 50.0, "lineCount": 1, "durationMs": 3000, "warnings": ["missing_payer_name"]},
            {"status": "Failed", "requiresReview": False, "warnings": []},
        ]
        agg = aggregate(runs, scope={"type": "daily", "date": "2026-10-16"})
        self.assertEqual(agg["count"], 3)
        self.assertEqual(agg["completed"], 1)
        self.assertEqual(agg["reviewRequired"], 1)
        self.assertEqual(agg["failed"], 1)
        self.assertEqual(agg["totalProcessedAmount"], 150.0)
        self.assertEqual(agg["totalPaymentLines"], 3)
        self.assertEqual(agg["avgDurationMs"], 2000)
        self.assertEqual(agg["topWarnings"][0], {"warning": "missing_payer_name", "count": 1})

    def test_top_warnings_count_review_reasons_not_notes(self):
        new = entity_to_summary({"warnings": "payment_total_mismatch, pro_paid_across_payments", "reviewReasons": "payment_total_mismatch"})
        old = entity_to_summary({"warnings": "missing_payer_name"})
        self.assertEqual(new["reviewReasons"], ["payment_total_mismatch"])
        self.assertIsNone(old["reviewReasons"])
        top = {w["warning"] for w in aggregate([new, old], scope={})["topWarnings"]}
        self.assertEqual(top, {"payment_total_mismatch", "missing_payer_name"})

    def test_by_payer_rolls_up_lines_amounts_and_review(self):
        runs = [
            {"payerName": "Acme", "status": "Completed", "lineCount": 2, "paymentTotal": 100.0, "calculatedPaymentTotal": 100.0, "isReconciled": True, "remittanceDate": "2026-10-01"},
            {"payerName": "Acme", "status": "ReviewRequired", "lineCount": 3, "paymentTotal": 90.0, "calculatedPaymentTotal": 100.0, "totalDifference": -10.0, "isReconciled": False, "amountAtRisk": 10.0, "remittanceDate": "2026-10-05"},
            {"payerName": None, "status": "Failed"},
        ]
        rows = {r["payerName"]: r for r in by_payer(runs)}
        acme = rows["Acme"]
        self.assertEqual(acme["runs"], 2)
        self.assertEqual(acme["lines"], 5)
        self.assertEqual(acme["calculatedAmount"], 200.0)
        self.assertEqual(acme["unreconciledAmount"], 10.0)
        self.assertEqual(acme["reviewRequired"], 1)
        self.assertEqual(acme["lastRemittanceDate"], "2026-10-05")
        self.assertEqual(rows["(unknown payer)"]["failed"], 1)

    def test_pipeline_snapshot_counts_in_flight_and_buckets(self):
        now = datetime(2026, 10, 16, 12, 0, tzinfo=timezone.utc)
        runs = [
            {"status": "Extracting", "currentState": "Extracting"},
            {"status": "AssuranceReview", "currentState": "AssuranceReview"},
            {"status": "Completed", "processedAtUtc": (now - timedelta(minutes=2)).isoformat(), "durationMs": 4000, "queueLatencyMs": 500},
            {"status": "ReviewRequired", "processedAtUtc": (now - timedelta(minutes=30)).isoformat(), "durationMs": 9000},
            {"status": "Completed", "processedAtUtc": (now - timedelta(hours=3)).isoformat()},
        ]
        snap = pipeline_snapshot(runs, now=now, window_minutes=60)
        self.assertEqual(snap["inFlight"], 2)
        self.assertEqual(snap["inFlightByState"], {"Extracting": 1, "AssuranceReview": 1})
        self.assertEqual(snap["processedInWindow"], 2)
        self.assertEqual(len(snap["buckets"]), 12)
        self.assertEqual(snap["buckets"][-1]["completed"], 1)
        self.assertEqual(snap["avgQueueLatencyMs"], 500)

    def test_artifact_access_is_scoped_to_processing_id(self):
        pid = "REM-0123456789ABCDEF0123"
        self.assertTrue(artifact_allowed("attachments", f"{pid}/doc-001/REMITTANCE.pdf", pid))
        self.assertTrue(artifact_allowed("manifests", f"{pid}.json", pid))
        self.assertFalse(artifact_allowed("attachments", "REM-FFFFFFFFFFFFFFFFFFFF/doc-001/x.pdf", pid))
        self.assertFalse(artifact_allowed("attachments", f"{pid}/../other", pid))
        self.assertFalse(artifact_allowed("deployments", f"{pid}/x", pid))

    def test_event_entity_parses_details(self):
        event = event_entity_to_event({"seq": 3, "newState": "Extracted", "detailsJson": '{"durationMs": 1200}'})
        self.assertEqual(event["details"]["durationMs"], 1200)
        self.assertEqual(event["newState"], "Extracted")


if __name__ == "__main__":
    unittest.main()
