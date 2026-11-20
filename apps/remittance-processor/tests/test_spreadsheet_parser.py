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

    def test_unlabelled_total_row_is_recognized_by_its_sum(self):
        # A bare =SUM row (no "Total" label) and a repeat of it below are the table's total, not lines.
        content = b"PRO Number,Amount Paid\n001234567,10.00\n001234568,15.50\n,25.50\n\n,25.50\n"
        result = parse_csv(content, "remittance.csv")
        self.assertEqual([line["proInvoiceNumber"] for line in result["paymentLines"]], ["001234567", "001234568"])
        self.assertNotIn("spreadsheet_total_mismatch", result["warnings"])
        self.assertEqual(sum(1 for w in result["warnings"] if w.endswith("total_or_subtotal_row_skipped")), 2)

    def test_a_sheet_read_as_a_statement_offers_its_numeric_columns_as_candidates(self):
        from unittest.mock import patch

        from extraction import column_mapping

        content = (b"Invoice Number,Invoice Date,Units,Invoice Charge,Due Date\n"
                   b"001234567,09/01/2026,3,100.00,10/01/2026\n001234568,09/02/2026,1,50.25,10/02/2026\n,,,150.25,\n")
        statement = {"headerRow": 0, "identifierColumn": 0, "amountPaidColumn": None, "tableKind": "statement_or_open_items",
                     "source": "agent", "agent": "x", "rulesAgree": None}
        with patch.object(column_mapping, "map_columns", return_value=statement):
            result = parse_csv(content, "statement.csv")
        self.assertEqual(result["paymentLines"], [])  # nothing is decided here
        columns = {column["header"]: [line["amountPaid"] for line in column["lines"]] for column in result["amountColumns"]}
        self.assertEqual(columns, {"Units": ["3", "1"], "Invoice Charge": ["100.00", "50.25"]})  # dates are not amounts

    def test_a_repeated_header_row_is_not_a_payment_line(self):
        content = b"PRO Number,Amount Paid\n001234567,10.00\nPRO Number,\n001234568,5.00\nTotal,15.00\n"
        result = parse_csv(content, "remittance.csv")
        self.assertEqual([line["proInvoiceNumber"] for line in result["paymentLines"]], ["001234567", "001234568"])
        self.assertNotIn("spreadsheet_total_mismatch", result["warnings"])

    def test_a_line_missing_its_pro_is_not_mistaken_for_a_total(self):
        content = b"PRO Number,Amount Paid\n001234567,10.00\n,10.00\n"  # one line above: can't be a total
        lines = parse_csv(content, "remittance.csv")["paymentLines"]
        self.assertEqual(len(lines), 2)
        self.assertIn("missing_invoice_number", lines[1]["warnings"])

    def test_formula_without_a_saved_value_is_unresolved(self):
        import io

        from openpyxl import Workbook

        from extraction.spreadsheet_parser import parse_xlsx

        workbook = Workbook()
        sheet = workbook.active
        sheet.append(["PRO Number", "Amount Paid"])
        sheet.append(["001234567", 10.0])
        sheet.append(["001234568", "=B2*2"])  # openpyxl saves no computed value
        buffer = io.BytesIO()
        workbook.save(buffer)
        result = parse_xlsx(buffer.getvalue(), "remittance.xlsx")
        self.assertIn("Sheet!B3:formula_unresolved", result["warnings"])
        self.assertIn("spreadsheet_error_cell", result["paymentLines"][1]["warnings"])

    def test_workbook_reader_follows_the_file_signature_not_the_extension(self):
        from unittest.mock import patch

        from extraction import spreadsheet_parser

        legacy = bytes.fromhex("d0cf11e0a1b11ae1") + b"\x00" * 8
        modern = b"PK\x03\x04" + b"\x00" * 8
        with patch.object(spreadsheet_parser, "parse_xls", return_value="xls") as xls, \
                patch.object(spreadsheet_parser, "parse_xlsx", return_value="xlsx") as xlsx:
            self.assertEqual(spreadsheet_parser.parse_spreadsheet(legacy, "remittance.xlsx"), "xls")  # legacy workbook named .xlsx
            self.assertEqual(spreadsheet_parser.parse_spreadsheet(modern, "remittance.xls"), "xlsx")
            self.assertEqual(spreadsheet_parser.parse_spreadsheet(b"other", "remittance.xlsx"), "xlsx")  # unknown: by extension
        self.assertEqual((xls.call_count, xlsx.call_count), (1, 2))


if __name__ == "__main__":
    unittest.main()