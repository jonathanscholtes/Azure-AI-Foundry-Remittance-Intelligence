import copy
import os
import unittest
from unittest.mock import patch

from classification import intake_decision

ENV = {"INTAKE_DECISION_DEPLOYMENT": "intake-decision", "CONTENT_UNDERSTANDING_ENDPOINT": "https://foundry.example"}


def _source(cid, name, lines, source_type="attachment", strategy="content_understanding"):
    return {
        "candidateId": cid,
        "name": name,
        "strategy": strategy,
        "excerpt": f"text of {name}",
        "normalized": {"sourceType": source_type, "paymentLines": [{"proInvoiceNumber": p, "amountPaid": a} for p, a in lines]},
    }


# Shaped like a forwarded multi-transfer thread: an embedded email listing three transfers, a transfer-proof PDF
# whose only line has no PRO, and the clerk's question in the body.
MULTI_TRANSFER = [
    _source("doc-003", "embedded-email-3.txt", [("13600000802", 2150.0), ("13600000803", 2150.0), ("13600000804", 2150.0)], "embedded_message_body"),
    _source("doc-011", "TRANSFER SEPT 11 2150.00.pdf", [(None, 2150.0)]),
    _source("message-body", "message-body.txt", [("13600000802", 2150.0), ("13600000805", 2150.0)], "message_body"),
]


def _answers(roles, thread="dispute_or_inquiry", probability=0.9):
    answers = {f"role_{i}": {"type": "choice", "choice": role, "probabilities": {role: probability}} for i, role in enumerate(roles)}
    answers["thread_kind"] = {"type": "choice", "choice": thread, "probabilities": {thread: probability}}
    return {"model": "microsoft-decision-1", "answers": answers}


class IntakeDecisionTests(unittest.TestCase):
    def test_single_cu_source_never_calls_decision(self):
        calls = []
        one = [MULTI_TRANSFER[0], _source("doc-002", "sheet.xlsx", [("1", 1.0)], strategy="deterministic_parser")]
        with patch.dict(os.environ, ENV), patch.object(intake_decision, "call", lambda body: calls.append(body)):
            self.assertIsNone(intake_decision.classify(one))
        self.assertEqual(calls, [])

    def test_one_call_asks_every_ambiguous_source_and_the_thread(self):
        seen = {}

        def fake(body):
            seen.update(body)
            return _answers(["payment_detail", "payment_proof", "correspondence"])

        with patch.dict(os.environ, ENV), patch.object(intake_decision, "call", fake):
            result = intake_decision.classify(copy.deepcopy(MULTI_TRANSFER), subject="RV: Pago")
        self.assertEqual(set(seen["questions"]), {"role_0", "role_1", "role_2", "thread_kind"})
        self.assertEqual([s["id"] for s in seen["state"]["sources"]], ["doc-003", "doc-011", "message-body"])
        self.assertEqual(result["roles"]["doc-011"], {"role": "payment_proof", "probability": 0.9})

    def test_multi_transfer_keeps_the_three_transfers(self):
        extractions = copy.deepcopy(MULTI_TRANSFER)
        with patch.dict(os.environ, ENV), patch.object(intake_decision, "call", lambda body: _answers(["payment_detail", "payment_proof", "correspondence"])):
            record = intake_decision.apply(extractions, intake_decision.classify(extractions))
        kept = [(l["proInvoiceNumber"], l["amountPaid"]) for e in extractions for l in e["normalized"]["paymentLines"]]
        self.assertEqual(kept, [("13600000802", 2150.0), ("13600000803", 2150.0), ("13600000804", 2150.0)])
        reasons = sorted(e["reason"] for e in record["excluded"])
        self.assertEqual(reasons, ["correspondence", "correspondence", "payment_proof_corroborates"])
        self.assertEqual(record["threadKind"], "dispute_or_inquiry")

    def test_proof_lines_are_evidence_when_detail_exists(self):
        # CU read the customer account number off the bank receipt as a "PRO".
        extractions = copy.deepcopy(MULTI_TRANSFER) + [_source("doc-008", "TRANSFER SEPT 09 2150.00.pdf", [("13200008", 2150.0)])]
        with patch.dict(os.environ, ENV), patch.object(intake_decision, "call", lambda body: _answers(["payment_detail", "payment_proof", "correspondence", "payment_proof"])):
            intake_decision.apply(extractions, intake_decision.classify(extractions))
        kept = {l["proInvoiceNumber"] for e in extractions for l in e["normalized"]["paymentLines"]}
        self.assertEqual(kept, {"13600000802", "13600000803", "13600000804"})

    def test_proof_lines_count_when_there_is_no_detail(self):
        extractions = [_source("doc-001", "transfer.pdf", [("001234567", 50.0)]), _source("message-body", "body.txt", [("001234567", 50.0)], "message_body")]
        with patch.dict(os.environ, ENV), patch.object(intake_decision, "call", lambda body: _answers(["payment_proof", "correspondence"])):
            intake_decision.apply(extractions, intake_decision.classify(extractions))
        self.assertEqual(extractions[0]["normalized"]["paymentLines"][0]["proInvoiceNumber"], "001234567")

    def test_low_confidence_changes_nothing(self):
        extractions = copy.deepcopy(MULTI_TRANSFER)
        with patch.dict(os.environ, ENV), patch.object(intake_decision, "call", lambda body: _answers(["payment_detail", "payment_proof", "correspondence"], probability=0.5)):
            record = intake_decision.apply(extractions, intake_decision.classify(extractions))
        self.assertFalse(record["applied"])
        self.assertIsNone(record["threadKind"])
        self.assertEqual(sum(len(e["normalized"]["paymentLines"]) for e in extractions), 6)

    def test_never_excludes_every_line(self):
        extractions = copy.deepcopy(MULTI_TRANSFER)
        with patch.dict(os.environ, ENV), patch.object(intake_decision, "call", lambda body: _answers(["correspondence", "other", "statement"])):
            record = intake_decision.apply(extractions, intake_decision.classify(extractions))
        self.assertFalse(record["applied"])
        self.assertEqual(sum(len(e["normalized"]["paymentLines"]) for e in extractions), 6)

    def test_failure_or_missing_deployment_is_fail_safe(self):
        def broken(body):
            raise TimeoutError()

        extractions = copy.deepcopy(MULTI_TRANSFER)
        with patch.dict(os.environ, ENV), patch.object(intake_decision, "call", broken):
            record = intake_decision.apply(extractions, intake_decision.classify(extractions))
        self.assertEqual(record["status"], "failed")
        self.assertFalse(record["applied"])
        with patch.dict(os.environ, {"INTAKE_DECISION_DEPLOYMENT": ""}):
            self.assertIsNone(intake_decision.classify(copy.deepcopy(MULTI_TRANSFER)))

    def test_probability_shapes_are_tolerated(self):
        self.assertEqual(intake_decision._choice({"choice": "a", "probabilities": {"a": 0.8}}), ("a", 0.8))
        self.assertEqual(intake_decision._choice({"choice": "a", "probabilities": [{"option": "a", "probability": 0.7}]}), ("a", 0.7))
        self.assertEqual(intake_decision._choice({"choice": "a", "probability": 0.6}), ("a", 0.6))


class TransferPaymentsTests(unittest.TestCase):
    """Several transfers in one thread, recognized from the transfer proofs Decision-1 identified."""

    @staticmethod
    def _proof(cid, date, total):
        return {"candidateId": cid, "normalized": {"remittanceMetadata": {"remittanceDate": date, "paymentTotal": total}}}

    def _groups(self, proofs, lines_total):
        from orchestration.activities import _transfer_payments

        roles = {p["candidateId"]: {"role": "payment_proof"} for p in proofs}
        return _transfer_payments(proofs, {"roles": roles}, {"calculatedPaymentTotal": lines_total})

    def test_one_payment_per_transfer_date_when_they_add_up_to_the_lines(self):
        proofs = [self._proof("doc-008", "2026-09-09", 2150), self._proof("doc-011", "2026-09-11", 2150),
                  self._proof("doc-009", "2026-09-11", 2150), self._proof("doc-012", "2026-09-24", 2150)]
        groups = self._groups(proofs, 6450.0)
        self.assertEqual([g["label"] for g in groups], ["Transfer 2026-09-09", "Transfer 2026-09-11", "Transfer 2026-09-24"])
        self.assertEqual(groups[1]["sources"], ["doc-011", "doc-009"])

    def test_no_payments_when_transfers_do_not_explain_the_lines(self):
        proofs = [self._proof("doc-008", "2026-09-09", 2150), self._proof("doc-011", "2026-09-11", 2150)]
        self.assertEqual(self._groups(proofs, 6450.0), [])  # a third payment has no proof
        self.assertEqual(self._groups(proofs[:1], 2150.0), [])  # one transfer is one payment
        same_date = [self._proof("doc-008", "2026-09-09", 2150), self._proof("doc-011", "2026-09-09", 900)]
        self.assertEqual(self._groups(same_date, 3050.0), [])


if __name__ == "__main__":
    unittest.main()
