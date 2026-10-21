import unittest

from extraction.html_tables import extract_html_payment_tables


def _row(*cells, colspan=None):
    out = []
    for index, cell in enumerate(cells):
        span = f" colspan={colspan}" if colspan and index == 0 else ""
        out.append(f"<td{span}><p><span>{cell}</span></p></td>")
    return "<tr>" + "".join(out) + "</tr>"


METADATA = (
    "<table>"
    + _row("Payment Reference Number", "5242702")
    + _row("Payment Amount", "1,000.50")
    + "</table>"
)


def _detail(rows, total="1,000.50"):
    body = _row("Remittance Detail", colspan=4)
    body += _row("Document Reference Number", "Document Date", "Document Amount", "Amount Paid")
    for pro, amount in rows:
        body += _row(pro, "9/3/26", amount, amount)
    if total is not None:
        body += _row("", "", "Total", total)
    return f"<table>{body}</table>"


# A PRO may legitimately repeat (here with a zero adjustment row); the parser keeps every row.
ROWS = [("00900000101", "617.32"), ("00900000102", "383.18"), ("00900000102", "0.00")]


class HtmlTableTests(unittest.TestCase):
    def test_reads_rows_that_reconcile_to_the_table_total(self):
        html = f"<html><body><p>From Payer</p><p>Acme LLC</p>{METADATA}{_detail(ROWS)}</body></html>"
        result = extract_html_payment_tables(html, prefix="Subject: Remit\n\n")

        self.assertTrue(result["accepted"])
        self.assertEqual([(l["proInvoiceNumber"], l["amountPaid"]) for l in result["paymentLines"]],
                         [("00900000101", "617.32"), ("00900000102", "383.18"), ("00900000102", "0.00")])
        self.assertEqual(result["paymentLines"][0]["source"]["method"], "html_table")

    def test_condensed_text_keeps_metadata_and_drops_detail_rows(self):
        html = f"<p>From Payer</p><p>Acme LLC</p>{METADATA}{_detail(ROWS)}"
        text = extract_html_payment_tables(html, prefix="Subject: Remit\n\n")["condensedText"]

        self.assertTrue(text.startswith("Subject: Remit"))
        self.assertIn("Payment Amount | 1,000.50", text)
        self.assertIn("Document Reference Number | Document Date | Document Amount | Amount Paid", text)
        self.assertIn("3 payment detail rows were read directly", text)
        self.assertNotIn("00900000101", text)
        self.assertIn("Total | 1,000.50", text)

    def test_total_mismatch_falls_back_to_content_understanding(self):
        result = extract_html_payment_tables(METADATA + _detail(ROWS, total="999.00"))
        self.assertFalse(result["accepted"])
        self.assertEqual(result["tables"][0]["reason"], "table_total_mismatch")

    def test_unparseable_row_falls_back(self):
        result = extract_html_payment_tables(_detail([("00900000101", "617.32"), ("00900000102", "see note")], total=None))
        self.assertFalse(result["accepted"])
        self.assertTrue(result["tables"][0]["reason"].startswith("unparseable_row"))

    def test_without_total_row_is_accepted_and_left_to_reconciliation(self):
        result = extract_html_payment_tables(_detail(ROWS, total=None))
        self.assertTrue(result["accepted"])
        self.assertEqual(len(result["paymentLines"]), 3)

    def test_layout_tables_and_plain_bodies_are_ignored(self):
        self.assertFalse(extract_html_payment_tables(METADATA)["accepted"])
        self.assertFalse(extract_html_payment_tables("<p>Paid invoice 123 for $10.00</p>")["accepted"])
        self.assertFalse(extract_html_payment_tables(None)["accepted"])

    def test_table_nested_in_a_layout_table_is_found(self):
        html = f"<table><tr><td><p>Payment Advice</p>{_detail(ROWS)}</td></tr></table>"
        result = extract_html_payment_tables(html)
        self.assertTrue(result["accepted"])
        self.assertEqual(len(result["paymentLines"]), 3)


if __name__ == "__main__":
    unittest.main()
