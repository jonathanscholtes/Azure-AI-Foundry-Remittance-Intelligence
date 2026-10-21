import json
import os
import unittest
from unittest.mock import patch

from resolution import resolver

PDF_MARKDOWN = (
    "<table><tr><th>Our Voucher</th><th>Your Voucher</th><th>Amount Paid</th></tr>"
    "<tr><td>00000000000500001</td><td>15800001001</td><td>612.40</td></tr>"
    "<tr><td></td><td>Total</td><td>612.40</td></tr></table>"
)
BLOBS = {
    "candidates/body.txt": b"Subject: Pago\n\nWe paid invoice 15800001001 for $612.40 on 10/8.",
    "analysis/doc-001.json": json.dumps({"result": {"contents": [{"markdown": PDF_MARKDOWN}]}}).encode(),
}
EXTRACTIONS = [
    {"candidateId": "message-body", "sourceTextUri": "candidates/body.txt", "normalized": {"paymentLines": []}},
    {"candidateId": "doc-001", "rawAnalysisBlobUri": "analysis/doc-001.json", "normalized": {"paymentLines": [{"proInvoiceNumber": "00000000000500001", "amountPaid": 612.40}]}},
]


def _texts():
    return resolver.SourceTexts(EXTRACTIONS, lambda uri: BLOBS[uri])


class ResolverTests(unittest.TestCase):
    def test_triggers_are_verification_failures_only(self):
        self.assertEqual(resolver.needs_resolution(["payment_total_mismatch", "invalid_pro_number"]), ["payment_total_mismatch"])
        self.assertEqual(resolver.needs_resolution(["negative_amount_paid", "multiple_payments_in_email"]), [])
        # Several payments explain the gap to the one stated total - nothing to resolve.
        self.assertEqual(resolver.needs_resolution(["payment_total_mismatch", "multiple_payments_in_email"]), [])
        self.assertEqual(resolver.skip_reason(["payment_total_mismatch", "multiple_payments_in_email"]), "explained_by_multiple_payments")
        self.assertIsNone(resolver.skip_reason(["payment_total_mismatch"]))  # the expert runs
        self.assertIsNone(resolver.skip_reason(["multiple_payments_in_email"]))  # nothing was off

    def test_detail_problems_call_the_expert_but_a_missing_date_alone_does_not(self):
        self.assertEqual(resolver.needs_resolution(["conflicting_payerName"]), ["conflicting_payerName"])
        self.assertEqual(resolver.needs_resolution(["missing_remittance_date"]), [])
        self.assertEqual(resolver.needs_resolution(["conflicting_payerName", "missing_remittance_date"]),
                         ["conflicting_payerName", "missing_remittance_date"])
        # Several payments explain differing dates and references.
        self.assertEqual(resolver.needs_resolution(["conflicting_remittanceDate", "multiple_payments_in_email"]), [])

    def test_a_missing_date_calls_the_expert_when_the_run_is_in_review_anyway(self):
        in_review = ["missing_remittance_date", "remittance_inquiry_thread"]
        self.assertEqual(resolver.needs_resolution(in_review, requires_review=True), ["missing_remittance_date"])
        self.assertEqual(resolver.needs_resolution(in_review), [])  # a clean run never pays for it
        # One date can't be filled for several payments, or for an email without a remittance.
        for code in ("multiple_payments_in_email", "no_remittance_in_email"):
            self.assertEqual(resolver.needs_resolution(in_review + [code], requires_review=True), [])

    def _detail_texts(self):
        header = resolver.header_text({"subject": "RE: ACH Payment of $250.00 10/02", "senderEmail": "ap@contoso.example",
                                       "sentDate": "2026-10-03T14:00:00"})
        return resolver.SourceTexts(EXTRACTIONS, lambda uri: BLOBS[uri], extra={resolver.HEADER_SOURCE: header})

    def test_verified_date_is_applied_and_payer_only_recommended(self):
        proposal = {"details": [
            {"field": "remittanceDate", "value": "2026-10-02", "sourceId": "message-header", "evidence": "ACH Payment of $250.00 10/02", "reason": "Subject."},
            {"field": "payerName", "value": "Contoso", "sourceId": "message-header", "evidence": "ap@contoso.example", "reason": "Sender."},
        ]}
        triggers = ["conflicting_payerName", "missing_remittance_date"]
        with patch.dict(os.environ, {"RECEIVING_ORG_ALIASES": "Foundry Freight"}):
            details = resolver.gate_details(proposal, self._detail_texts(), triggers, sent_date="2026-10-03")
        self.assertEqual(details["applied"]["remittanceDate"]["value"], "2026-10-02")
        self.assertEqual(details["recommended"]["payerName"]["value"], "Contoso")
        self.assertEqual(resolver.details_status(details), ("accepted", "details_resolved"))

    def test_evidence_matches_through_markdown_table_formatting(self):
        texts = resolver.SourceTexts([], lambda uri: b"", extra={"doc-003": "| **Remit From** | Contoso Farm |\n| Date | 10/02/2026 |"})
        proposal = {"details": [{"field": "payerName", "value": "Contoso Farm", "sourceId": "doc-003",
                                 "evidence": "Remit From: Contoso Farm", "reason": ""}]}
        details = resolver.gate_details(proposal, texts, ["conflicting_payerName"])
        self.assertEqual(details["recommended"]["payerName"]["value"], "Contoso Farm")
        # Words must still match whole: "Contoso Far" is not evidence for "Contoso Farm".
        partial = {"details": [{**proposal["details"][0], "evidence": "Remit From Contoso Far"}]}
        self.assertEqual(resolver.gate_details(partial, texts, ["conflicting_payerName"])["rejected"][0]["problem"], "evidence_not_in_source")

    def test_unverifiable_details_are_rejected(self):
        texts = self._detail_texts()
        cases = [
            ({"field": "remittanceDate", "value": "2026-10-02", "sourceId": "message-header", "evidence": "paid on 10/02 by wire"}, "evidence_not_in_source"),
            ({"field": "remittanceDate", "value": "2026-11-02", "sourceId": "message-header", "evidence": "ACH Payment of $250.00 10/02"}, "date_not_in_evidence"),
            ({"field": "remittanceDate", "value": "2025-10-02", "sourceId": "message-header", "evidence": "ACH Payment of $250.00 10/02"}, "date_far_from_email"),
            ({"field": "payerName", "value": "Foundry Freight", "sourceId": "message-body", "evidence": "We paid invoice"}, "payer_is_payee"),
            ({"field": "paymentReference", "value": "999", "sourceId": "message-body", "evidence": "We paid invoice"}, "not_in_question"),
        ]
        triggers = ["conflicting_payerName", "conflicting_remittanceDate"]
        for item, problem in cases:
            with patch.dict(os.environ, {"RECEIVING_ORG_ALIASES": "Foundry Freight"}):
                details = resolver.gate_details({"details": [{**item, "reason": ""}]}, texts, triggers, sent_date="2026-10-03")
            self.assertFalse(details["applied"] or details["recommended"], problem)
            if problem != "not_in_question":  # a detail nobody asked about is ignored, not recorded
                self.assertEqual(details["rejected"][0]["problem"], problem)

    def test_applied_date_clears_its_review_reason_and_completes_the_run(self):
        final = {"status": "ReviewRequired", "remittanceMetadata": {"remittanceDate": None},
                 "quality": {"warnings": ["conflicting_remittanceDate", "missing_remittance_date"],
                             "reviewReasons": ["conflicting_remittanceDate"], "notes": ["missing_remittance_date"], "requiresReview": True}}
        record = {"value": "2026-10-02", "sourceId": "message-header", "evidence": "10/02", "reason": ""}
        result = resolver.apply_resolution(final, {"status": "accepted", "details": {"applied": {"remittanceDate": record}}})
        self.assertEqual(result["remittanceMetadata"]["remittanceDate"], "2026-10-02")
        self.assertEqual(result["quality"]["reviewReasons"], [])
        self.assertEqual(result["quality"]["notes"], ["remittance_date_resolved_by_expert"])
        self.assertFalse(result["quality"]["requiresReview"])
        self.assertEqual(result["status"], "Extracted")

    def test_confirmed_lines_turn_the_thread_kind_into_the_review_reason(self):
        final = {
            "paymentLines": [{"proInvoiceNumber": p, "amountPaid": 2150.0, "source": {"candidateId": "doc-003"}} for p in ("1", "2")],
            "quality": {"warnings": ["payment_total_mismatch"], "reviewReasons": ["payment_total_mismatch"], "notes": []},
        }
        same = {"outcome": "cannot_resolve", "lines": [{"sourceId": "doc-003", "pro": p, "amount": 2150} for p in ("1", "2")]}
        self.assertTrue(resolver.confirms_lines(same, final))
        self.assertFalse(resolver.confirms_lines({**same, "lines": same["lines"][:1]}, final))  # selects one
        self.assertFalse(resolver.confirms_lines({**same, "outcome": "resolved"}, final))
        result = resolver.apply_resolution(final, {"status": "confirmed", "threadWarning": "multiple_payments_in_email"})
        self.assertEqual(result["quality"]["reviewReasons"], ["multiple_payments_in_email"])
        self.assertIn("payment_total_mismatch", result["quality"]["notes"])  # explained by the payments
        self.assertEqual(result["resolution"]["status"], "confirmed")

    def test_tools_read_text_tables_and_check_lines(self):
        from extraction import column_mapping

        tools = resolver.make_tools(_texts(), 612.40)
        page = tools["get_source_text"]("message-body", 0)
        self.assertIn("15800001001", page["text"])
        self.assertIsNone(page["nextOffset"])
        # "Your Voucher" is the PRO column: the ACH Extraction Expert maps it (the frozen rules can't).
        expert = lambda agent, prompt: (json.dumps({"headerRow": 0, "identifierColumn": 1, "amountPaidColumn": 2,
                                                   "tableKind": "payment_detail", "confidence": 0.9}), [])
        column_mapping._cache.clear()
        with patch.dict(os.environ, {"EXTRACTION_AGENT_NAME": "ach-extraction-expert", "FOUNDRY_PROJECT_ENDPOINT": "https://p.example"}), \
                patch.object(column_mapping, "invoke", expert):
            tables = tools["read_tables"]("doc-001")
        self.assertEqual(tables["lines"], [{"pro": "15800001001", "amount": "612.40"}])
        checked = tools["check_lines"]([{"sourceId": "doc-001", "pro": "15800001001", "amount": 612.40},
                                        {"sourceId": "doc-001", "pro": "99999999999", "amount": 1.0}])
        self.assertEqual([c["grounded"] for c in checked["lines"]], [True, False])
        self.assertFalse(checked["allGrounded"])
        self.assertAlmostEqual(checked["differenceFromStated"], -1.0)

    def _validate(self, normalized):
        total = sum(line["amountPaid"] for item in normalized for line in item["paymentLines"])
        return {"quality": {"isReconciled": abs(total - 612.40) < 0.005}}

    def test_gate_accepts_grounded_and_reconciled_lines(self):
        proposal = {"outcome": "resolved", "lines": [{"sourceId": "doc-001", "pro": "15800001001", "amount": 612.40, "evidence": "Your Voucher"}]}
        decision = resolver.gate(proposal, EXTRACTIONS, _texts(), self._validate)
        self.assertTrue(decision["accepted"])
        lines = {e["candidateId"]: e["normalized"]["paymentLines"] for e in decision["extractions"]}
        self.assertEqual(lines["doc-001"][0]["proInvoiceNumber"], "15800001001")
        self.assertEqual(lines["doc-001"][0]["source"]["method"], "resolution_expert")
        self.assertEqual(EXTRACTIONS[1]["normalized"]["paymentLines"][0]["proInvoiceNumber"], "00000000000500001")  # inputs untouched

    def test_gate_refuses_picking_a_subset_of_valid_lines(self):
        # Multi-transfer thread: three grounded $2,150 transfers; the expert "reconciles" by choosing the Sept 11 one.
        body = b"Transfers: PRO 13600000802 $2,150.00 on 9/9; PRO 13600000803 $2,150.00 on 9/11; PRO 13600000804 $2,150.00 on 9/24."
        extractions = [{"candidateId": "doc-003", "sourceTextUri": "t", "normalized": {"paymentLines": [
            {"proInvoiceNumber": pro, "amountPaid": 2150.0, "source": {"grounded": True}}
            for pro in ("13600000802", "13600000803", "13600000804")]}}]
        texts = resolver.SourceTexts(extractions, lambda uri: body)
        proposal = {"outcome": "resolved", "lines": [{"sourceId": "doc-003", "pro": "13600000803", "amount": 2150.0, "evidence": "9/11"}]}
        validate = lambda normalized: {"quality": {"isReconciled": True}}
        decision = resolver.gate(proposal, extractions, texts, validate)
        self.assertFalse(decision["accepted"])
        self.assertEqual(decision["reason"], "drops_trusted_lines")
        self.assertEqual(len(decision["dropped"]), 2)

    def test_gate_allows_correcting_a_grounded_lines_pro(self):
        # The row's other number was taken as the PRO (grounded, same row): a corrected PRO keeps the slot.
        extractions = [{"candidateId": "doc-001", "rawAnalysisBlobUri": "analysis/doc-001.json", "normalized": {"paymentLines": [
            {"proInvoiceNumber": "00000000000500001", "amountPaid": 612.40, "source": {"grounded": True}}]}}]
        texts = resolver.SourceTexts(extractions, lambda uri: BLOBS[uri])
        proposal = {"outcome": "resolved", "lines": [{"sourceId": "doc-001", "pro": "15800001001", "amount": 612.40, "evidence": "Your Voucher"}]}
        self.assertTrue(resolver.gate(proposal, extractions, texts, self._validate)["accepted"])

    def test_gate_refuses_ungrounded_unreconciled_or_unresolved(self):
        texts = _texts()
        invented = {"outcome": "resolved", "lines": [{"sourceId": "doc-001", "pro": "12345678901", "amount": 612.40, "evidence": ""}]}
        self.assertEqual(resolver.gate(invented, EXTRACTIONS, texts, self._validate)["reason"], "line_not_grounded:12345678901")
        short = {"outcome": "resolved", "lines": [{"sourceId": "message-body", "pro": "15800001001", "amount": 612.40, "evidence": ""},
                                                  {"sourceId": "doc-001", "pro": "15800001001", "amount": 612.40, "evidence": ""}]}
        self.assertEqual(resolver.gate(short, EXTRACTIONS, texts, self._validate)["reason"], "not_reconciled")
        self.assertFalse(resolver.gate({"outcome": "cannot_resolve", "lines": []}, EXTRACTIONS, texts, self._validate)["accepted"])
        unknown = {"outcome": "resolved", "lines": [{"sourceId": "doc-999", "pro": "1", "amount": 1, "evidence": ""}]}
        self.assertEqual(resolver.gate(unknown, EXTRACTIONS, texts, self._validate)["reason"], "unknown_source:doc-999")


if __name__ == "__main__":
    unittest.main()
