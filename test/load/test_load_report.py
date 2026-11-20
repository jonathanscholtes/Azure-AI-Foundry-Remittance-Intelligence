"""Offline checks for load_report.summarize (no API): python test/load/test_load_report.py"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import load_report  # noqa: E402

ALIASES = {"cu-gpt-55": "gpt-5.5", "reasoning-gpt-56-terra": "gpt-5.6-terra"}


def _run(outcome, *, standard=0, minimal=1, agents=None, cost=0.01, lines=3):
    usage = {
        "contentUnderstanding": {"calls": 1, "pages": {k: v for k, v in (("standard", standard), ("minimal", minimal)) if v},
                                 "contextualizationTokens": 1000 * (standard + minimal),
                                 "tokens": {"cu-gpt-55-input": 1000, "cu-gpt-55-cached-input": 200, "cu-gpt-55-output": 100}},
        "agents": agents or {},
        "estimate": {"total": cost, "byComponent": {"contentUnderstanding": cost}},
    }
    return {"outcome": outcome, "lineCount": lines, "durationMs": 1000, "queueLatencyMs": 100, "cuCalls": 1,
            "agentCalls": sum(a["calls"] for a in (agents or {}).values()), "decisionCalls": 0, "usage": usage}


class SummarizeTests(unittest.TestCase):
    def setUp(self):
        resolver = {"ach-resolution-expert": {"calls": 2, "model": "reasoning-gpt-56-terra", "inputTokens": 500, "outputTokens": 50}}
        self.runs = [
            _run("handled_by_rules", standard=3, minimal=2),
            _run("handled_by_rules", standard=7),
            _run("flagged_for_review", agents=resolver),
        ]
        self.summary = load_report.summarize(self.runs, ALIASES)

    def test_counts_and_shares_are_per_message(self):
        s = self.summary
        self.assertEqual(s["messages"], 3)  # not overwritten by any page count
        self.assertEqual(s["outcomes"], {"first pass": 2, "agent-resolved": 0, "review": 1, "failed": 0})
        self.assertEqual(s["shares"]["anyAiPct"], 100.0)
        self.assertEqual(s["shares"]["agentCallsPct"], 33.3)
        self.assertEqual(s["shares"]["resolutionPct"], 33.3)
        self.assertEqual(s["shares"]["humanReviewPct"], 33.3)
        for value in s["shares"].values():
            self.assertLessEqual(value, 100.0)
        self.assertEqual((s["shareCounts"]["humanReview"], s["shareCounts"]["resolution"]), (1, 1))
        self.assertEqual(s["outcomePcts"]["first pass"], 66.7)
        self.assertEqual(s["lines"]["avg"], 3.0)

    def test_pages_are_split_by_tier(self):
        cu = self.summary["cu"]
        self.assertEqual(cu["pagesTotal"], {"minimal": 4, "standard": 10})
        self.assertEqual(cu["pagesPerMessage"], {"minimal": 1.3, "standard": 3.3})

    def test_tokens_and_cost(self):
        tokens = self.summary["tokensPerMessage"]
        self.assertEqual(tokens["gpt-5.5"]["usedBy"], 3)
        self.assertEqual(tokens["gpt-5.6-terra"]["usedBy"], 1)
        self.assertEqual(self.summary["cost"]["total"], 0.03)
        self.assertEqual(self.summary["cost"]["perMessage"], 0.01)

    def test_percentile_is_nearest_rank(self):
        self.assertEqual(load_report.percentile([52.8, 10.0], 0.5), 10.0)
        self.assertEqual(load_report.percentile([52.8, 10.0], 0.95), 52.8)
        self.assertIsNone(load_report.percentile([], 0.5))


if __name__ == "__main__":
    unittest.main()
