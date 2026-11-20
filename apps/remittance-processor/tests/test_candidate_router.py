import unittest

from extraction.candidate_router import route_candidates


class CandidateRouterTests(unittest.TestCase):
    def test_routes_csv_deterministically_and_pdf_to_analyzer(self):
        candidates = [
            {
                "candidateId": "csv-1",
                "sourceType": "attachment",
                "name": "remittance.csv",
                "extension": ".csv",
                "classification": "remittance_candidate",
                "score": 80,
            },
            {
                "candidateId": "pdf-1",
                "sourceType": "attachment",
                "name": "remittance.pdf",
                "extension": ".pdf",
                "classification": "remittance_candidate",
                "score": 100,
            },
        ]
        calls = []

        def fake_analyze(**kwargs):
            calls.append(kwargs)
            return {
                "operationId": "op-1",
                "raw": {"fields": {}},
                "normalized": {
                    "candidateId": kwargs["candidate_id"],
                    "sourceType": kwargs["source_type"],
                    "remittanceMetadata": {},
                    "paymentLines": [],
                    "warnings": [],
                },
            }

        normalized, raw = route_candidates(
            candidates,
            {
                "csv-1": (b"PRO,Amount Paid\n001234567,12.50\n", "text/csv"),
                "pdf-1": (b"%PDF", "application/pdf"),
            },
            endpoint="https://foundry.example",
            analyzer_id="ach.remittance.v1",
            analyze=fake_analyze,
        )

        self.assertEqual([item["strategy"] for item in normalized], ["deterministic_parser", "content_understanding"])
        self.assertEqual(normalized[0]["paymentLines"][0]["proInvoiceNumber"], "001234567")
        self.assertEqual(calls[0]["candidate_id"], "pdf-1")
        self.assertEqual(raw[1]["operationId"], "op-1")
        # Only the CU candidate records the analyzer it used.
        self.assertNotIn("analyzerId", normalized[0])
        self.assertEqual(normalized[1]["analyzerId"], "ach.remittance.v1")

    def test_missing_analyzer_configuration_becomes_review_warning(self):
        candidate = {
            "candidateId": "pdf-1",
            "sourceType": "attachment",
            "name": "remittance.pdf",
            "extension": ".pdf",
            "classification": "remittance_candidate",
        }
        normalized, _ = route_candidates(
            [candidate],
            {"pdf-1": (b"%PDF", "application/pdf")},
            endpoint=None,
            analyzer_id="ach.remittance.v1",
        )

        self.assertIn("content_understanding_not_configured", normalized[0]["warnings"])

    def test_spreadsheet_title_block_goes_to_cu_for_metadata_only(self):
        candidate = {"candidateId": "csv-1", "sourceType": "attachment", "name": "remittance.csv", "extension": ".csv",
                     "classification": "remittance_candidate"}
        content = (b"Payment Summary,\nPayer,Fabrikam Inc.\nPaid On,09/15/2026\n,\n"
                   b"PRO Number,Amount Paid\n001234567,10.00\n001234568,5.00\n,15.00\n")
        sent = []

        def fake_analyze(**kwargs):
            sent.append(kwargs)
            return {"operationId": "op-meta", "raw": {}, "usage": {"calls": 1},
                    "normalized": {"remittanceMetadata": {"remittanceDate": "2026-09-15"}, "metadataEvidence": {},
                                   "paymentLines": [{"proInvoiceNumber": "999999999", "amountPaid": 1.0}], "warnings": []}}

        normalized, raw = route_candidates([candidate], {"csv-1": (content, "text/csv")}, endpoint="https://foundry.example",
                                           analyzer_id="ach.remittance.v1", analyze=fake_analyze)

        text = sent[0]["content"].decode("utf-8")
        self.assertEqual(sent[0]["content_type"], "text/plain")
        self.assertIn("Paid On | 09/15/2026", text)
        self.assertNotIn("001234567", text)  # the lines stay the code's
        result = normalized[0]
        self.assertEqual(result["remittanceMetadata"]["remittanceDate"], "2026-09-15")
        self.assertEqual(result["metadataRole"], "advisory")
        self.assertEqual([line["proInvoiceNumber"] for line in result["paymentLines"]], ["001234567", "001234568"])
        self.assertEqual(raw[0]["metadataOperationId"], "op-meta")

    def test_spreadsheet_with_only_a_table_makes_no_cu_call(self):
        candidate = {"candidateId": "csv-1", "sourceType": "attachment", "name": "remittance.csv", "extension": ".csv",
                     "classification": "remittance_candidate"}
        calls = []
        route_candidates([candidate], {"csv-1": (b"PRO Number,Amount Paid\n001234567,10.00\n001234568,5.00\nTotal,15.00\n", "text/csv")},
                         endpoint="https://foundry.example", analyzer_id="ach.remittance.v1", analyze=lambda **kw: calls.append(kw))
        self.assertEqual(calls, [])

    def test_unreadable_spreadsheet_becomes_review_warning_not_a_failed_run(self):
        candidate = {
            "candidateId": "xlsx-1",
            "sourceType": "attachment",
            "name": "remittance.xlsx",
            "extension": ".xlsx",
            "classification": "remittance_candidate",
        }
        normalized, raw = route_candidates(
            [candidate],
            {"xlsx-1": (b"not a workbook", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
            endpoint=None,
            analyzer_id="ach.remittance.v1",
        )

        self.assertIn("spreadsheet_parse_failed", normalized[0]["warnings"])
        self.assertEqual(normalized[0]["paymentLines"], [])
        self.assertEqual(raw[0]["errorType"], normalized[0]["analysisErrorType"])


if __name__ == "__main__":
    unittest.main()