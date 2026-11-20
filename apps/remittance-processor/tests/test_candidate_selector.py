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

    def test_image_below_the_analysis_minimum_is_not_sent_for_analysis(self):
        # A 40 x 40 px signature icon: Content Understanding rejects it, and it cannot hold a remittance.
        icon = {"candidateId": "doc-001", "name": "image001.png", "extension": ".png", "size": 4767, "isInline": False,
                "imagePixels": [40, 40]}
        page = {"candidateId": "doc-002", "name": "image002.png", "extension": ".png", "size": 60000, "isInline": False,
                "imagePixels": [1275, 1650]}
        unknown = {"candidateId": "doc-003", "name": "scan.tif", "extension": ".tif", "size": 60000, "isInline": False,
                   "imagePixels": None}
        result = {c["candidateId"]: c for c in select_candidates([icon, page, unknown], subject="ACH Payment")["candidates"]}
        self.assertEqual(result["doc-001"]["classification"], "ignored_candidate")
        self.assertIn("below the 50 px analysis minimum", " ".join(result["doc-001"]["selectionReasons"]))
        self.assertEqual(result["doc-002"]["classification"], "remittance_candidate")
        self.assertEqual(result["doc-003"]["classification"], "remittance_candidate")  # size unknown: unchanged

    def test_image_size_is_read_from_the_header(self):
        import struct

        from msg_parser.image_size import image_size

        png = b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\rIHDR" + struct.pack(">II", 40, 30) + b"\x08\x06\x00\x00\x00"
        gif = b"GIF89a" + struct.pack("<HH", 120, 60)
        jpeg = b"\xff\xd8" + b"\xff\xe0" + struct.pack(">H", 4) + b"JF" + b"\xff\xc0" + struct.pack(">HBHH", 11, 8, 480, 640) + b"\x03"
        self.assertEqual(image_size(png), (40, 30))
        self.assertEqual(image_size(gif), (120, 60))
        self.assertEqual(image_size(jpeg), (640, 480))
        self.assertIsNone(image_size(b"%PDF-1.7"))
        self.assertIsNone(image_size(b"\x89PNG\r\n\x1a\n"))  # truncated


if __name__ == "__main__":
    unittest.main()