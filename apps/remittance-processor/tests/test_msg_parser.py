import sys
import types
import unittest
from unittest.mock import patch

from msg_parser.parser import parse_msg


class FakeAttachment:
    data = b"%PDF sample"
    longFilename = "REMITTANCE.pdf"
    mimetype = "application/pdf"
    cid = None


class FakeMessage:
    subject = "ACH Remittance"
    sender = "Billing <billing@example.com>"
    senderEmail = None
    date = "2026-10-05"
    messageId = "<message-001@example.com>"
    inReplyTo = "<message-000@example.com>"
    headerDict = {"References": "<message-000@example.com> <message-root@example.com>"}
    body = "Please find the remittance details attached."
    htmlBody = b"<p>Remittance</p>"
    attachments = [FakeAttachment()]

    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True


class MsgParserTests(unittest.TestCase):
    def test_preserves_attachment_bytes_and_message_metadata(self):
        message = FakeMessage()
        parser_module = types.SimpleNamespace(openMsg=lambda _: message)
        with patch.dict(sys.modules, {"extract_msg": parser_module}):
            result = parse_msg(b"original msg", "ACH REMITTANCE.msg")

        self.assertEqual(result.subject, "ACH Remittance")
        self.assertEqual(result.sender_email, "billing@example.com")
        self.assertEqual(result.message_id, "<message-001@example.com>")
        self.assertEqual(result.in_reply_to, "<message-000@example.com>")
        self.assertEqual(result.references, ["<message-000@example.com>", "<message-root@example.com>"])
        self.assertEqual(result.attachments[0].name, "REMITTANCE.pdf")
        self.assertEqual(result.attachments[0].content, b"%PDF sample")
        self.assertTrue(message.closed)

    def test_does_not_deencapsulate_html_when_plain_body_present(self):
        # Reading htmlBody triggers the slow RTF->HTML deencapsulation; it must be skipped when a
        # usable plain-text body exists (the cause of large-remittance intake stalls).
        class HtmlTracker(FakeMessage):
            accessed = False

            @property
            def htmlBody(self):
                type(self).accessed = True
                return b"<p>should not be read</p>"

        message = HtmlTracker()
        parser_module = types.SimpleNamespace(openMsg=lambda _: message)
        with patch.dict(sys.modules, {"extract_msg": parser_module}):
            result = parse_msg(b"x", "a.msg")

        self.assertFalse(HtmlTracker.accessed)
        self.assertIsNone(result.html_body)
        self.assertEqual(result.body, "Please find the remittance details attached.")

    def test_falls_back_to_html_body_when_plain_body_missing(self):
        class NoPlainBody(FakeMessage):
            body = ""

        message = NoPlainBody()
        parser_module = types.SimpleNamespace(openMsg=lambda _: message)
        with patch.dict(sys.modules, {"extract_msg": parser_module}):
            result = parse_msg(b"x", "a.msg")

        self.assertIn("Remittance", result.html_body)

    def test_unpacks_nested_embedded_emails_with_lineage(self):
        # "See the attached email": the proof of payment is two levels down, inside attached emails.
        class Proof:
            data = b"%PDF proof"
            longFilename = "TRANSFER SEPT 09 2150.00.pdf"
            mimetype = "application/pdf"
            cid = None

        class Inner:
            subject = "Transfer Sept 09"
            sender = "Payer AP <ap@payer.example>"
            date = "2026-09-09"
            body = "Proof of transfer $2150.00 for PRO 13600000802"
            attachments = [Proof()]

        class Forwarded:
            subject = "RV: Pago"
            sender = "Payer AP <ap@payer.example>"
            date = "2026-09-30"
            body = "Sending the last transfers"
            attachments = [types.SimpleNamespace(data=Inner(), longFilename="Transfer.msg")]

        class Outer(FakeMessage):
            attachments = [FakeAttachment(), types.SimpleNamespace(data=Forwarded(), longFilename="RV Pago.msg")]

        message = Outer()
        with patch.dict(sys.modules, {"extract_msg": types.SimpleNamespace(openMsg=lambda _: message)}):
            result = parse_msg(b"x", "a.msg")

        self.assertEqual(result.warnings, [])
        names = [(a.name, a.source_type, a.embedded_in) for a in result.attachments]
        self.assertEqual(names[0], ("REMITTANCE.pdf", "attachment", None))
        self.assertIn(("embedded-email-2.txt", "embedded_message_body", "RV: Pago"), names)
        self.assertIn(("TRANSFER SEPT 09 2150.00.pdf", "attachment", "RV: Pago > Transfer Sept 09"), names)
        inner_body = next(a for a in result.attachments if a.embedded_in == "RV: Pago > Transfer Sept 09" and a.extension == ".txt")
        self.assertIn(b"PRO 13600000802", inner_body.content)
        self.assertIn(b"Subject: Transfer Sept 09", inner_body.content)

    def test_attachment_without_data_is_still_flagged(self):
        class Broken:
            data = None
            longFilename = "unreadable.bin"

        class WithBroken(FakeMessage):
            attachments = [Broken()]

        message = WithBroken()
        with patch.dict(sys.modules, {"extract_msg": types.SimpleNamespace(openMsg=lambda _: message)}):
            result = parse_msg(b"x", "a.msg")
        self.assertEqual(result.warnings, ["attachment 1 skipped: no file data"])

    def test_rejects_empty_input(self):
        with self.assertRaises(ValueError):
            parse_msg(b"")


if __name__ == "__main__":
    unittest.main()