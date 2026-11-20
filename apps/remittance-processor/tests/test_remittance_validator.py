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

    def _proof_sources(self, column_amounts=(100.00, 50.25, 24.75), stated=175.00, strategy="deterministic_parser"):
        body = {"candidateId": "message-body", "sourceType": "message_body", "score": 40, "strategy": "content_understanding",
                "remittanceMetadata": {"payerName": "Fabrikam", "paymentTotal": stated},
                "paymentLines": [{"proInvoiceNumber": "70000001", "amountPaid": 900.0}]}  # a model's misreading of the thread
        sheet = {"candidateId": "doc-002", "sourceType": "attachment", "score": 90, "strategy": strategy, "remittanceMetadata": {},
                 "paymentLines": [], "warnings": ["spreadsheet_no_paid_amounts"],
                 "amountColumns": [
                     {"column": 2, "header": "Quantity", "lines": [{"proInvoiceNumber": f"00123456{i}", "amountPaid": q} for i, q in enumerate((3, 1, 2))]},
                     {"column": 5, "header": "Invoice Charge",
                      "lines": [{"proInvoiceNumber": f"00123456{i}", "amountPaid": a} for i, a in enumerate(column_amounts)]},
                 ]}
        return [body, sheet]

    def test_a_code_read_column_that_adds_up_exactly_to_the_payment_is_the_detail(self):
        result = validate_and_merge(self._proof_sources(), tolerance=Decimal("0.01"))
        quality = result["quality"]
        self.assertTrue(quality["isReconciled"])
        self.assertEqual(result["status"], "Extracted")
        self.assertEqual([line["amountPaid"] for line in result["paymentLines"]], [100.0, 50.25, 24.75])
        self.assertIn("payment_detail_proven_by_total", quality["notes"])
        self.assertEqual(quality["paymentDetailProof"]["column"], "Invoice Charge")
        self.assertEqual(quality["paymentDetailProof"]["supersededLineCount"], 1)  # the body's misreading, set aside
        # A total written as negative (a ledger view of the same payment) is matched by its amount.
        negative = validate_and_merge(self._proof_sources(stated=-175.00), tolerance=Decimal("0.01"))
        self.assertTrue(negative["quality"]["isReconciled"])
        self.assertEqual(negative["remittanceMetadata"]["paymentTotal"], 175.0)

    def test_no_proof_without_an_exact_single_code_read_match(self):
        off_by_a_cent = validate_and_merge(self._proof_sources(column_amounts=(100.00, 50.25, 24.74)))
        self.assertFalse(off_by_a_cent["quality"]["isReconciled"])
        self.assertNotIn("paymentDetailProof", off_by_a_cent["quality"])
        read_by_a_model = validate_and_merge(self._proof_sources(strategy="content_understanding"))
        self.assertNotIn("paymentDetailProof", read_by_a_model["quality"])
        two_readings = self._proof_sources()
        two_readings[1]["amountColumns"].append(
            {"column": 6, "header": "Other", "lines": [{"proInvoiceNumber": f"00999999{i}", "amountPaid": a} for i, a in enumerate((75.0, 100.0))]})
        self.assertNotIn("paymentDetailProof", validate_and_merge(two_readings)["quality"])

    def test_the_same_payment_in_two_sources_counts_once_even_when_only_one_has_dates(self):
        pdf = {"candidateId": "doc-001", "sourceType": "attachment", "score": 100, "strategy": "content_understanding",
               "remittanceMetadata": {"paymentTotal": 30.00},
               "paymentLines": [{"proInvoiceNumber": "001234567", "amountPaid": 10.0, "invoiceDate": "2026-09-01", "lineReference": "INV-1"},
                                {"proInvoiceNumber": "001234568", "amountPaid": 20.0, "invoiceDate": "2026-09-02", "lineReference": "INV-2"}]}
        sheet = {"candidateId": "doc-002", "sourceType": "attachment", "score": 90, "strategy": "deterministic_parser",
                 "remittanceMetadata": {},
                 "paymentLines": [{"proInvoiceNumber": "001234567", "amountPaid": 10.0}, {"proInvoiceNumber": "001234568", "amountPaid": 20.0}]}
        result = validate_and_merge([pdf, sheet], tolerance=Decimal("0.01"))
        self.assertEqual(len(result["paymentLines"]), 2)
        self.assertTrue(result["quality"]["isReconciled"])
        self.assertEqual(len(result["quality"]["duplicateGroups"]), 2)
        # A PRO paid twice within ONE source is still two lines, not merged.
        twice = {**sheet, "paymentLines": sheet["paymentLines"] + [{"proInvoiceNumber": "001234567", "amountPaid": 10.0, "lineReference": "B"}]}
        self.assertEqual(len(validate_and_merge([twice], tolerance=Decimal("0.01"))["paymentLines"]), 3)

    def test_one_payment_for_several_pros_without_amounts_is_named(self):
        source = {"candidateId": "doc-002", "sourceType": "attachment", "score": 90,
                  "remittanceMetadata": {"payerName": "Fabrikam", "paymentTotal": 300.00},
                  "paymentLines": [{"proInvoiceNumber": pro, "amountPaid": None} for pro in ("001234567", "001234568", "001234569")]}
        quality = validate_and_merge([source], tolerance=Decimal("0.01"))["quality"]
        self.assertIn("amounts_not_itemized", quality["warnings"])
        self.assertEqual(quality["reviewReasons"], ["amounts_not_itemized"])  # the symptoms become notes
        self.assertIn("payment_total_mismatch", quality["notes"])
        self.assertTrue(quality["requiresReview"])
        # One PRO without an amount, or some lines with amounts, is not this case.
        partial = {**source, "paymentLines": [{"proInvoiceNumber": "001234567", "amountPaid": 100.0},
                                              {"proInvoiceNumber": "001234568", "amountPaid": None}]}
        self.assertNotIn("amounts_not_itemized", validate_and_merge([partial])["quality"]["warnings"])

    def test_a_logo_attachment_never_contests_the_payer(self):
        body = {"candidateId": "message-body", "sourceType": "message_body", "score": 40,
                "remittanceMetadata": {"payerName": "Fabrikam Inc.", "paymentTotal": 15.00},
                "paymentLines": [{"proInvoiceNumber": "001234567", "amountPaid": 15.00}]}
        logo = {"candidateId": "doc-002", "sourceType": "attachment", "score": 70,
                "remittanceMetadata": {"payerName": "Northwind Signs"}, "paymentLines": []}
        result = validate_and_merge([body, logo], tolerance=Decimal("0.01"))
        self.assertEqual(result["remittanceMetadata"]["payerName"], "Fabrikam Inc.")
        self.assertNotIn("conflicting_payerName", result["quality"]["warnings"])
        # Alone, it still names the payer (better than nothing; the analyst sees it).
        alone = validate_and_merge([{**logo, "paymentLines": [{"proInvoiceNumber": "001234567", "amountPaid": 15.00}]}])
        self.assertEqual(alone["remittanceMetadata"]["payerName"], "Northwind Signs")

    def test_spreadsheet_metadata_is_chosen_like_a_document_but_never_conflicts(self):
        body = {"candidateId": "message-body", "sourceType": "message_body", "score": 40,
                "remittanceMetadata": {"payerName": "Fabrikam and Sons, Inc."}, "paymentLines": []}
        sheet = {"candidateId": "doc-001", "sourceType": "attachment", "score": 90, "metadataRole": "advisory",
                 "remittanceMetadata": {"payerName": "Fabrikam & Sons", "remittanceDate": "2026-09-15", "paymentTotal": 15.00},
                 "paymentLines": [{"proInvoiceNumber": "001234567", "amountPaid": 10.00},
                                  {"proInvoiceNumber": "001234568", "amountPaid": 5.00}]}
        result = validate_and_merge([body, sheet], tolerance=Decimal("0.01"))
        self.assertEqual(result["remittanceMetadata"]["payerName"], "Fabrikam & Sons")  # the document outranks the email
        self.assertEqual(result["remittanceMetadata"]["remittanceDate"], "2026-09-15")
        self.assertNotIn("conflicting_payerName", result["quality"]["warnings"])
        self.assertTrue(result["quality"]["isReconciled"])  # the sheet's total reconciles the lines

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