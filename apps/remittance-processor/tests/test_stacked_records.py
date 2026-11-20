import unittest

from extraction.stacked_records import extract_stacked_records

RECORDS = [
    ["00900000101          PO 7100201     $312.40", "Adjustment        $27.35", "$339.75"],
    ["00900000102          PO 7100202     $1,804.15", "$1,804.15"],
    ["00900000103          PO 7100203     $529.60", "Adjustment        ($4.10)", "$525.50"],
    # an offset against an earlier payment, then a re-issue: several modifiers on one record
    ["00900000104          PO 7100204     $190.00", "Check 50000001    ($240.00)", "Check 50000002    $60.00",
     "Adjustment        $17.85", "$27.85"],
]


def _body(records=RECORDS, stated="$2,697.25", footer=("Questions about this payment? Contact accounts payable.",)):
    head = ["Subject: Payment Notice", "", "Funds were sent to your account on 09/15/2026.",
            "Payment Number: 50000099"]
    if stated is not None:
        head.append(f"Total Amount: {stated}")
    head += ["", "Reference        Order          Gross        Net", "-" * 60]
    return "\r\n".join(head + [line for record in records for line in record] + ["=" * 40, *footer])


class StackedRecordTests(unittest.TestCase):
    def test_reads_records_that_balance_and_reconcile_to_the_stated_total(self):
        result = extract_stacked_records(_body())
        self.assertTrue(result["accepted"])
        lines = result["paymentLines"]
        self.assertEqual([(l["proInvoiceNumber"], l["amountPaid"]) for l in lines],
                         [("00900000101", "339.75"), ("00900000102", "1804.15"), ("00900000103", "525.50"),
                          ("00900000104", "27.85")])
        self.assertEqual((lines[0]["originalInvoiceAmount"], lines[0]["adjustmentAmount"], lines[0]["lineReference"]),
                         (312.40, 27.35, "PO 7100201"))
        self.assertIsNone(lines[1]["adjustmentAmount"])  # no modifier lines
        self.assertEqual(lines[3]["adjustmentAmount"], -162.15)  # net minus gross across all modifiers
        self.assertEqual(lines[0]["source"]["method"], "stacked_records")
        self.assertEqual(result["tables"][0]["statedTotal"], "2697.25")

    def test_condensed_text_keeps_header_and_footer_only(self):
        text = extract_stacked_records(_body())["condensedText"]
        self.assertIn("Total Amount: $2,697.25", text)
        self.assertIn("[4 payment detail records were read directly", text)
        self.assertIn("Contact accounts payable", text)
        self.assertNotIn("00900000101", text)
        self.assertNotIn("Adjustment", text)

    def test_unbalanced_record_falls_back(self):
        records = [RECORDS[0], ["00900000102          PO 7100202     $1,804.15", "$1,814.15"], *RECORDS[2:]]
        result = extract_stacked_records(_body(records, stated="$2,707.25"))
        self.assertFalse(result["accepted"])
        self.assertTrue(result["tables"][0]["reason"].startswith("record_does_not_balance"))

    def test_record_without_net_line_falls_back_never_skips(self):
        records = [RECORDS[0], ["00900000102          PO 7100202     $1,804.15"], *RECORDS[2:]]
        result = extract_stacked_records(_body(records, stated="$893.10"))
        self.assertFalse(result["accepted"])
        self.assertTrue(result["tables"][0]["reason"].startswith("record_without_net_line"))

    def test_total_mismatch_falls_back(self):
        result = extract_stacked_records(_body(stated="$2,700.00"))
        self.assertFalse(result["accepted"])
        self.assertEqual(result["tables"][0]["reason"], "stated_total_mismatch")

    def test_missing_or_conflicting_stated_total_falls_back(self):
        self.assertEqual(extract_stacked_records(_body(stated=None))["tables"][0]["reason"], "no_stated_total")
        conflicting = extract_stacked_records(_body(footer=("Payment Total: $4,000.00",)))
        self.assertEqual(conflicting["tables"][0]["reason"], "conflicting_stated_totals")

    def test_a_loose_amount_between_records_falls_back(self):
        records = [RECORDS[0], ["Subtotal $339.75"], *RECORDS[1:]]
        result = extract_stacked_records(_body(records))
        self.assertFalse(result["accepted"])
        self.assertTrue(result["tables"][0]["reason"].startswith("unattached_amount"))

    def test_a_one_cell_per_line_table_or_free_text_is_not_this_layout(self):
        table = "\n".join(["Invoice", "Amount", "14700000903", "$ 789.81", "05900000401", "$ 1,191.91", "Total", "$ 1,981.72"])
        self.assertFalse(extract_stacked_records(table)["accepted"])
        self.assertEqual(extract_stacked_records("Payment for invoice 001234567 is attached.")["tables"], [])
        self.assertFalse(extract_stacked_records("")["accepted"])


if __name__ == "__main__":
    unittest.main()
