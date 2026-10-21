import unittest

from extraction.spreadsheet_parser import parse_csv


class SpreadsheetParserTests(unittest.TestCase):
    def test_prefers_amount_paid_and_skips_total_rows(self):
        content = (
            "Statement,October\nPRO Number,Invoice Total,Amount Due,Amount Paid,Discount\n"
            "001234567,120.00,120.00,100.00,20.00\nTOTAL,,,100.00,\n"
        ).encode("utf-8")

        result = parse_csv(content, "remittance.csv")

        self.assertEqual(len(result["paymentLines"]), 1)
        self.assertEqual(result["paymentLines"][0]["proInvoiceNumber"], "001234567")
        self.assertEqual(result["paymentLines"][0]["amountPaid"], "100.00")
        self.assertEqual(result["paymentLines"][0]["source"]["row"], 3)
        self.assertTrue(any("total_or_subtotal" in warning for warning in result["warnings"]))

    def test_unresolved_amount_is_preserved_as_review_warning(self):
        result = parse_csv(
            b"PRO,Amount Paid\n001234567,not available\n",
            "remittance.csv",
        )

        self.assertIsNone(result["paymentLines"][0]["amountPaid"])
        self.assertIn("missing_or_invalid_amount_paid", result["paymentLines"][0]["warnings"])


    def test_abbreviated_paid_header_is_recognized(self):
        result = parse_csv(b"Pro#,Open Amt Due,Tot Amt Pd\n13600000805,2150,2150\nTotals for Download-,2150,2150\n", "ar.csv")
        self.assertEqual([(l["proInvoiceNumber"], l["amountPaid"]) for l in result["paymentLines"]], [("13600000805", "2150")])
        self.assertNotIn("header_row_not_detected; first two columns used and require review", result["warnings"])

    def test_statement_with_an_empty_paid_column_contributes_no_lines(self):
        # An open-items AR export: amounts owed, nothing paid. Never read "Open Amt Due" as paid.
        content = b"Pro#,Open Amt Due,Original Amt,Tot Amt Pd\n13600000805,2150,2150,\n13600000801,1488.2,1488.2,\nTotals for Download-,3638.2,3638.2,0\n"
        result = parse_csv(content, "statement.csv")
        self.assertEqual(result["paymentLines"], [])
        self.assertEqual(result["warnings"], ["spreadsheet_no_paid_amounts"])

    def test_fallback_skips_an_unrecognized_header_row_and_totals(self):
        result = parse_csv(b"Ref,Value\n001234567,10.00\nTotals,10.00\n", "x.csv")
        self.assertEqual([(l["proInvoiceNumber"], l["amountPaid"]) for l in result["paymentLines"]], [("001234567", "10.00")])
        self.assertIn("header_row_not_detected; first two columns used and require review", result["warnings"])


if __name__ == "__main__":
    unittest.main()