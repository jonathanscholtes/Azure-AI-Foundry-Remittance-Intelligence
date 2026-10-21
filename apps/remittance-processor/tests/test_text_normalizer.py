import unittest

from msg_parser.text_normalizer import html_to_plain_text


class TextNormalizerTests(unittest.TestCase):
    def test_converts_html_and_omits_script_and_style_content(self):
        text = html_to_plain_text(
            "<html><style>hidden</style><body><p>Pay &amp; remit</p>"
            "<script>unsafe()</script><div>Invoice 001</div></body></html>"
        )

        self.assertEqual(text, "Pay & remit\nInvoice 001")


if __name__ == "__main__":
    unittest.main()