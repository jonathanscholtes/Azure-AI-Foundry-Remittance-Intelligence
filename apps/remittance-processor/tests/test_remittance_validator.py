import os
import unittest
from decimal import Decimal
from unittest.mock import patch

from shared import pro_rules
from validation.remittance_validator import (
    create_output_filename,
    create_payment_csv,
    date_from_subject,
    normalize_pro,
    validate_and_merge,
)


class RemittanceValidatorTests(unittest.TestCase):
    def test_normalizes_allowed_trailing_pro_suffixes_and_preserves_zeroes(self):
        rules = pro_rules.rules({"PRO_MIN_DIGITS": "7", "PRO_MAX_DIGITS": "11", "PRO_STRIP_SUFFIXES": " X, XY "})
        self.assertEqual(normalize_pro("001234567X", rules), ("001234567", "001234567X", None))
        self.assertEqual(normalize_pro("001234567xy", rules), ("001234567", "001234567xy", None))
        self.assertEqual(normalize_pro("001234567Q", rules), (None, "001234567Q", "invalid_pro_number"))
        self.assertEqual(normalize_pro("123456", rules), (None, "123456", "invalid_pro_number"))

    def test_unset_pro_settings_are_neutral(self):
        for env in ({}, {"PRO_MIN_DIGITS": "", "PRO_MAX_DIGITS": "abc", "PRO_STRIP_SUFFIXES": " , "}):
            rules = pro_rules.rules(env)
            self.assertEqual((rules["minDigits"], rules["maxDigits"]), (6, 12))
            self.assertEqual((rules["stripSuffixes"], rules["suffixExamples"]), ((), ()))
            self.assertEqual(normalize_pro("001234567", rules), ("001234567", "001234567", None))
            self.assertEqual(normalize_pro("001234567X", rules), (None, "001234567X", "invalid_pro_number"))

    def test_pro_rules_come_from_app_settings(self):
        with patch.dict(os.environ, {"PRO_MIN_DIGITS": "7", "PRO_MAX_DIGITS": "11", "PRO_STRIP_SUFFIXES": "X"}):
            self.assertEqual(normalize_pro("001234567X"), ("001234567", "001234567X", None))
        with patch.dict(os.environ, {"PRO_STRIP_SUFFIXES": ""}):
            self.assertEqual(normalize_pro("001234567X"), (None, "001234567X", "invalid_pro_number"))

    def test_validates_reconciles_and_creates_headerless_csv(self):
        result = validate_and_merge(
            [
                {
                    "candidateId": "doc-001",
                    "sourceType": "attachment",
                    "score": 100,
                    "remittanceMetadata": {
                        "payerName": "Acme Transport",
                        "remittanceDate": "2026-10-05",
                        "paymentTotal": 100.00,
                        "currency": "USD",
                    },
                    "paymentLines": [
                        {"proInvoiceNumber": "001234567", "amountPaid": 60.00},
                        {"proInvoiceNumber": "001234568", "amountPaid": 40.00},
                    ],
                }
            ],
            tolerance=Decimal("0.01"),
        )

        self.assertEqual(result["status"], "Extracted")
        self.assertTrue(result["quality"]["isReconciled"])
        self.assertEqual(create_payment_csv(result), b"001234567,60.00\n001234568,40.00\n")

    def test_analyzer_version_records_the_analyzers_actually_used(self):
        cu = {"candidateId": "doc-001", "score": 100, "analyzerId": "ach.remittance.v2", "remittanceMetadata": {},
              "paymentLines": [{"proInvoiceNumber": "001234567", "amountPaid": 10.00}]}
        sheet = {"candidateId": "doc-002", "score": 90, "remittanceMetadata": {},
                 "paymentLines": [{"proInvoiceNumber": "001234568", "amountPaid": 5.00}]}
        self.assertEqual(validate_and_merge([cu, sheet])["analyzerVersion"], "ach.remittance.v2")
        self.assertEqual(validate_and_merge([sheet])["analyzerVersion"], "none")

    def test_exact_duplicate_lines_are_deduplicated_with_lineage(self):
        result = validate_and_merge(
            [
                {
                    "candidateId": "one",
                    "sourceType": "attachment",
                    "remittanceMetadata": {"payerName": "Acme", "remittanceDate": "2026-10-05"},
                    "paymentLines": [{"proInvoiceNumber": "001234567", "amountPaid": 10}],
                },
                {
                    "candidateId": "two",
                    "sourceType": "message_body",
                    "remittanceMetadata": {},
                    "paymentLines": [{"proInvoiceNumber": "001234567", "amountPaid": 10}],
                },
            ]
        )

        self.assertEqual(result["status"], "Extracted")
        self.assertEqual(len(result["paymentLines"]), 1)
        self.assertEqual(result["quality"]["duplicateGroups"][0]["duplicateCandidateId"], "two")
        self.assertEqual(create_payment_csv(result), b"001234567,10.00\n")

    def test_conflicting_amounts_for_same_pro_require_review(self):
        result = validate_and_merge(
            [
                {
                    "candidateId": "one",
                    "sourceType": "attachment",
                    "remittanceMetadata": {"payerName": "Acme", "remittanceDate": "2026-10-05"},
                    "paymentLines": [{"proInvoiceNumber": "001234567", "amountPaid": 10}],
                },
                {
                    "candidateId": "two",
                    "sourceType": "message_body",
                    "remittanceMetadata": {},
                    "paymentLines": [{"proInvoiceNumber": "001234567", "amountPaid": 12}],
                },
            ]
        )

        self.assertEqual(result["status"], "ReviewRequired")
        self.assertIn("conflicting_payment_line", result["quality"]["warnings"])
        self.assertIn("conflicting_payment_line", result["quality"]["reviewReasons"])

    def test_date_from_subject(self):
        self.assertEqual(date_from_subject("RE: ACH Payment of $30,000.00 10/02", "2026-10-03T09:00:00"), "2026-10-02")
        self.assertEqual(date_from_subject("Payment 12/30", "2027-01-04"), "2026-12-30")  # year rolls back
        self.assertEqual(date_from_subject("Remittance 10/02/2026", None), "2026-10-02")
        self.assertIsNone(date_from_subject("Payment 10/02", None))  # no year anywhere
        self.assertIsNone(date_from_subject("Payments 10/02 and 10/09", "2026-10-10"))  # ambiguous
        self.assertIsNone(date_from_subject("Invoice 3/14/2019", "2026-10-03"))  # far from the email
        self.assertIsNone(date_from_subject("Paid $1,250.00", "2026-10-03"))

    def test_subject_date_fills_only_a_missing_date(self):
        candidate = {"candidateId": "one", "sourceType": "attachment", "remittanceMetadata": {"payerName": "Acme", "paymentTotal": 10},
                     "paymentLines": [{"proInvoiceNumber": "001234567", "amountPaid": 10}]}
        result = validate_and_merge([candidate], subject_date="2026-10-02")
        self.assertEqual(result["remittanceMetadata"]["remittanceDate"], "2026-10-02")
        self.assertIn("remittance_date_derived_from_subject", result["quality"]["notes"])
        dated = {**candidate, "remittanceMetadata": {**candidate["remittanceMetadata"], "remittanceDate": "2026-10-05"}}
        self.assertEqual(validate_and_merge([dated], subject_date="2026-10-02")["remittanceMetadata"]["remittanceDate"], "2026-10-05")

    def test_same_pro_in_separate_payments_is_a_note_not_a_conflict(self):
        def line(amount, group):
            return {"proInvoiceNumber": "001234567", "amountPaid": amount, "source": {"paymentGroup": group}}

        result = validate_and_merge(
            [
                {
                    "candidateId": "body",
                    "sourceType": "message_body",
                    "remittanceMetadata": {"payerName": "Acme", "remittanceDate": "2026-10-05", "paymentTotal": 30},
                    "paymentLines": [line(10, "Total CA"), line(20, "Total AZ")],
                }
            ]
        )

        quality = result["quality"]
        self.assertNotIn("conflicting_payment_line", quality["warnings"])
        self.assertIn("pro_paid_across_payments", quality["notes"])
        self.assertNotIn("pro_paid_across_payments", quality["reviewReasons"])
        self.assertEqual(result["status"], "Extracted")

    def test_same_pro_twice_in_one_payment_still_conflicts(self):
        def line(amount):
            return {"proInvoiceNumber": "001234567", "amountPaid": amount, "source": {"paymentGroup": "Total CA"}}

        result = validate_and_merge(
            [
                {
                    "candidateId": "body",
                    "sourceType": "message_body",
                    "remittanceMetadata": {"payerName": "Acme", "remittanceDate": "2026-10-05", "paymentTotal": 30},
                    "paymentLines": [line(10), line(20)],
                }
            ]
        )

        self.assertIn("conflicting_payment_line", result["quality"]["reviewReasons"])
        self.assertEqual(result["status"], "ReviewRequired")

    def test_invalid_identifier_and_unreconciled_total_require_review(self):
        result = validate_and_merge(
            [
                {
                    "candidateId": "doc-001",
                    "sourceType": "attachment",
                    "remittanceMetadata": {
                        "payerName": "Acme",
                        "remittanceDate": "2026-10-05",
                        "paymentTotal": 15.00,
                    },
                    "paymentLines": [{"proInvoiceNumber": "AB-123", "amountPaid": 10}],
                }
            ]
        )

        self.assertEqual(result["status"], "ReviewRequired")
        self.assertIn("invalid_pro_number", result["quality"]["warnings"])
        self.assertIn("payment_total_mismatch", result["quality"]["warnings"])

    def test_candidate_extraction_failure_requires_review(self):
        result = validate_and_merge(
            [
                {
                    "candidateId": "doc-001",
                    "sourceType": "attachment",
                    "remittanceMetadata": {},
                    "paymentLines": [],
                    "warnings": ["content_understanding_analysis_failed"],
                }
            ]
        )

        self.assertEqual(result["status"], "ReviewRequired")
        self.assertIn("content_understanding_analysis_failed", result["quality"]["warnings"])

    def test_intra_candidate_metadata_conflict_requires_review(self):
        result = validate_and_merge(
            [
                {
                    "candidateId": "doc-001",
                    "sourceType": "attachment",
                    "remittanceMetadata": {"payerName": "Acme"},
                    "warnings": ["conflicting_payerName_within_candidate"],
                    "paymentLines": [{"proInvoiceNumber": "001234567", "amountPaid": 10}],
                }
            ]
        )

        self.assertEqual(result["status"], "ReviewRequired")

    def test_payer_name_formatting_variants_are_not_a_conflict(self):
        result = validate_and_merge(
            [
                {
                    "candidateId": "doc-001",
                    "sourceType": "attachment",
                    "remittanceMetadata": {"payerName": "Acme Transport, Inc.", "remittanceDate": "2026-10-05", "paymentTotal": 10},
                    "paymentLines": [{"proInvoiceNumber": "001234567", "amountPaid": 10}],
                },
                {
                    "candidateId": "message-body",
                    "sourceType": "message_body",
                    "remittanceMetadata": {"payerName": "ACME TRANSPORT"},
                    "paymentLines": [],
                },
            ]
        )
        self.assertNotIn("conflicting_payerName", result["quality"]["warnings"])

    def test_genuinely_different_payer_names_still_conflict(self):
        result = validate_and_merge(
            [
                {
                    "candidateId": "a",
                    "sourceType": "attachment",
                    "remittanceMetadata": {"payerName": "Acme Transport", "remittanceDate": "2026-10-05", "paymentTotal": 10},
                    "paymentLines": [{"proInvoiceNumber": "001234567", "amountPaid": 10}],
                },
                {
                    "candidateId": "b",
                    "sourceType": "message_body",
                    "remittanceMetadata": {"payerName": "Globex Logistics"},
                    "paymentLines": [],
                },
            ]
        )
        self.assertIn("conflicting_payerName", result["quality"]["warnings"])

    def test_document_date_wins_over_email_date(self):
        result = validate_and_merge(
            [
                {
                    "candidateId": "message-body",
                    "sourceType": "message_body",
                    "remittanceMetadata": {"payerName": "Acme", "remittanceDate": "2026-10-07"},
                    "paymentLines": [],
                },
                {
                    "candidateId": "doc-001",
                    "sourceType": "attachment",
                    "remittanceMetadata": {"payerName": "Acme", "remittanceDate": "2026-10-06", "paymentTotal": 10},
                    "paymentLines": [{"proInvoiceNumber": "001234567", "amountPaid": 10}],
                },
            ]
        )
        # Document date wins, and a document/email mismatch is not treated as a conflict.
        self.assertEqual(result["remittanceMetadata"]["remittanceDate"], "2026-10-06")
        self.assertNotIn("conflicting_remittanceDate", result["quality"]["warnings"])

    def test_two_documents_disagreeing_on_date_still_conflict(self):
        result = validate_and_merge(
            [
                {
                    "candidateId": "doc-001",
                    "sourceType": "attachment",
                    "remittanceMetadata": {"payerName": "Acme", "remittanceDate": "2026-10-06", "paymentTotal": 10},
                    "paymentLines": [{"proInvoiceNumber": "001234567", "amountPaid": 10}],
                },
                {
                    "candidateId": "doc-002",
                    "sourceType": "attachment",
                    "remittanceMetadata": {"remittanceDate": "2026-10-09"},
                    "paymentLines": [],
                },
            ]
        )
        self.assertIn("conflicting_remittanceDate", result["quality"]["warnings"])

    def test_payer_matching_receiving_org_is_flagged(self):
        result = validate_and_merge(
            [
                {
                    "candidateId": "doc-001",
                    "sourceType": "attachment",
                    "remittanceMetadata": {"payerName": "Foundry Freight Lines, Inc.", "remittanceDate": "2026-10-05", "paymentTotal": 10},
                    "paymentLines": [{"proInvoiceNumber": "001234567", "amountPaid": 10}],
                }
            ],
            payee_aliases=["Foundry Freight Lines"],
        )
        self.assertIn("payer_name_matches_payee", result["quality"]["warnings"])
        self.assertEqual(result["status"], "ReviewRequired")

    def test_payee_name_is_dropped_when_a_real_payer_is_present(self):
        result = validate_and_merge(
            [
                {
                    "candidateId": "doc-001",
                    "sourceType": "attachment",
                    "remittanceMetadata": {"payerName": "Foundry Freight Lines, Inc.", "remittanceDate": "2026-10-05", "paymentTotal": 10},
                    "paymentLines": [{"proInvoiceNumber": "001234567", "amountPaid": 10}],
                },
                {
                    "candidateId": "message-body",
                    "sourceType": "message_body",
                    "remittanceMetadata": {"payerName": "Acme Transport"},
                    "paymentLines": [],
                },
            ],
            payee_aliases=["Foundry Freight Lines"],
        )
        # The payee's name is dropped; the real payer from the sender-derived candidate wins cleanly.
        self.assertEqual(result["remittanceMetadata"]["payerName"], "Acme Transport")
        self.assertNotIn("payer_name_matches_payee", result["quality"]["warnings"])
        self.assertNotIn("conflicting_payerName", result["quality"]["warnings"])

    def test_ordinary_payer_is_not_flagged_as_payee(self):
        result = validate_and_merge(
            [
                {
                    "candidateId": "doc-001",
                    "sourceType": "attachment",
                    "remittanceMetadata": {"payerName": "Acme Transport", "remittanceDate": "2026-10-05", "paymentTotal": 10},
                    "paymentLines": [{"proInvoiceNumber": "001234567", "amountPaid": 10}],
                }
            ],
            payee_aliases=["Foundry Freight Lines"],
        )
        self.assertNotIn("payer_name_matches_payee", result["quality"]["warnings"])
        self.assertEqual(result["status"], "Extracted")

    def test_sender_org_does_not_override_a_real_payer_name(self):
        result = validate_and_merge(
            [
                {
                    "candidateId": "message-body",
                    "sourceType": "message_body",
                    "remittanceMetadata": {"payerName": "Fabrikam Wine & Spirits", "remittanceDate": "2026-10-05", "paymentTotal": 10},
                    "paymentLines": [{"proInvoiceNumber": "001234567", "amountPaid": 10}],
                }
            ],
            sender_org="Pacedge",
            sender_mailbox="accounting",
        )
        self.assertEqual(result["remittanceMetadata"]["payerName"], "Fabrikam Wine & Spirits")

    def test_sender_org_replaces_extracted_mailbox_name(self):
        result = validate_and_merge(
            [
                {
                    "candidateId": "message-body",
                    "sourceType": "message_body",
                    "remittanceMetadata": {"payerName": "AP_Riverside", "remittanceDate": "2026-10-05", "paymentTotal": 10},
                    "paymentLines": [{"proInvoiceNumber": "001234567", "amountPaid": 10}],
                }
            ],
            sender_org="Tailspin",
            sender_mailbox="AP_Riverside",
        )
        self.assertEqual(result["remittanceMetadata"]["payerName"], "Tailspin")

    def test_sender_org_replaces_payee_name_when_only_payee_was_extracted(self):
        result = validate_and_merge(
            [
                {
                    "candidateId": "doc-001",
                    "sourceType": "attachment",
                    "remittanceMetadata": {"payerName": "Foundry Freight Lines, Inc.", "remittanceDate": "2026-10-05", "paymentTotal": 10},
                    "paymentLines": [{"proInvoiceNumber": "001234567", "amountPaid": 10}],
                }
            ],
            payee_aliases=["Foundry Freight Lines"],
            sender_org="Tailspin",
            sender_mailbox="ap",
        )
        self.assertEqual(result["remittanceMetadata"]["payerName"], "Tailspin")
        self.assertNotIn("payer_name_matches_payee", result["quality"]["warnings"])

    def test_sender_org_fills_missing_payer(self):
        result = validate_and_merge(
            [
                {
                    "candidateId": "doc-001",
                    "sourceType": "attachment",
                    "remittanceMetadata": {"remittanceDate": "2026-10-05", "paymentTotal": 10},
                    "paymentLines": [{"proInvoiceNumber": "001234567", "amountPaid": 10}],
                }
            ],
            sender_org="Tailspin",
            sender_mailbox="ap",
        )
        self.assertEqual(result["remittanceMetadata"]["payerName"], "Tailspin")

    def test_amount_less_candidate_is_dropped_when_another_has_amounts(self):
        result = validate_and_merge(
            [
                {
                    "candidateId": "message-body",
                    "sourceType": "message_body",
                    "remittanceMetadata": {"payerName": "Acme", "remittanceDate": "2026-10-05", "paymentTotal": 30},
                    "paymentLines": [
                        {"proInvoiceNumber": "001234567", "amountPaid": 10},
                        {"proInvoiceNumber": "001234568", "amountPaid": 20},
                    ],
                },
                {
                    "candidateId": "doc-001",
                    "sourceType": "attachment",
                    "remittanceMetadata": {},
                    # A zero-balance exceptions image: invoice numbers but no amounts.
                    "paymentLines": [
                        {"proInvoiceNumber": "009999991", "amountPaid": None},
                        {"proInvoiceNumber": "009999992", "amountPaid": None},
                    ],
                },
            ]
        )
        pros = {line["proInvoiceNumber"] for line in result["paymentLines"]}
        self.assertEqual(pros, {"001234567", "001234568"})
        self.assertNotIn("missing_or_invalid_amount_paid", result["quality"]["warnings"])
        self.assertTrue(result["quality"]["isReconciled"])

    def test_output_filename_is_stable_and_sanitized(self):
        filename = create_output_filename(
            {"payerName": "Acme / Freight, Inc.", "remittanceDate": "2026-10-05", "paymentTotal": 100},
        )

        self.assertEqual(filename, "Acme-Freight-Inc_2026-10-05_100.00.csv")


if __name__ == "__main__":
    unittest.main()