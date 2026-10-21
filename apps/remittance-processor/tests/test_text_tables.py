import unittest

from extraction.text_tables import extract_text_payment_tables

HEADER = ["Remittance Detail", "Document Reference Number", "Document Date", "Document Currency",
          "Document Amount", "Amount Withheld", "Discount Taken", "Amount Paid"]
ROWS = [
    ["00900000101", "9/3/26", "USD", "617.32", "0.00", "0.00", "617.32"],
    ["05100000301-CR1", "9/4/26", "USD", "-4.25", "0.00", "0.00", "-4.25"],
    ["00900000102", "9/3/26", "USD", "634.00", "0.00", "0.00", "634.00"],
]


def _body(rows=ROWS, total="1,247.07", preamble=("Payment Amount", "1,247.07")):
    cells = list(preamble) + HEADER + [cell for row in rows for cell in row]
    if total is not None:
        cells += ["Total", "0.00", "0.00", total]
    return "Subject: Remittance\n\n" + "\n\n".join(cells)


class TextTableTests(unittest.TestCase):
    def test_reads_cell_per_line_table_that_reconciles_to_its_total(self):
        result = extract_text_payment_tables(_body())
        self.assertTrue(result["accepted"])
        self.assertEqual([(l["proInvoiceNumber"], l["amountPaid"]) for l in result["paymentLines"]],
                         [("00900000101", "617.32"), ("05100000301-CR1", "-4.25"), ("00900000102", "634.00")])
        self.assertEqual(result["tables"][0]["columns"][0], "Document Reference Number")  # title line skipped

    def test_condensed_text_keeps_metadata_header_and_total(self):
        text = extract_text_payment_tables(_body())["condensedText"]
        self.assertIn("Payment Amount\n1,247.07", text)
        self.assertIn("Amount Paid\n[3 payment detail rows were read directly", text)
        self.assertNotIn("00900000101", text)
        self.assertTrue(text.rstrip().endswith("Total\n0.00\n0.00\n1,247.07"))

    def test_a_dropped_cell_shifts_types_and_falls_back(self):
        rows = [ROWS[0], ["05100000301", "9/4/26", "-4.25", "0.00", "0.00", "-4.25"], ROWS[2]]
        self.assertFalse(extract_text_payment_tables(_body(rows))["accepted"])

    def test_total_mismatch_falls_back(self):
        result = extract_text_payment_tables(_body(total="1,300.00"))
        self.assertFalse(result["accepted"])
        self.assertEqual(result["tables"][0]["reason"], "table_total_mismatch")

    def test_missing_total_row_falls_back(self):
        self.assertFalse(extract_text_payment_tables(_body(total=None))["accepted"])

    def test_one_row_sub_tables_are_all_read(self):
        # Several sub-batches, each "Invoice / Amount" with one or more rows and its own Total.
        body = "\n".join([
            "Invoice", "Amount", "14700000903", "$ 789.81", "05900000401", "$ 1,191.91", "Total CA", "$ 1,981.72",
            "Invoice", "Amount", "05900000401", "$ 250.00", "Total AZ", "$ 250.00",
            "Invoice", "Amount", "14700000902", "$ 1,915.40", "Total CO", "$ 1,915.40",
        ])
        result = extract_text_payment_tables(body)
        self.assertTrue(result["accepted"])
        self.assertEqual(len(result["paymentLines"]), 4)

    def test_title_line_is_not_taken_as_a_column(self):
        # The header run starts with a title; columns are aligned on the repeating records, not the run.
        result = extract_text_payment_tables(_body())
        self.assertEqual(result["tables"][0]["columns"], HEADER[1:])

    def test_free_text_body_is_ignored(self):
        self.assertFalse(extract_text_payment_tables("Customer remittance payment for invoice 001234567 attached.")["accepted"])
        self.assertFalse(extract_text_payment_tables("")["accepted"])


if __name__ == "__main__":
    unittest.main()
