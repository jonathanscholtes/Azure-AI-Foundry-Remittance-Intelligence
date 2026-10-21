import unittest

from msg_parser.candidate_selector import select_candidates


class CandidateSelectorTests(unittest.TestCase):
    def test_prefers_named_pdf_and_selects_content_understanding(self):
        result = select_candidates(
            [
                {
                    "candidateId": "doc-001",
                    "sourceType": "attachment",
                    "name": "ACH Remittance.pdf",
                    "contentType": "application/pdf",
                    "size": 24000,
                    "isInline": False,
                }
            ],
            subject="ACH payment details",
        )

        self.assertEqual(result["recommendedProcessing"]["primaryCandidateId"], "doc-001")
        self.assertEqual(result["recommendedProcessing"]["strategy"], "content_understanding")

    def test_retains_body_and_duplicate_candidates(self):
        candidates = [
            {"candidateId": "one", "name": "invoice.csv", "extension": ".csv", "size": 2048, "sha256": "same"},
            {"candidateId": "two", "name": "copy.csv", "extension": ".csv", "size": 2048, "sha256": "same"},
        ]
        result = select_candidates(candidates, body="Payment remittance details for invoice 001.")

        self.assertEqual(len(result["candidates"]), 3)
        self.assertEqual(result["candidates"][1]["classification"], "duplicate_candidate")
        self.assertEqual(result["recommendedProcessing"]["strategy"], "deterministic_parser")

    def test_retains_but_does_not_select_small_inline_logo(self):
        result = select_candidates(
            [
                {
                    "candidateId": "logo",
                    "name": "signature-logo.png",
                    "extension": ".png",
                    "size": 1200,
                    "isInline": True,
                }
            ]
        )

        self.assertEqual(result["candidates"][0]["classification"], "ignored_candidate")
        self.assertIsNone(result["recommendedProcessing"]["primaryCandidateId"])

    def test_subject_only_invoice_reference_is_a_candidate(self):
        result = select_candidates([], subject="Invoice 001234567")

        self.assertEqual(result["recommendedProcessing"]["primaryCandidateId"], "message-body")
        self.assertEqual(result["recommendedProcessing"]["strategy"], "content_understanding")

    def test_inline_gif_is_treated_as_decorative(self):
        result = select_candidates(
            [
                {
                    "candidateId": "doc-002",
                    "name": "image002.gif",
                    "extension": ".gif",
                    "size": 10000,
                    "isInline": True,
                }
            ]
        )

        self.assertEqual(result["candidates"][0]["classification"], "ignored_candidate")

    def test_inline_screenshot_larger_than_tiny_asset_threshold_is_retained(self):
        result = select_candidates(
            [
                {
                    "candidateId": "inline-screenshot",
                    "name": "image001.png",
                    "extension": ".png",
                    "size": 12000,
                    "isInline": True,
                }
            ]
        )

        self.assertEqual(result["candidates"][0]["classification"], "remittance_candidate")


if __name__ == "__main__":
    unittest.main()