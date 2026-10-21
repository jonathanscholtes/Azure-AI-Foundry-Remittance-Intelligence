import os
import unittest
from unittest.mock import patch

from assurance.case import build_case, thread_excerpt
from assurance.experts import agent_reference, parse_agent_json, run_expert
from assurance.fusion import fuse
from assurance.triage import MAX_ISSUE_LINES, rule_finding, triage

FINAL = {
    "processingId": "REM-X",
    "remittanceMetadata": {"payerName": "Acme", "paymentTotal": 90.0, "currency": "USD"},
    "paymentLines": [{"proInvoiceNumber": "001", "amountPaid": 100.0}],
    "quality": {"lineCount": 1, "calculatedPaymentTotal": 100.0, "totalDifference": -10.0, "warnings": ["payment_total_mismatch"]},
}


def _line(pro, amount, *warnings, raw=None):
    line = {"proInvoiceNumber": pro, "amountPaid": amount, "warnings": list(warnings)}
    if raw:
        line["rawProInvoiceNumber"] = raw
    return line


# A run like the reprocessed Wingtip Freight remittance: reconciled, but with PRO-format, credit and
# conflicting-PRO issues.
MIXED = {
    "processingId": "REM-M",
    "remittanceMetadata": {"payerName": "Wingtip Freight US LLC", "paymentTotal": 1000.0},
    "paymentLines": [
        _line("03700000201", 500.0),
        _line(None, 25.0, "invalid_pro_number", raw="03700000201-X1"),
        _line("05100000301", -4.25, "negative_amount_paid"),
        _line("32400001101", 554.92),
        _line("32400001101", 600.00, "conflicting_amount_for_pro"),
    ],
    "quality": {"lineCount": 5, "totalDifference": 0.0, "warnings": ["conflicting_payment_line", "invalid_pro_number", "negative_amount_paid"]},
}

# A run where extraction mis-paired lines: those lines are ours, not the payer's.
MISPAIRED = {
    "processingId": "REM-U",
    "remittanceMetadata": {"payerName": "Wingtip Freight US LLC", "paymentTotal": 1000.0},
    "paymentLines": [
        _line("1", 300.0, "line_not_grounded"),
        _line("2", -5.0, "line_not_grounded", "negative_amount_paid"),
        _line("3", 400.0),
    ],
    "quality": {"lineCount": 3, "totalDifference": 305.0,
                "warnings": ["line_not_grounded", "negative_amount_paid", "payment_total_mismatch"]},
}


class TriageTests(unittest.TestCase):
    def test_splits_issues_by_owner_with_the_lines_involved(self):
        issues = {issue["id"]: issue for issue in triage(MIXED)}
        self.assertEqual(issues["data:invalid_pro_number"]["kind"], "data_quality")
        self.assertEqual(issues["data:invalid_pro_number"]["lines"][0]["rawProInvoiceNumber"], "03700000201-X1")
        self.assertEqual(issues["payer:negative_amount_paid"]["headline"], "1 negative line ($4.25)")
        conflict = issues["payer:conflicting_payment_line"]
        self.assertEqual(conflict["lineCount"], 2)  # both lines of the PRO, not just the second
        self.assertNotIn("payer:payment_total_mismatch", issues)  # reconciled

    def test_extraction_problems_are_not_payer_questions(self):
        issues = triage(MISPAIRED)
        kinds = {issue["id"]: issue["kind"] for issue in issues}
        self.assertEqual(kinds["extraction:line_not_grounded"], "extraction")
        # The only negative line is mis-paired, and the total gap is explained by extraction first.
        self.assertNotIn("payer:negative_amount_paid", kinds)
        self.assertNotIn("payer:payment_total_mismatch", kinds)
        self.assertIn("off from the extracted lines by $305.00", issues[0]["title"])

    def test_unexplained_short_pay_goes_to_the_agent(self):
        issues = triage(FINAL)
        self.assertEqual([(i["id"], i["headline"]) for i in issues], [("payer:payment_total_mismatch", "Lines exceed payment by $10.00")])

    def test_several_payments_in_one_email_are_named(self):
        groups = [{"label": "Total CA", "amount": "1200.00"}, {"label": "Total AZ", "amount": "250.00"}, {"label": "Total CO", "amount": "1915.40"}]
        final = {**MIXED, "paymentGroups": groups,
                 "quality": {**MIXED["quality"], "warnings": MIXED["quality"]["warnings"] + ["multiple_payments_in_email"]}}
        issue = next(i for i in triage(final) if i["id"] == "payer:multiple_payments_in_email")
        self.assertEqual(issue["kind"], "payer")
        self.assertEqual(issue["headline"], "3 payments in one email ($3,365.40)")
        self.assertIn("Total AZ $250.00", issue["title"])
        self.assertIsNone(issue["amount"])  # structural: every dollar reconciles, nothing is at risk

    def test_several_transfers_fold_the_per_payment_conflicts(self):
        transfer = lambda pro: {"proInvoiceNumber": pro, "amountPaid": 2150.0, "source": {"candidateId": "doc-003"}, "warnings": []}
        groups = [{"label": f"Transfer 2026-09-{d}", "amount": 2150.0} for d in ("09", "11", "24")]
        final = {
            "remittanceMetadata": {"payerName": "Acme", "paymentTotal": 2150.0},
            "paymentLines": [transfer("13600000802"), transfer("13600000803"), transfer("13600000804")],
            "paymentGroups": groups,
            "quality": {"totalDifference": -4300.0, "warnings": [
                "conflicting_paymentReference", "conflicting_remittanceDate", "multiple_payments_in_email", "payment_total_mismatch"]},
        }
        issues = triage(final)
        self.assertEqual([i["id"] for i in issues], ["payer:multiple_payments_in_email"])
        self.assertEqual(issues[0]["headline"], "3 payments in one email ($6,450.00)")
        self.assertIn("own date and reference", issues[0]["title"])
        self.assertIn("stated total ($2,150.00) is one payment", issues[0]["title"])

    def test_confirmed_lines_are_not_an_extraction_problem(self):
        final = {**FINAL, "resolution": {"status": "confirmed", "reason": "lines_confirmed", "explanation": "Two separate transfers."}}
        issues = triage(final)
        self.assertEqual([i["id"] for i in issues], ["payer:payment_total_mismatch"])
        self.assertIn("confirmed the lines: Two separate transfers.", issues[0]["title"])
        rejected = {**FINAL, "resolution": {"status": "rejected", "reason": "not_reconciled", "explanation": "x"}}
        self.assertIn("extraction:resolution_unresolved", [i["id"] for i in triage(rejected)])

    def test_payer_recommendation_makes_one_decision_for_the_analyst(self):
        payer = {"value": "Contoso", "sourceId": "doc-003", "evidence": "Remit from: Contoso", "reason": "The remitter on the advice."}
        final = {**FINAL, "quality": {**FINAL["quality"], "totalDifference": 0.0, "warnings": ["conflicting_payerName"]},
                 "resolution": {"status": "recommended", "triggers": ["conflicting_payerName"], "details": {"recommended": {"payerName": payer}}}}
        issues = triage(final)
        self.assertEqual([i["id"] for i in issues], ["data:conflicting_payerName"])  # no extraction finding
        self.assertEqual(issues[0]["headline"], "Confirm payer: Contoso")
        finding = rule_finding(issues[0])
        self.assertIn("Confirm the payer is \"Contoso\"", finding["recommendedAction"])
        self.assertEqual(finding["recommendation"]["evidence"], "Remit from: Contoso")
        rejected = {**final, "resolution": {"status": "rejected", "triggers": ["conflicting_payerName"], "details": {}}}
        self.assertEqual([i["id"] for i in triage(rejected)], ["data:conflicting_payerName"])

    def test_only_a_repeat_within_one_source_is_a_duplicate_payment(self):
        line = {"proInvoiceNumber": "13600000802", "amountPaid": 2150.0, "source": {"candidateId": "doc-003"}, "warnings": []}
        across = {**MIXED, "paymentLines": [line],
                  "quality": {**MIXED["quality"], "duplicateGroups": [{"duplicateOfLineIndex": 0, "duplicateCandidateId": "doc-006"}]}}
        self.assertNotIn("payer:duplicate_payment", {i["id"] for i in triage(across)})
        within = {**across, "quality": {**across["quality"], "duplicateGroups": [{"duplicateOfLineIndex": 0, "duplicateCandidateId": "doc-003"}]}}
        self.assertIn("payer:duplicate_payment", {i["id"] for i in triage(within)})

    def test_issue_lines_are_capped(self):
        many = {**MIXED, "paymentLines": [_line(str(i), -1.0, "negative_amount_paid") for i in range(80)]}
        issue = next(i for i in triage(many) if i["id"] == "payer:negative_amount_paid")
        self.assertEqual(len(issue["lines"]), MAX_ISSUE_LINES)
        self.assertTrue(issue["linesTruncated"])
        self.assertEqual(issue["lineCount"], 80)


class CaseTests(unittest.TestCase):
    def test_case_carries_only_payer_issues_and_lists_internal_ones(self):
        case = build_case(MIXED)
        self.assertEqual({i["id"] for i in case["issues"]}, {"payer:negative_amount_paid", "payer:conflicting_payment_line"})
        self.assertTrue(any("PRO number" in title for title in case["handledInternally"]))
        self.assertNotIn("body", case)
        self.assertNotIn("threadExcerpt", build_case(MIXED, excerpt="We paid on 10/01."))  # not an inquiry thread

    def test_inquiry_thread_case_carries_the_body_excerpt(self):
        inquiry = {**FINAL, "quality": {**FINAL["quality"], "warnings": ["remittance_inquiry_thread"]}}
        extractions = [
            {"candidateId": "doc-001", "excerpt": "Invoice table", "normalized": {"sourceType": "attachment"}},
            {"candidateId": "body", "excerpt": "Our bank run failed; we will re-send the payment.",
             "normalized": {"sourceType": "message_body"}},
        ]
        excerpt = thread_excerpt(extractions)
        self.assertEqual(excerpt, "Our bank run failed; we will re-send the payment.")
        self.assertEqual(build_case(inquiry, excerpt=excerpt)["threadExcerpt"], excerpt)
        self.assertIsNone(thread_excerpt([extractions[0]]))


class ExpertTests(unittest.TestCase):
    def test_agent_reference_pins_version(self):
        self.assertEqual(agent_reference("a:3"), {"type": "agent_reference", "name": "a", "version": "3"})
        self.assertEqual(agent_reference("a"), {"type": "agent_reference", "name": "a"})

    def test_parse_tolerates_fences(self):
        self.assertEqual(parse_agent_json('```json\n{"decision": "x"}\n```'), {"decision": "x"})
        self.assertEqual(parse_agent_json('Here you go: {"decision": "y"} thanks'), {"decision": "y"})
        self.assertEqual(parse_agent_json("no json"), {})

    def test_not_configured_without_endpoint(self):
        with patch.dict(os.environ, {"ASSURANCE_AGENT_NAME": "", "FOUNDRY_PROJECT_ENDPOINT": ""}):
            finding = run_expert("policy", build_case(FINAL))
        self.assertEqual(finding["status"], "not_configured")

    def test_per_issue_findings_are_normalized_and_unknown_issues_dropped(self):
        def invoke(_agent, _prompt):
            return (
                '{"findings": ['
                '{"issueId": "payer:negative_amount_paid", "decision": "approve_deduction", "confidence": 3, "citations": [{"policyId": "P1"}]},'
                '{"issueId": "payer:conflicting_payment_line", "decision": "post_it"},'
                '{"issueId": "made_up", "decision": "dispute_short_pay"}]}'
            ), []

        with patch.dict(os.environ, {"ASSURANCE_AGENT_NAME": "ach-assurance-expert"}):
            result = run_expert("policy", build_case(MIXED), invoke=invoke)
        by_issue = {f["issueId"]: f for f in result["findings"]}
        self.assertEqual(set(by_issue), {"payer:negative_amount_paid", "payer:conflicting_payment_line"})
        self.assertEqual(by_issue["payer:negative_amount_paid"]["confidence"], 1.0)
        self.assertEqual(by_issue["payer:conflicting_payment_line"]["decision"], "manual_review")

    def test_legacy_single_decision_applies_to_every_issue(self):
        def invoke(_agent, _prompt):
            return '{"decision": "confirm_with_payer", "confidence": 0.7}', []

        with patch.dict(os.environ, {"ASSURANCE_AGENT_NAME": "ach-assurance-expert"}):
            result = run_expert("policy", build_case(MIXED), invoke=invoke)
        self.assertEqual(len(result["findings"]), 2)

    def test_invoke_failure_is_contained(self):
        def invoke(_agent, _prompt):
            raise TimeoutError()

        with patch.dict(os.environ, {"ASSURANCE_AGENT_NAME": "ach-assurance-expert"}):
            finding = run_expert("policy", build_case(FINAL), invoke=invoke)
        self.assertEqual(finding["status"], "failed")
        self.assertEqual(finding["error"], "TimeoutError")


def _agent(*findings, expert="policy"):
    return {"expert": expert, "status": "completed", "findings": list(findings)}


class FusionTests(unittest.TestCase):
    def test_every_issue_gets_a_finding_and_payer_issues_use_the_agent(self):
        agent = _agent(
            {"issueId": "payer:negative_amount_paid", "decision": "approve_deduction", "confidence": 0.9, "amountAtRisk": 4.25,
             "category": "debit_memo", "rationale": "r", "recommendedAction": "Approve the credit on PRO 05100000301.",
             "citations": [{"policyId": "DD-01"}]},
            {"issueId": "payer:conflicting_payment_line", "decision": "confirm_with_payer", "confidence": 0.7, "amountAtRisk": 1154.92},
        )
        fused = fuse([agent], MIXED)
        by_issue = {f["issueId"]: f for f in fused["findings"]}
        self.assertEqual(fused["issueCount"], 3)
        self.assertEqual(by_issue["data:invalid_pro_number"]["decision"], "correct_data")
        self.assertEqual(by_issue["data:invalid_pro_number"]["source"], "rules")
        self.assertEqual(by_issue["payer:negative_amount_paid"]["decision"], "approve_deduction")
        self.assertEqual(by_issue["payer:negative_amount_paid"]["citations"][0]["expert"], "policy")
        # Primary: payer issues before data fixes, the larger amount at risk first.
        self.assertEqual(fused["findings"][0]["issueId"], "payer:conflicting_payment_line")
        self.assertEqual(fused["headline"], "1 PRO paid with different amounts")
        self.assertEqual(fused["decision"], "confirm_with_payer")

    def test_extraction_issue_leads_and_is_rules_based(self):
        fused = fuse([{"expert": "policy", "status": "skipped"}], MISPAIRED)
        self.assertEqual(fused["decision"], "verify_extraction")
        self.assertEqual(fused["headline"], "2 lines mis-paired by extraction")
        self.assertIn("Do not contact the payer", fused["recommendedAction"])

    def test_missing_agent_finding_falls_back_to_manual_review(self):
        fused = fuse([{"expert": "policy", "status": "not_configured"}], FINAL)
        self.assertEqual(fused["decision"], "manual_review")
        self.assertEqual(fused["amountAtRisk"], 10.0)
        self.assertIn("not configured", fused["rationale"])

    def test_disagreement_on_an_issue_downgrades_to_manual_review(self):
        a = _agent({"issueId": "payer:payment_total_mismatch", "decision": "approve_deduction", "confidence": 0.9})
        b = _agent({"issueId": "payer:payment_total_mismatch", "decision": "dispute_short_pay", "confidence": 0.6}, expert="webiq")
        fused = fuse([a, b], FINAL)
        self.assertEqual(fused["decision"], "manual_review")
        self.assertFalse(fused["expertsAgree"])


if __name__ == "__main__":
    unittest.main()
