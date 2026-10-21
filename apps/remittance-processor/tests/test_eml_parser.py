import unittest
from email.message import EmailMessage

from msg_parser.eml_parser import parse_eml


class EmlParserTests(unittest.TestCase):
    def test_body_and_attachment_are_decomposed(self):
        message = EmailMessage()
        message["From"] = "Acme AP <ap@example.com>"
        message["Subject"] = "ACH Remittance"
        message["Date"] = "Thu, 15 Oct 2026 10:00:00 +0000"
        message["Message-ID"] = "<m1@example.com>"
        message.set_content("Payment total 100.00\nPRO 001234567 100.00")
        message.add_attachment(b"001234567,100.00\n", maintype="text", subtype="csv", filename="remittance.csv")

        parsed = parse_eml(message.as_bytes(), "x.eml")

        self.assertEqual(parsed.subject, "ACH Remittance")
        self.assertEqual(parsed.sender_email, "ap@example.com")
        self.assertIn("001234567", parsed.body)
        self.assertEqual(len(parsed.attachments), 1)
        self.assertEqual(parsed.attachments[0].name, "remittance.csv")
        self.assertEqual(parsed.attachments[0].extension, ".csv")
        self.assertEqual(parsed.attachments[0].content, b"001234567,100.00\n")
        self.assertTrue(parsed.sent_date.startswith("2026-10-15"))

    def test_body_only_message(self):
        message = EmailMessage()
        message["Subject"] = "Remittance"
        message.set_content("Remittance detail in body")
        parsed = parse_eml(message.as_bytes())
        self.assertEqual(parsed.attachments, [])
        self.assertIn("Remittance detail", parsed.body)

    def test_attached_email_is_unpacked_with_its_attachments(self):
        inner = EmailMessage()
        inner["From"] = "Payer AP <ap@payer.example>"
        inner["Subject"] = "Transfer Sept 09"
        inner.set_content("Proof of transfer $2150.00 for PRO 13600000802")
        inner.add_attachment(b"%PDF proof", maintype="application", subtype="pdf", filename="transfer.pdf")

        outer = EmailMessage()
        outer["Subject"] = "RV: Pago"
        outer.set_content("Already deducted per the attached email")
        outer.add_attachment(inner)

        parsed = parse_eml(outer.as_bytes(), "x.eml")

        self.assertIn("attached email", parsed.body)  # the outer body stays the message body
        by_name = {a.name: a for a in parsed.attachments}
        self.assertEqual(by_name["transfer.pdf"].embedded_in, "Transfer Sept 09")
        body = next(a for a in parsed.attachments if a.source_type == "embedded_message_body")
        self.assertIn(b"PRO 13600000802", body.content)
        self.assertEqual(parsed.warnings, [])

    def test_attachment_without_extension_is_typed_from_its_content(self):
        # "WIRE_TRANSFER_3690_..." with no extension and a generic content type: really a PDF.
        message = EmailMessage()
        message["Subject"] = "Payment (open with TIFF)"
        message.set_content("Attached")
        message.add_attachment(b"%PDF-1.7 remittance", maintype="application", subtype="octet-stream", filename="WIRE_TRANSFER_3690")
        message.add_attachment(b"II*\x00scan", maintype="application", subtype="octet-stream", filename="SCAN_0001")
        message.add_attachment(b"%PDF-1.7 named", maintype="application", subtype="pdf", filename="remit.pdf")

        by_name = {a.name: a for a in parse_eml(message.as_bytes(), "x.eml").attachments}
        self.assertEqual((by_name["WIRE_TRANSFER_3690"].extension, by_name["WIRE_TRANSFER_3690"].content_type), (".pdf", "application/pdf"))
        self.assertEqual(by_name["WIRE_TRANSFER_3690"].detected_type, ".pdf")
        self.assertEqual(by_name["SCAN_0001"].extension, ".tif")
        self.assertIsNone(by_name["remit.pdf"].detected_type)  # a known extension is trusted as-is

    def test_empty_content_rejected(self):
        with self.assertRaises(ValueError):
            parse_eml(b"")


if __name__ == "__main__":
    unittest.main()
