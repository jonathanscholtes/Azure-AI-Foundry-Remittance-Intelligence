import json
import os
import unittest
from unittest.mock import patch

from extraction import column_mapping
from extraction.spreadsheet_parser import parse_csv

AGENT_ENV = {"EXTRACTION_AGENT_NAME": "ach-extraction-expert", "FOUNDRY_PROJECT_ENDPOINT": "https://project.example"}


class FakeExpert:
    """Stands in for the ACH Extraction Expert: returns a fixed mapping and counts calls."""

    def __init__(self, **answer):
        self.answer = {"headerRow": 0, "identifierColumn": 0, "amountPaidColumn": 1, "tableKind": "payment_detail", "confidence": 0.9, **answer}
        self.calls = []

    def __call__(self, agent, prompt):
        self.calls.append((agent, json.loads(prompt)))
        return json.dumps(self.answer), []


class ColumnMappingTests(unittest.TestCase):
    def setUp(self):
        column_mapping._cache.clear()

    def _parse(self, content, expert):
        with patch.dict(os.environ, AGENT_ENV), patch.object(column_mapping, "invoke", expert):
            return parse_csv(content, "remit.csv")

    def test_without_the_agent_the_frozen_rules_apply(self):
        with patch.dict(os.environ, {"EXTRACTION_AGENT_NAME": ""}):
            mapping = column_mapping.resolve_columns([["PRO", "Amount Paid"], ["001234567", "10.00"]])
        self.assertEqual((mapping["source"], mapping["identifierColumn"], mapping["amountPaidColumn"]), ("rules", 0, 1))

    def test_agent_maps_a_header_the_rules_do_not_know(self):
        expert = FakeExpert(identifierColumn=1, amountPaidColumn=3)
        result = self._parse("Fecha,Factura,Importe adeudado,Importe pagado\n9/3/26,001234567,120.00,100.00\n".encode(), expert)
        self.assertEqual([(l["proInvoiceNumber"], l["amountPaid"]) for l in result["paymentLines"]], [("001234567", "100.00")])
        self.assertEqual(result["columnMapping"]["source"], "agent")
        self.assertEqual(expert.calls[0][0], "ach-extraction-expert")
        self.assertEqual(expert.calls[0][1]["rows"][0]["cells"][3], "Importe pagado")

    def test_agent_statement_verdict_contributes_no_lines(self):
        expert = FakeExpert(tableKind="statement_or_open_items", amountPaidColumn=None)
        result = self._parse(b"Pro#,Open Amt Due\n13600000805,2150\n", expert)
        self.assertEqual(result["paymentLines"], [])
        self.assertEqual(result["warnings"], ["spreadsheet_no_paid_amounts"])

    def test_out_of_range_answer_falls_back_to_rules(self):
        expert = FakeExpert(amountPaidColumn=9)
        result = self._parse(b"PRO,Amount Paid\n001234567,10.00\n", expert)
        self.assertEqual(result["columnMapping"]["source"], "rules")
        self.assertEqual(result["paymentLines"][0]["amountPaid"], "10.00")

    def test_disagreement_with_the_rules_is_recorded(self):
        # The rules pick "Amount Paid"; the expert picks "Net Paid" - the expert wins and it is flagged.
        expert = FakeExpert(amountPaidColumn=2)
        result = self._parse(b"PRO,Amount Paid,Net Paid\n001234567,10.00,9.50\n", expert)
        self.assertEqual(result["paymentLines"][0]["amountPaid"], "9.50")
        self.assertIn("column_mapping_differs_from_rules", result["warnings"])

    def test_identical_tables_call_the_agent_once(self):
        expert = FakeExpert()
        self._parse(b"PRO,Paid\n001234567,10.00\n", expert)
        self._parse(b"PRO,Paid\n001234567,10.00\n", expert)
        self.assertEqual(len(expert.calls), 1)

    def test_same_layout_with_different_values_reuses_one_answer(self):
        # Fabrikam: three sub-tables, same "Invoice / Amount" layout, different rows.
        expert = FakeExpert()
        with patch.dict(os.environ, AGENT_ENV), patch.object(column_mapping, "invoke", expert):
            column_mapping.map_columns([["Invoice", "Amount"], ["14700000903", "$ 789.81"], ["05900000401", "$ 1,191.91"]])
            column_mapping.map_columns([["Invoice", "Amount"], ["05900000401", "$ 250.00"]])
            column_mapping.map_columns([["Invoice", "Amount"], ["14700000902", "$ 1,915.40"]])
        self.assertEqual(len(expert.calls), 1)

    def test_a_changed_layout_is_asked_again(self):
        # Formats change month to month: renamed or added columns are a new layout, never a stale answer.
        expert = FakeExpert()
        with patch.dict(os.environ, AGENT_ENV), patch.object(column_mapping, "invoke", expert):
            column_mapping.map_columns([["Invoice", "Amount"], ["14700000903", "789.81"]])
            column_mapping.map_columns([["Invoice", "Amount Paid"], ["14700000903", "789.81"]])
            column_mapping.map_columns([["Invoice", "Date", "Amount"], ["14700000903", "9/3/26", "789.81"]])
        self.assertEqual(len(expert.calls), 3)

    def test_cached_answers_expire(self):
        expert = FakeExpert()
        rows = [["Invoice", "Amount"], ["14700000903", "789.81"]]
        with patch.dict(os.environ, AGENT_ENV), patch.object(column_mapping, "invoke", expert):
            column_mapping.map_columns(rows)
            with patch.object(column_mapping, "CACHE_TTL_SECONDS", -1):
                column_mapping.map_columns(rows)
        self.assertEqual(len(expert.calls), 2)

    def test_sheet_total_row_verifies_the_lines(self):
        expert = FakeExpert()
        ok = self._parse(b"PRO,Paid\n001234567,10.00\n001234568,5.00\nTotal,15.00\n", expert)
        self.assertTrue(ok["totalVerified"])
        bad = self._parse(b"PRO,Paid\n001234567,10.00\n001234568,5.00\nTotal,16.00\n", expert)
        self.assertIn("spreadsheet_total_mismatch", bad["warnings"])

    def test_disagreement_without_any_total_goes_to_review(self):
        from validation.remittance_validator import validate_and_merge

        expert = FakeExpert(amountPaidColumn=2)
        parsed = self._parse(b"PRO,Amount Paid,Net Paid\n001234567,10.00,9.50\n", expert)
        self.assertIn("column_mapping_unverified", parsed["warnings"])
        candidate = {"candidateId": "doc-001", "sourceType": "attachment", "score": 50,
                     "remittanceMetadata": {"payerName": "Acme", "remittanceDate": "2026-10-01"},
                     "paymentLines": parsed["paymentLines"], "warnings": parsed["warnings"]}
        no_total = validate_and_merge([candidate], payee_aliases=[])
        self.assertIn("column_mapping_needs_review", no_total["quality"]["warnings"])
        self.assertTrue(no_total["quality"]["requiresReview"])
        stated = {**candidate, "remittanceMetadata": {**candidate["remittanceMetadata"], "paymentTotal": 9.50}}
        reconciled = validate_and_merge([stated], payee_aliases=[])
        self.assertNotIn("column_mapping_needs_review", reconciled["quality"]["warnings"])

    def test_agent_failure_falls_back_to_rules(self):
        def broken(agent, prompt):
            raise TimeoutError()

        result = self._parse(b"PRO,Amount Paid\n001234567,10.00\n", broken)
        self.assertEqual(result["columnMapping"]["source"], "rules")


if __name__ == "__main__":
    unittest.main()
