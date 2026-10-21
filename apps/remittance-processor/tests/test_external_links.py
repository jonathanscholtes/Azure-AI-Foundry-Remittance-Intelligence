import types
import unittest

from orchestration.activities import _external_link_count, _org_from_email


def _parsed(body):
    return types.SimpleNamespace(body=body, html_body=None)


class ExternalLinkHeuristicTests(unittest.TestCase):
    """Only links that plausibly hide remittance detail should be flagged - not signature/marketing links."""

    def test_signature_and_marketing_links_are_ignored(self):
        body = (
            "Today we processed your ACH payment. Summary below.\n"
            "USA Website <https://example.com/us>\n"
            "Product Catalog <https://example.com/catalog>\n"
            "Follow us <https://twitter.com/acme>"
        )
        self.assertEqual(_external_link_count(_parsed(body)), 0)

    def test_remittance_detail_portal_link_is_counted(self):
        body = "Your remittance details are available in the portal: https://portal.example.com/login"
        self.assertEqual(_external_link_count(_parsed(body)), 1)

    def test_action_phrase_link_is_counted(self):
        body = "Please view your payment remittance at https://pay.example.com/view to see line items."
        self.assertEqual(_external_link_count(_parsed(body)), 1)

    def test_body_without_links_is_zero(self):
        self.assertEqual(_external_link_count(_parsed("No links here - the full remittance is in the body.")), 0)


class OrgFromEmailTests(unittest.TestCase):
    def test_derives_org_from_company_domain(self):
        self.assertEqual(_org_from_email("AP_Riverside@tailspin.com"), "Tailspin")
        self.assertEqual(_org_from_email("payments@northwind.com"), "Northwind")

    def test_handles_compound_tld_and_subdomain(self):
        self.assertEqual(_org_from_email("billing@acme.co.uk"), "Acme")
        self.assertEqual(_org_from_email("ap@mail.tailspin.com"), "Tailspin")

    def test_free_mail_provider_returns_none(self):
        self.assertIsNone(_org_from_email("someone@gmail.com"))

    def test_missing_or_malformed_returns_none(self):
        self.assertIsNone(_org_from_email(None))
        self.assertIsNone(_org_from_email("not-an-email"))


if __name__ == "__main__":
    unittest.main()
