import unittest
from types import SimpleNamespace

from shared import usage

# The shape the analyze operation reports (from a live call on a short synthetic text).
CU_OPERATION_USAGE = {
    "documentPagesMinimal": 1,
    "contextualizationTokens": 1000,
    "tokens": {"gpt-5.5-input": 2144, "gpt-5.5-cached-input": 1792, "gpt-5.5-output": 133},
}

PRICING = {
    "version": "test",
    "currency": "USD",
    "contentUnderstanding": {"pagesPer1000": {"minimal": 1.0, "basic": 2.0, "standard": 10.0}, "contextualizationPer1M": 2.0},
    "models": {
        "gpt-5.5": {"aliases": ["cu-gpt-55"], "inputPer1M": 1.0, "cachedInputPer1M": 0.5, "outputPer1M": 10.0},
        "gpt-5.6-terra": {"aliases": ["reasoning-gpt-56-terra"], "inputPer1M": 2.0, "cachedInputPer1M": 1.0, "outputPer1M": 20.0},
        "Microsoft-Decision-1": {"aliases": ["intake-decision"], "inputPer1M": 0.1, "outputPer1M": 0.4},
    },
}


def _response(model, input_tokens, output_tokens, cached=0, reasoning=0):
    return SimpleNamespace(
        model=model,
        usage=SimpleNamespace(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            input_tokens_details=SimpleNamespace(cached_tokens=cached),
            output_tokens_details=SimpleNamespace(reasoning_tokens=reasoning),
        ),
    )


class UsageTests(unittest.TestCase):
    def test_cu_operation_usage_is_normalized(self):
        block = usage.cu_from_operation(CU_OPERATION_USAGE)
        self.assertEqual(block["calls"], 1)
        self.assertEqual(block["pages"], {"minimal": 1})
        self.assertEqual(block["contextualizationTokens"], 1000)
        self.assertEqual(block["tokens"]["gpt-5.5-cached-input"], 1792)
        self.assertIsNone(usage.cu_from_operation(None))

    def test_chunks_and_activities_add_up(self):
        one = usage.cu_from_operation(CU_OPERATION_USAGE)
        merged = usage.merge({"contentUnderstanding": one}, {"contentUnderstanding": one}, None, {})
        cu = merged["contentUnderstanding"]
        self.assertEqual((cu["calls"], cu["pages"]["minimal"], cu["contextualizationTokens"]), (2, 2, 2000))

    def test_agent_turns_and_decision_calls_are_collected(self):
        with usage.collect() as outer:
            usage.record_agent_response("ach-resolution-expert", _response("gpt-5.6-terra", 5000, 900, cached=1000, reasoning=700))
            with usage.collect() as inner:  # a nested activity call (e.g. re-validation) also counts outward
                usage.record_agent_response("ach-resolution-expert:6", _response("gpt-5.6-terra", 6000, 300))
                usage.record_decision_response({"model": "Microsoft-Decision-1", "usage": {"input_tokens": 800, "output_tokens": 20}}, "intake-decision")
        expert = outer["agents"]["ach-resolution-expert"]
        self.assertEqual((expert["calls"], expert["inputTokens"], expert["outputTokens"], expert["reasoningTokens"]), (2, 11000, 1200, 700))
        self.assertEqual(expert["model"], "gpt-5.6-terra")
        self.assertEqual(inner["decision"]["calls"], 1)
        self.assertEqual(outer["decision"]["inputTokens"], 800)

    def test_recording_outside_a_collector_is_a_no_op(self):
        usage.record_agent_response("ach-assurance-expert", _response("gpt-5.5", 10, 10))
        usage.record_cu({"calls": 1})

    def test_estimate_prices_each_meter_and_excludes_cached_from_input(self):
        block = {
            "contentUnderstanding": usage.cu_from_operation(CU_OPERATION_USAGE),
            "agents": {"ach-assurance-expert": {"calls": 1, "model": "cu-gpt-55", "inputTokens": 3000, "cachedInputTokens": 1000, "outputTokens": 500}},
            "decision": {"calls": 1, "model": "intake-decision", "inputTokens": 1000, "outputTokens": 100},
        }
        estimate = usage.estimate(block, PRICING)
        cu = (1 / 1000 * 1.0) + (1000 / 1e6 * 2.0) + ((2144 - 1792) / 1e6 * 1.0) + (1792 / 1e6 * 0.5) + (133 / 1e6 * 10.0)
        agent = (2000 / 1e6 * 1.0) + (1000 / 1e6 * 0.5) + (500 / 1e6 * 10.0)
        decision = (1000 / 1e6 * 0.1) + (100 / 1e6 * 0.4)
        self.assertAlmostEqual(estimate["byComponent"]["contentUnderstanding"], cu, places=6)
        self.assertAlmostEqual(estimate["byComponent"]["ach-assurance-expert"], agent, places=6)
        self.assertAlmostEqual(estimate["byComponent"]["decision"], decision, places=6)
        self.assertAlmostEqual(estimate["total"], cu + agent + decision, places=5)
        self.assertTrue(estimate["complete"])
        self.assertEqual(estimate["pricingVersion"], "test")

    def test_missing_rates_are_listed_and_the_estimate_marked_incomplete(self):
        block = {"contentUnderstanding": usage.cu_from_operation(CU_OPERATION_USAGE)}
        estimate = usage.estimate(block, {"models": {}, "contentUnderstanding": {}})
        self.assertFalse(estimate["complete"])
        self.assertIn("cu.pages.minimal", estimate["missingRates"])
        self.assertIn("model.gpt-5.5.outputPer1M", estimate["missingRates"])
        self.assertEqual(estimate["total"], 0.0)

    def test_a_run_without_ai_calls_costs_nothing(self):
        estimate = usage.estimate({}, PRICING)
        self.assertEqual((estimate["total"], estimate["complete"]), (0.0, True))

    def test_totals_for_the_ledger(self):
        block = {
            "contentUnderstanding": usage.cu_from_operation(CU_OPERATION_USAGE),
            "agents": {"ach-extraction-expert": {"calls": 2, "inputTokens": 100, "outputTokens": 10}},
            "decision": {"calls": 1, "inputTokens": 50, "outputTokens": 5},
            "estimate": {"total": 0.0123, "complete": True},
        }
        totals = usage.totals(block)
        self.assertEqual(totals["cuCalls"], 1)
        self.assertEqual(totals["cuPages"], 1)
        self.assertEqual(totals["agentCalls"], 2)
        self.assertEqual(totals["decisionCalls"], 1)
        self.assertEqual(totals["aiTokens"], 2144 + 133 + 110 + 55)  # cached input is part of input
        self.assertEqual(totals["estimatedCost"], 0.0123)

    def test_the_shipped_price_table_loads_and_says_what_its_rates_are(self):
        pricing = usage.load_pricing()
        self.assertIn("gpt-5.5", pricing["models"])
        self.assertIn("standard", pricing["contentUnderstanding"]["pagesPer1000"])
        estimate = usage.estimate({"decision": {"calls": 1, "model": "intake-decision", "inputTokens": 10, "outputTokens": 1}}, pricing)
        self.assertIn("list prices", estimate["basis"])
        self.assertIn("Estimate only", estimate["disclaimer"])


if __name__ == "__main__":
    unittest.main()
