import csv
import io
import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from ledgerlab.core import Ledger, LedgerError, MAX_AMOUNT


class LedgerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "ledger.sqlite3"
        self.ledger = Ledger(self.path)

    def assert_balanced(self):
        state = self.ledger.state()
        transactions = {}
        cash = 0
        for entry in state["entries"]:
            transactions.setdefault(entry["transaction_id"], []).append(entry)
            cash += entry["amount_cents"] if entry["account"] == "cash" else 0
        for entries in transactions.values():
            self.assertEqual(len(entries), 2)
            self.assertEqual(sum(e["amount_cents"] for e in entries), 0)
            self.assertEqual(
                {e["account"] for e in entries}, {"cash", "merchant_payable"}
            )
        self.assertEqual(state["totals"]["ledger_balance_cents"], 0)
        self.assertEqual(cash, state["totals"]["net_cents"])

    def test_payment_and_multiple_refunds_conserve_money(self):
        payment = self.ledger.create_payment(1001, "Sensor kit", "create")["payment"]
        self.ledger.refund_payment(payment["id"], 300, "refund-a")
        last = self.ledger.refund_payment(payment["id"], 701, "refund-b")["payment"]
        self.assertEqual(
            (last["refunded_cents"], last["net_cents"], last["status"]),
            (1001, 0, "refunded"),
        )
        self.assert_balanced()

    def test_repeated_request_reuses_original_response(self):
        first = self.ledger.create_payment(1200, "Notebook", "same-key")
        second = self.ledger.create_payment(1200, "Notebook", "same-key")
        self.assertFalse(first["replayed"])
        self.assertTrue(second["replayed"])
        self.assertEqual(first["payment"], second["payment"])
        self.assertEqual(self.ledger.state()["totals"]["payment_count"], 1)

    def test_replay_is_original_snapshot_even_after_later_refund(self):
        first = self.ledger.create_payment(1200, "Notebook", "create")
        self.ledger.refund_payment(first["payment"]["id"], 200, "refund")
        replay = self.ledger.create_payment(1200, "Notebook", "create")
        self.assertEqual(replay["payment"], first["payment"])
        self.assertEqual(self.ledger.state()["payments"][0]["net_cents"], 1000)

    def test_reused_key_rejects_changed_payload_or_endpoint(self):
        first = self.ledger.create_payment(100, "Notebook", "key")
        for invoke in [
            lambda: self.ledger.create_payment(101, "Notebook", "key"),
            lambda: self.ledger.create_payment(100, "Changed", "key"),
            lambda: self.ledger.refund_payment(first["payment"]["id"], 100, "key"),
        ]:
            with self.assertRaises(LedgerError) as result:
                invoke()
            self.assertEqual(result.exception.code, "idempotency_conflict")
        self.assertEqual(len(self.ledger.state()["entries"]), 2)

    def test_concurrent_same_key_creates_one_payment(self):
        with ThreadPoolExecutor(max_workers=12) as pool:
            results = list(
                pool.map(
                    lambda _: self.ledger.create_payment(
                        2500, "Concurrent", "concurrent"
                    ),
                    range(24),
                )
            )
        self.assertEqual(sum(not r["replayed"] for r in results), 1)
        self.assertEqual(len({r["payment"]["id"] for r in results}), 1)
        self.assertEqual(len(self.ledger.state()["entries"]), 2)
        self.assert_balanced()

    def test_concurrent_refunds_cannot_exceed_payment(self):
        payment = self.ledger.create_payment(1000, "Concurrent refunds", "payment")[
            "payment"
        ]

        def refund(index):
            try:
                self.ledger.refund_payment(payment["id"], 600, f"refund-{index}")
                return "ok"
            except LedgerError as exc:
                return exc.code

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(refund, range(2)))
        self.assertCountEqual(results, ["ok", "refund_exceeds_balance"])
        self.assertEqual(self.ledger.state()["payments"][0]["net_cents"], 400)
        self.assert_balanced()

    def test_refund_replay_does_not_refund_twice(self):
        payment = self.ledger.create_payment(1000, "Notebook", "payment")["payment"]
        first = self.ledger.refund_payment(payment["id"], 300, "refund")
        replay = self.ledger.refund_payment(payment["id"], 300, "refund")
        self.assertEqual(first["refund"], replay["refund"])
        self.assertTrue(replay["replayed"])
        self.assertEqual(self.ledger.state()["totals"]["refund_cents"], 300)

    def test_failed_refund_leaves_no_partial_data(self):
        payment = self.ledger.create_payment(1000, "Notebook", "payment")["payment"]
        before = self.ledger.state()
        with self.assertRaises(LedgerError):
            self.ledger.refund_payment(payment["id"], 1001, "refund")
        self.assertEqual(before, self.ledger.state())
        self.assertFalse(
            self.ledger.refund_payment(payment["id"], 1000, "refund")["replayed"]
        )

    def test_internal_failure_rolls_back_payment_and_key(self):
        with patch.object(
            self.ledger, "journal", side_effect=RuntimeError("simulated disk failure")
        ):
            with self.assertRaises(RuntimeError):
                self.ledger.create_payment(100, "Rollback", "retry")
        self.assertEqual(self.ledger.state()["totals"]["payment_count"], 0)
        self.assertFalse(
            self.ledger.create_payment(100, "Rollback", "retry")["replayed"]
        )

    def test_database_survives_new_ledger_instance(self):
        first = self.ledger.create_payment(900, "Persisted", "saved")
        reopened = Ledger(self.path)
        self.assertEqual(reopened.state()["payments"][0]["id"], first["payment"]["id"])
        self.assertTrue(reopened.create_payment(900, "Persisted", "saved")["replayed"])

    def test_amount_validation_including_boolean_and_float(self):
        for value in [
            True,
            False,
            0,
            -1,
            1.0,
            "100",
            None,
            MAX_AMOUNT + 1,
            float("nan"),
        ]:
            with self.subTest(value=value), self.assertRaises(LedgerError):
                self.ledger.create_payment(value, "Invalid", "key")
        self.assertEqual(self.ledger.state()["totals"]["payment_count"], 0)

    def test_key_and_description_validation(self):
        for key in ["", None, "has space", "k" * 129]:
            with self.subTest(key=key), self.assertRaises(LedgerError):
                self.ledger.create_payment(100, "Description", key)
        for description in ["", None, " " * 3, "x" * 141, "a\nb", "\ud800"]:
            with self.subTest(description=description), self.assertRaises(LedgerError):
                self.ledger.create_payment(100, description, "key")

    def test_missing_payment(self):
        with self.assertRaises(LedgerError) as result:
            self.ledger.refund_payment("pay_missing", 100, "refund")
        self.assertEqual(result.exception.status, 404)

    def test_accounting_rows_are_immutable(self):
        payment = self.ledger.create_payment(1000, "Immutable", "pay")["payment"]
        self.ledger.refund_payment(payment["id"], 100, "ref")
        with self.ledger.connection() as db:
            for table in ["payments", "refunds", "entries"]:
                with self.subTest(table=table), self.assertRaises(
                    sqlite3.IntegrityError
                ):
                    db.execute(f"DELETE FROM {table}")
        self.assert_balanced()

    def test_seed_is_non_destructive_and_balanced(self):
        self.assertTrue(self.ledger.seed_demo()["seeded"])
        before = self.ledger.state()
        self.assertFalse(self.ledger.seed_demo()["seeded"])
        self.assertEqual(before, self.ledger.state())
        self.assertEqual(before["totals"]["payment_count"], 8)
        self.assertEqual(before["totals"]["refund_cents"], 4700)
        self.assert_balanced()

    def test_seed_does_not_mix_with_user_data(self):
        self.ledger.create_payment(500, "Existing", "existing")
        self.assertFalse(self.ledger.seed_demo()["seeded"])
        self.assertEqual(self.ledger.state()["totals"]["payment_count"], 1)

    def test_sample_csv_has_deliberate_findings(self):
        self.ledger.seed_demo()
        result = self.ledger.reconcile(self.ledger.state()["sample_csv"])
        self.assertEqual(
            result["summary"],
            {
                "matched": 6,
                "mismatched": 1,
                "missing": 1,
                "unknown": 0,
                "duplicate": 0,
                "invalid": 0,
            },
        )

    def test_reconciliation_handles_duplicates_unknown_and_invalid(self):
        payment = self.ledger.create_payment(1000, "Known", "known")["payment"]["id"]
        content = f"payment_id,net_cents,currency\n{payment},1000,USD\n{payment},1000,USD\nunknown,5,USD\nbad,1.5,USD\n"
        result = self.ledger.reconcile(content)
        self.assertEqual(
            result["summary"],
            {
                "matched": 1,
                "mismatched": 0,
                "missing": 0,
                "unknown": 1,
                "duplicate": 1,
                "invalid": 1,
            },
        )

    def test_invalid_currency_does_not_hide_missing_payment(self):
        payment = self.ledger.create_payment(1000, "Known", "known")["payment"]["id"]
        result = self.ledger.reconcile(
            f"payment_id,net_cents,currency\n{payment},1000,EUR\n"
        )
        self.assertEqual(result["summary"]["invalid"], 1)
        self.assertEqual(result["summary"]["missing"], 1)

    def test_csv_validation(self):
        for content in [
            "",
            None,
            "wrong,header\na,b",
            'payment_id,net_cents,currency\n"unterminated',
            "x" * 256001,
            "\ud800",
        ]:
            with self.subTest(content=str(content)[:40]), self.assertRaises(
                LedgerError
            ):
                self.ledger.reconcile(content)
        content = "payment_id,net_cents,currency\n" + "".join(
            f"x{i},1,USD\n" for i in range(2001)
        )
        with self.assertRaises(LedgerError):
            self.ledger.reconcile(content)

    def test_export_is_parseable_and_balanced(self):
        self.ledger.seed_demo()
        rows = list(csv.DictReader(io.StringIO(self.ledger.export_csv())))
        self.assertEqual(len(rows), 20)
        self.assertEqual(sum(int(row["amount_cents"]) for row in rows), 0)


if __name__ == "__main__":
    unittest.main()
