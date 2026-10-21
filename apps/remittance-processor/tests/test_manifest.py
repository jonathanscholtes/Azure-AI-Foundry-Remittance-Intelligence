import unittest

from msg_parser.manifest import create_source_manifest


class ManifestTests(unittest.TestCase):
    def test_manifest_carries_run_and_correlation_ids(self):
        manifest = create_source_manifest(
            source_name="payment.msg",
            source_content=b"sample source",
            source_blob_uri="incoming/payment.msg",
            message={"subject": "Remittance"},
            attachments=[],
            candidates={
                "candidates": [],
                "recommendedProcessing": {
                    "primaryCandidateId": None,
                    "strategy": "manual_review",
                    "requiresMultipleCandidateAnalysis": False,
                },
            },
            run_id="8bc1c4f4-e31a-4fe3-a45e-a4b85a3d9d34",
            correlation_id="8bc1c4f4-e31a-4fe3-a45e-a4b85a3d9d34",
        )

        self.assertEqual(manifest["runId"], "8bc1c4f4-e31a-4fe3-a45e-a4b85a3d9d34")
        self.assertEqual(manifest["correlationId"], "8bc1c4f4-e31a-4fe3-a45e-a4b85a3d9d34")


if __name__ == "__main__":
    unittest.main()