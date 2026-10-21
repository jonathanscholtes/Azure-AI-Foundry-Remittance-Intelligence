import unittest

from extraction.content_understanding import analyze_binary_candidate, normalize_analysis


class FakePoller:
    operation_id = "operation-123"

    def result(self):
        return {
            "contents": [
                {
                    "fields": {
                        "payerName": {"valueString": "Acme Transport", "confidence": 0.98},
                        "paymentTotal": {"valueNumber": 105.25},
                        "paymentLines": {
                            "valueArray": [
                                {
                                    "valueObject": {
                                        "proInvoiceNumber": {
                                            "valueString": "001234567X",
                                            "confidence": 0.91,
                                            "source": [{"pageNumber": 1}],
                                        },
                                        "amountPaid": {"valueNumber": 105.25, "confidence": 0.95},
                                    }
                                }
                            ]
                        },
                    }
                }
            ]
        }


class FakeClient:
    def __init__(self):
        self.args = None

    def begin_analyze_binary(self, *args, **kwargs):
        self.args = (args, kwargs)
        return FakePoller()


class ContentUnderstandingTests(unittest.TestCase):
    def test_binary_candidate_uses_content_type_and_returns_operation_id(self):
        client = FakeClient()
        result = analyze_binary_candidate(
            endpoint="https://foundry.example",
            analyzer_id="ach.remittance.v1",
            content=b"%PDF",
            content_type="application/pdf",
            candidate_id="doc-001",
            source_type="attachment",
            client=client,
        )

        self.assertEqual(client.args[0], ("ach.remittance.v1", b"%PDF"))
        self.assertEqual(client.args[1]["content_type"], "application/pdf")
        self.assertEqual(result["operationId"], "operation-123")
        self.assertEqual(result["normalized"]["remittanceMetadata"]["payerName"], "Acme Transport")
        self.assertEqual(
            result["normalized"]["paymentLines"][0]["proInvoiceNumber"],
            "001234567X",
        )
        self.assertEqual(
            result["normalized"]["paymentLines"][0]["source"]["fieldSources"]["proInvoiceNumber"],
            [{"pageNumber": 1}],
        )

    def test_missing_lines_normalize_to_empty_array(self):
        result = normalize_analysis({}, candidate_id="body", source_type="message_body")

        self.assertEqual(result["paymentLines"], [])
        self.assertIsNone(result["remittanceMetadata"]["payerName"])

    def test_collects_lines_from_every_content_block(self):
        result = normalize_analysis(
            {
                "contents": [
                    {"fields": {"payerName": {"valueString": "Acme"}, "paymentLines": {"valueArray": [{"valueObject": {"proInvoiceNumber": {"valueString": "001234567"}, "amountPaid": {"valueNumber": 10}}}]}}},
                    {"fields": {"payerName": {"valueString": "Other"}, "paymentLines": {"valueArray": [{"valueObject": {"proInvoiceNumber": {"valueString": "001234568"}, "amountPaid": {"valueNumber": 20}}}]}}},
                ]
            },
            candidate_id="doc-1",
            source_type="attachment",
        )

        self.assertEqual(len(result["paymentLines"]), 2)
        self.assertEqual(result["paymentLines"][1]["source"]["contentIndex"], 1)
        self.assertIn("conflicting_payerName_within_candidate", result["warnings"])


MULTI_PAYMENT_MARKDOWN = """```text
Today we processed three ACH payments to you.  Please see below for payment summary.

Invoice
 Amount
14700000903
 $        789.81
14700000905
 $    1,290.66
14700000901
 $        467.19
14700000904
 $    1,046.63
05900000401
 $    1,191.91
Total CA
 $    1,200.00
*Scheduled for 10/7/26*

Invoice
 Amount
05900000401
 $        250.00
Total AZ
 $        250.00
*Scheduled for 10/8/26*

Invoice
 Amount
14700000902
 $    1,915.40
Total CO
 $    1,915.40
*Scheduled for 10/7/26*
```"""


class MultiBatchRecoveryTests(unittest.TestCase):
    """A single email of per-region sub-batches: no header total/date, only subtotals + schedules."""

    def _analyze(self, markdown, extra_fields=None):
        fields = {"payerName": {"valueString": "Fabrikam Wine & Spirits", "confidence": 1}}
        fields.update(extra_fields or {})
        return normalize_analysis(
            {"contents": [{"markdown": markdown, "fields": fields}]},
            candidate_id="message-body",
            source_type="message_body",
        )

    def test_derives_total_from_subtotals_and_earliest_scheduled_date(self):
        result = self._analyze(MULTI_PAYMENT_MARKDOWN)
        metadata = result["remittanceMetadata"]
        # 1,200.00 + 250.00 + 1,915.40
        self.assertAlmostEqual(metadata["paymentTotal"], 3365.40, places=2)
        self.assertEqual(metadata["remittanceDate"], "2026-10-07")
        self.assertIn("payment_total_derived_from_subtotals", result["warnings"])
        self.assertIn("remittance_date_derived_from_schedule", result["warnings"])
        self.assertEqual(result["metadataEvidence"]["paymentTotal"]["source"], "derived:sum_of_3_subtotals")

    def test_does_not_override_values_the_analyzer_already_captured(self):
        result = self._analyze(
            MULTI_PAYMENT_MARKDOWN,
            extra_fields={
                "paymentTotal": {"valueNumber": 3365.40, "confidence": 0.9},
                "remittanceDate": {"valueDate": "2026-10-08", "confidence": 0.9},
            },
        )
        self.assertEqual(result["remittanceMetadata"]["remittanceDate"], "2026-10-08")
        self.assertNotIn("payment_total_derived_from_subtotals", result["warnings"])
        self.assertNotIn("remittance_date_derived_from_schedule", result["warnings"])

    def test_no_markdown_leaves_metadata_untouched(self):
        result = normalize_analysis(
            {"contents": [{"fields": {"payerName": {"valueString": "Acme"}}}]},
            candidate_id="body",
            source_type="message_body",
        )
        self.assertIsNone(result["remittanceMetadata"]["paymentTotal"])
        self.assertIsNone(result["remittanceMetadata"]["remittanceDate"])


class EdiImpliedDecimalTests(unittest.TestCase):
    """X12 RMR addenda encode money without a decimal point: 123450 means $1,234.50, not $123,450."""

    def _line(self, amount, reference, markdown=None):
        result = normalize_analysis(
            {
                "contents": [
                    {
                        **({"markdown": markdown} if markdown else {}),
                        "fields": {
                            "paymentLines": {
                                "valueArray": [
                                    {
                                        "valueObject": {
                                            "proInvoiceNumber": {"valueString": "78000001201"},
                                            "amountPaid": {"valueNumber": amount},
                                            "originalInvoiceAmount": {"valueNumber": amount},
                                            "lineReference": {"valueString": reference},
                                        }
                                    }
                                ]
                            }
                        }
                    }
                ]
            },
            candidate_id="doc-001",
            source_type="attachment",
        )
        return result["paymentLines"][0]

    def test_rmr_addenda_integer_gets_implied_two_decimals(self):
        line = self._line(123450, "RMR*IV*78000001201*123450***123450****\\")
        self.assertEqual(line["amountPaid"], 1234.50)
        self.assertEqual(line["originalInvoiceAmount"], 1234.50)
        self.assertIn("amount_normalized_from_edi_addenda", line.get("warnings", []))

    def test_amount_with_cents_is_left_alone(self):
        line = self._line(1234.50, "RMR*IV*78000001201*123450***123450****\\")
        self.assertEqual(line["amountPaid"], 1234.50)
        self.assertNotIn("amount_normalized_from_edi_addenda", line.get("warnings", []))

    def test_non_edi_reference_is_left_alone(self):
        line = self._line(500, "Invoice 78000001201")
        self.assertEqual(line["amountPaid"], 500)
        self.assertNotIn("amount_normalized_from_edi_addenda", line.get("warnings", []))

    # A bank payment-detail page: the analyzer took the trace id as the line reference, not the RMR segment.
    BANK_PAGE = (
        "<table><tr><th>Beneficiary Name</th><th>Credit/(Debit)</th><th>Pmnt ID</th></tr>"
        "<tr><td>WINGTIP FREIGHT</td><td>1,234.50</td><td>639654<br>598</td></tr>"
        "<tr><td>Addenda:</td><td colspan=\"5\">RMR*IV*78000001201*123450***123450****\\</td></tr></table>"
    )

    def test_rmr_segment_in_the_source_converts_when_the_reference_is_another_id(self):
        line = self._line(123450, "639654598", markdown=self.BANK_PAGE)
        self.assertEqual(line["amountPaid"], 1234.50)
        self.assertIn("amount_normalized_from_edi_addenda", line["warnings"])
        self.assertTrue(line["source"]["grounded"])  # the converted amount still grounds against the source

    def test_rmr_segment_for_another_pro_does_not_convert(self):
        other = self.BANK_PAGE.replace("78000001201", "78000009999")
        line = self._line(123450, "639654598", markdown=other)
        self.assertEqual(line["amountPaid"], 123450)
        self.assertNotIn("amount_normalized_from_edi_addenda", line.get("warnings", []))


import re

from extraction.content_understanding import (
    analyze_candidate_content,
    chunk_text_for_analysis,
    merge_normalized_analyses,
)


def _large_remittance_body(n):
    header = (
        "From Payer\nWingtip Freight US LLC\nPayment Date\n10/5/26\nPayment Amount\n1,000.00\n\n"
        "Remittance Detail\nDocument Reference Number\nAmount Paid\n"
    )
    rows = "".join(f"{9000000000 + i}\n9/3/26\nUSD\n{(i % 90) + 1}.00\n0.00\n0.00\n{(i % 90) + 1}.00\n" for i in range(n))
    return header + rows


class ChunkingTests(unittest.TestCase):
    def test_small_text_is_a_single_chunk(self):
        self.assertEqual(chunk_text_for_analysis("a\nb\nc", max_chars=1000), ["a\nb\nc"])

    def test_large_text_splits_covering_every_line(self):
        text = _large_remittance_body(300)
        chunks = chunk_text_for_analysis(text, max_chars=2000, overlap_lines=8)
        self.assertGreater(len(chunks), 1)
        for chunk in chunks:
            self.assertLessEqual(len(chunk), 2300)
        joined = "".join(chunks)
        for i in range(300):
            self.assertIn(str(9000000000 + i), joined)  # no row dropped at a boundary

    def test_merge_takes_first_metadata_and_dedups_by_pro(self):
        parts = [
            {"remittanceMetadata": {"payerName": "Acme", "paymentTotal": 1000.0}, "metadataEvidence": {}, "paymentLines": [{"proInvoiceNumber": "1", "amountPaid": 10.0}], "warnings": []},
            {"remittanceMetadata": {}, "metadataEvidence": {}, "paymentLines": [{"proInvoiceNumber": "1", "amountPaid": 10.0}, {"proInvoiceNumber": "2", "amountPaid": 20.0}], "warnings": ["w"]},
        ]
        merged = merge_normalized_analyses(parts, candidate_id="message-body", source_type="message_body")
        self.assertEqual(merged["remittanceMetadata"]["payerName"], "Acme")
        self.assertEqual(len(merged["paymentLines"]), 2)
        self.assertIn("w", merged["warnings"])

    def test_merge_prefers_the_row_that_carries_an_amount(self):
        parts = [
            {"remittanceMetadata": {}, "paymentLines": [{"proInvoiceNumber": "1", "amountPaid": None}], "warnings": []},
            {"remittanceMetadata": {}, "paymentLines": [{"proInvoiceNumber": "1", "amountPaid": 10.0}], "warnings": []},
        ]
        merged = merge_normalized_analyses(parts, candidate_id="c", source_type="attachment")
        self.assertEqual(merged["paymentLines"][0]["amountPaid"], 10.0)

    def test_merge_keeps_a_repeated_pro_outside_the_overlap(self):
        # PRO 1 is paid twice in the document (partial payments); only an overlap copy is a duplicate.
        parts = [
            {"remittanceMetadata": {}, "paymentLines": [{"proInvoiceNumber": "1", "amountPaid": 10.0}, {"proInvoiceNumber": "2", "amountPaid": 20.0}], "warnings": []},
            {"remittanceMetadata": {}, "paymentLines": [{"proInvoiceNumber": "2", "amountPaid": 20.0}, {"proInvoiceNumber": "1", "amountPaid": 10.0}], "warnings": []},
        ]
        merged = merge_normalized_analyses(parts, candidate_id="c", source_type="message_body", overlaps=["", "2\n20.00\n"])
        self.assertEqual([(l["proInvoiceNumber"], l["amountPaid"]) for l in merged["paymentLines"]], [("1", 10.0), ("2", 20.0), ("1", 10.0)])

    def test_merge_prefers_the_grounded_overlap_copy(self):
        parts = [
            {"remittanceMetadata": {}, "paymentLines": [{"proInvoiceNumber": "7", "amountPaid": 5.0, "source": {"grounded": False}}], "warnings": []},
            {"remittanceMetadata": {}, "paymentLines": [{"proInvoiceNumber": "7", "amountPaid": 5.0, "source": {"grounded": True}}], "warnings": []},
        ]
        merged = merge_normalized_analyses(parts, candidate_id="c", source_type="attachment", overlaps=["", "7 5.00\n"])
        self.assertEqual(len(merged["paymentLines"]), 1)
        self.assertTrue(merged["paymentLines"][0]["source"]["grounded"])
        self.assertEqual(merged["ungroundedLineCount"], 0)

    def test_analyze_candidate_content_chunks_large_text_and_recovers_all_lines(self):
        calls = []

        def fake_analyze(*, endpoint, analyzer_id, content, content_type, candidate_id, source_type):
            text = content.decode("utf-8")
            calls.append(len(text))
            pros = re.findall(r"\b(900000\d{4})\b", text)
            metadata = {"payerName": "Wingtip Freight US LLC"} if "From Payer" in text else {}
            return {
                "operationId": None,
                "normalized": {
                    "remittanceMetadata": metadata,
                    "metadataEvidence": {},
                    "paymentLines": [{"proInvoiceNumber": pro, "amountPaid": 1.0} for pro in pros],
                    "warnings": [],
                },
                "raw": {},
                "usage": {"calls": 1, "pages": {"minimal": 1}, "contextualizationTokens": 1000, "tokens": {"gpt-5.5-output": 10}},
            }

        body = _large_remittance_body(300).encode("utf-8")
        result = analyze_candidate_content(
            analyze=fake_analyze, endpoint="x", analyzer_id="a", content=body, content_type="text/plain",
            candidate_id="message-body", source_type="message_body", max_chars=2000,
        )
        self.assertGreater(len(calls), 1)  # it chunked
        pros = {line["proInvoiceNumber"] for line in result["normalized"]["paymentLines"]}
        self.assertEqual(len(pros), 300)  # every line captured, overlaps de-duplicated
        self.assertEqual(result["normalized"]["remittanceMetadata"]["payerName"], "Wingtip Freight US LLC")
        # Every chunk is a billed operation: its usage is summed for the source.
        self.assertEqual(result["usage"]["calls"], len(calls))
        self.assertEqual(result["usage"]["contextualizationTokens"], 1000 * len(calls))
        self.assertEqual(result["usage"]["tokens"]["gpt-5.5-output"], 10 * len(calls))


from extraction.content_understanding import ground_lines

# One value per line, as Outlook renders an HTML table to plain text.
CELL_PER_LINE = "\n".join(
    ["10600000501", "9/18/26", "USD", "1,140.93", "0.00", "1,140.93",
     "10700000601", "9/17/26", "USD", "654.83", "0.00", "654.83",
     "11000000701", "9/3/26", "USD", "1,216.00", "0.00", "1,216.00"]
)


class GroundingTests(unittest.TestCase):
    def test_correct_pairs_are_grounded(self):
        lines = [{"proInvoiceNumber": "10700000601", "amountPaid": 654.83}, {"proInvoiceNumber": "11000000701", "amountPaid": 1216}]
        self.assertEqual(ground_lines(lines, CELL_PER_LINE), 0)
        self.assertTrue(all(line["source"]["grounded"] for line in lines))

    def test_amount_shifted_from_the_previous_row_is_flagged(self):
        # The extractor paired PRO 11000000701 with the previous row's 654.83.
        lines = [{"proInvoiceNumber": "10700000601", "amountPaid": 1140.93}, {"proInvoiceNumber": "11000000701", "amountPaid": 654.83}]
        self.assertEqual(ground_lines(lines, CELL_PER_LINE), 2)
        self.assertIn("line_not_grounded", lines[1]["warnings"])

    def test_skipped_row_cannot_lend_its_amount(self):
        # 10700000601 was not extracted; 10600000501 must not be grounded by that row's 654.83.
        lines = [{"proInvoiceNumber": "10600000501", "amountPaid": 654.83}]
        self.assertEqual(ground_lines(lines, CELL_PER_LINE), 1)

    def test_row_per_line_and_amount_before_id_on_the_same_line(self):
        markdown = "| 001234567 | 9/3/26 | $105.25 |\nPaid $20.00 on invoice 001234568\n"
        lines = [{"proInvoiceNumber": "001234567", "amountPaid": 105.25}, {"proInvoiceNumber": "001234568", "amountPaid": 20}]
        self.assertEqual(ground_lines(lines, markdown), 0)

    def test_hallucinated_pro_is_flagged_and_no_markdown_skips(self):
        lines = [{"proInvoiceNumber": "99999999999", "amountPaid": 654.83}]
        self.assertEqual(ground_lines(lines, CELL_PER_LINE), 1)
        self.assertEqual(ground_lines([{"proInvoiceNumber": "1", "amountPaid": 1}], ""), 0)


if __name__ == "__main__":
    unittest.main()