import unittest

from shared import run_metrics


def _source(strategy, lines, *, pages=0, chunks=1):
    return {
        "strategy": strategy,
        "lineCount": lines,
        "normalized": {"chunkCount": chunks},
        "usage": {"contentUnderstanding": {"pages": {"standard": pages}}} if pages else {},
    }


class MessageClassTests(unittest.TestCase):
    def test_each_class_from_what_produced_the_lines(self):
        self.assertEqual(run_metrics.message_class([_source("html_table+content_understanding", 12)]), "A")
        self.assertEqual(run_metrics.message_class([_source("text_table+content_understanding", 3)]), "A")
        self.assertEqual(run_metrics.message_class([_source("deterministic_parser", 400)]), "B")
        self.assertEqual(run_metrics.message_class([_source("content_understanding", 6, pages=1)]), "C")
        self.assertEqual(run_metrics.message_class([_source("content_understanding", 150, pages=2)]), "D")
        self.assertEqual(run_metrics.message_class([_source("content_understanding", 20, pages=9)]), "D")
        self.assertEqual(run_metrics.message_class([_source("content_understanding", 20, chunks=3)]), "D")

    def test_the_most_expensive_source_wins_and_sources_without_lines_dont_count(self):
        sources = [_source("deterministic_parser", 30), _source("content_understanding", 4, pages=1), _source("content_understanding", 0, pages=3)]
        self.assertEqual(run_metrics.message_class(sources), "C")
        self.assertEqual(run_metrics.message_class([_source("content_understanding", 0)]), "none")
        self.assertEqual(run_metrics.message_class([]), "none")


class OutcomeTests(unittest.TestCase):
    def test_every_run_lands_in_one_bucket(self):
        self.assertEqual(run_metrics.outcome("Completed"), "handled_by_rules")
        self.assertEqual(run_metrics.outcome("Completed", {"status": "accepted"}), "handled_by_agents")
        self.assertEqual(run_metrics.outcome("Completed", {"status": "accepted_details", "details": {"applied": {"remittanceDate": {}}}}), "handled_by_agents")
        self.assertEqual(run_metrics.outcome("Completed", {"status": "rejected", "details": {"applied": {}}}), "handled_by_rules")
        self.assertEqual(run_metrics.outcome("ReviewRequired", {"status": "accepted"}), "flagged_for_review")
        self.assertEqual(run_metrics.outcome("Failed"), "failed")
        self.assertEqual(run_metrics.outcome("DuplicateSkipped"), "duplicate")


class RunEventTests(unittest.TestCase):
    def test_event_carries_ledger_facts_steps_and_cost(self):
        ledger = {"status": "Completed", "payerName": "Fabrikam Logistics", "lineCount": 3, "messageClass": "C",
                  "outcome": "handled_by_rules", "estimatedCost": 0.02, "warnings": ["x"], "csvBlobUri": "results/x.csv"}
        event = run_metrics.run_event("REM-1", "run-1", "corr-1", ledger, steps={"intake": 900, "resolution": None},
                                      cost={"byComponent": {"contentUnderstanding": 0.02}, "pricingVersion": "v", "basis": "list"})
        self.assertEqual(event["event"], "remittance_run_completed")
        self.assertEqual((event["processingId"], event["payerName"], event["messageClass"]), ("REM-1", "Fabrikam Logistics", "C"))
        self.assertEqual(event["stepDurationsMs"], {"intake": 900})
        self.assertEqual(event["costByComponent"], {"contentUnderstanding": 0.02})
        self.assertNotIn("warnings", event)     # only the listed fields
        self.assertNotIn("csvBlobUri", event)


if __name__ == "__main__":
    unittest.main()
