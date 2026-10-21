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


if __name__ == "__main__":
    unittest.main()