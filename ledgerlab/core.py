"""Integer-cent accounting. Positive entries are debits, negative credits."""

from __future__ import annotations
import csv
import hashlib
import io
import json
import re
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

MAX_AMOUNT = 100_000_000
STATUSES = ("matched", "mismatched", "missing", "unknown", "duplicate", "invalid")


class LedgerError(Exception):
    def __init__(self, code, message, status=400):
        super().__init__(message)
        self.code, self.message, self.status = code, message, status


def timestamp():
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def amount(value):
    if type(value) is not int or not 1 <= value <= MAX_AMOUNT:
        raise LedgerError(
            "invalid_amount", f"amount_cents must be an integer from 1 to {MAX_AMOUNT}."
        )
    return value


class Ledger:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS payments (
                    id TEXT PRIMARY KEY, description TEXT NOT NULL,
                    amount_cents INTEGER NOT NULL CHECK(typeof(amount_cents)='integer' AND amount_cents>0),
                    currency TEXT NOT NULL DEFAULT 'USD' CHECK(currency='USD'), created_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS refunds (
                    id TEXT PRIMARY KEY, payment_id TEXT NOT NULL REFERENCES payments(id),
                    amount_cents INTEGER NOT NULL CHECK(typeof(amount_cents)='integer' AND amount_cents>0), created_at TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS refunds_payment ON refunds(payment_id);
                CREATE TABLE IF NOT EXISTS entries (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, transaction_id TEXT NOT NULL,
                    payment_id TEXT NOT NULL REFERENCES payments(id),
                    account TEXT NOT NULL CHECK(account IN ('cash','merchant_payable')),
                    amount_cents INTEGER NOT NULL CHECK(typeof(amount_cents)='integer' AND amount_cents<>0),
                    created_at TEXT NOT NULL, UNIQUE(transaction_id,account));
                CREATE TABLE IF NOT EXISTS idempotency (
                    key TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, response TEXT NOT NULL, created_at TEXT NOT NULL);
            """)
            for table in ("payments", "refunds", "entries"):
                for operation in ("UPDATE", "DELETE"):
                    # Identifiers come only from the constant tuples above.
                    db.execute(
                        f"CREATE TRIGGER IF NOT EXISTS {table}_no_{operation.lower()} BEFORE {operation} ON {table} BEGIN SELECT RAISE(ABORT, 'Accounting records are immutable'); END"
                    )

    @contextmanager
    def connection(self):
        db = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("PRAGMA busy_timeout=10000")
        try:
            yield db
        finally:
            db.close()

    @staticmethod
    def payment(db, payment_id):
        row = db.execute(
            """SELECT p.*, COALESCE(SUM(r.amount_cents),0) AS refunded_cents
            FROM payments p LEFT JOIN refunds r ON p.id=r.payment_id WHERE p.id=? GROUP BY p.id""",
            (payment_id,),
        ).fetchone()
        if row is None:
            raise LedgerError("payment_not_found", "That payment does not exist.", 404)
        result = dict(row)
        result["net_cents"] = result["amount_cents"] - result["refunded_cents"]
        result["status"] = (
            "refunded"
            if result["net_cents"] == 0
            else "partially_refunded" if result["refunded_cents"] else "captured"
        )
        return result

    @staticmethod
    def journal(db, transaction_id, payment_id, cents, created):
        db.executemany(
            "INSERT INTO entries(transaction_id,payment_id,account,amount_cents,created_at) VALUES (?,?,?,?,?)",
            [
                (transaction_id, payment_id, "cash", cents, created),
                (transaction_id, payment_id, "merchant_payable", -cents, created),
            ],
        )
        balance = db.execute(
            "SELECT SUM(amount_cents) FROM entries WHERE transaction_id=?",
            (transaction_id,),
        ).fetchone()[0]
        if balance != 0:
            raise RuntimeError("The transaction is not balanced.")

    def mutate(self, key, payload, action):
        if not isinstance(key, str) or not re.fullmatch(r"[A-Za-z0-9._:-]{1,128}", key):
            raise LedgerError(
                "invalid_idempotency_key",
                "Supply an Idempotency-Key with 1-128 letters, digits, dots, underscores, colons, or hyphens.",
            )
        fingerprint = hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        with self.connection() as db:
            # Lock before checking the key or refund balance, not after.
            db.execute("BEGIN IMMEDIATE")
            try:
                existing = db.execute(
                    "SELECT fingerprint,response FROM idempotency WHERE key=?", (key,)
                ).fetchone()
                if existing:
                    if existing["fingerprint"] != fingerprint:
                        raise LedgerError(
                            "idempotency_conflict",
                            "This key belongs to a different request. Use a new key for a new operation.",
                            409,
                        )
                    result = json.loads(existing["response"])
                    result["replayed"] = True
                else:
                    result = action(db)
                    result["replayed"] = False
                    db.execute(
                        "INSERT INTO idempotency VALUES (?,?,?,?)",
                        (key, fingerprint, json.dumps(result), timestamp()),
                    )
                db.commit()
                return result
            except BaseException:
                db.rollback()
                raise

    def create_payment(self, amount_cents, description, key):
        cents = amount(amount_cents)
        if not isinstance(description, str) or not 1 <= len(description.strip()) <= 140:
            raise LedgerError(
                "invalid_description", "description must contain 1-140 characters."
            )
        description = description.strip()
        try:
            description.encode("utf-8")
        except UnicodeError as exc:
            raise LedgerError(
                "invalid_description", "description must contain valid Unicode text."
            ) from exc
        if any(ord(ch) < 32 for ch in description):
            raise LedgerError(
                "invalid_description",
                "description must be a single line without control characters.",
            )

        def action(db):
            payment_id, created = "pay_" + uuid.uuid4().hex[:16], timestamp()
            db.execute(
                "INSERT INTO payments(id,description,amount_cents,created_at) VALUES (?,?,?,?)",
                (payment_id, description, cents, created),
            )
            self.journal(db, "txn_" + uuid.uuid4().hex[:16], payment_id, cents, created)
            return {"payment": self.payment(db, payment_id)}

        return self.mutate(
            key,
            {"operation": "payment", "amount_cents": cents, "description": description},
            action,
        )

    def refund_payment(self, payment_id, amount_cents, key):
        cents = amount(amount_cents)
        if not isinstance(payment_id, str) or not re.fullmatch(
            r"[A-Za-z0-9_-]{1,128}", payment_id
        ):
            raise LedgerError("invalid_payment_id", "Supply a valid payment ID.")

        def action(db):
            payment = self.payment(db, payment_id)
            if cents > payment["net_cents"]:
                raise LedgerError(
                    "refund_exceeds_balance",
                    f"Only {payment['net_cents']} cents remain refundable.",
                    409,
                )
            refund_id, created = "ref_" + uuid.uuid4().hex[:16], timestamp()
            db.execute(
                "INSERT INTO refunds VALUES (?,?,?,?)",
                (refund_id, payment_id, cents, created),
            )
            self.journal(
                db, "txn_" + uuid.uuid4().hex[:16], payment_id, -cents, created
            )
            return {
                "payment": self.payment(db, payment_id),
                "refund": {"id": refund_id, "amount_cents": cents},
            }

        return self.mutate(
            key,
            {"operation": "refund", "payment_id": payment_id, "amount_cents": cents},
            action,
        )

    def state(self):
        with self.connection() as db:
            db.execute("BEGIN")
            ids = [
                row[0]
                for row in db.execute(
                    "SELECT id FROM payments ORDER BY created_at DESC,id"
                )
            ]
            payments = [self.payment(db, payment_id) for payment_id in ids]
            entries = [
                dict(row)
                for row in db.execute("SELECT * FROM entries ORDER BY id DESC")
            ]
            db.commit()
        totals = {
            "gross_cents": sum(p["amount_cents"] for p in payments),
            "refund_cents": sum(p["refunded_cents"] for p in payments),
            "net_cents": sum(p["net_cents"] for p in payments),
            "payment_count": len(payments),
            "ledger_balance_cents": sum(e["amount_cents"] for e in entries),
        }
        output = io.StringIO(newline="")
        writer = csv.writer(output, lineterminator="\n")
        writer.writerow(["payment_id", "net_cents", "currency"])
        # Deliberately omit one payment and alter another for the demo.
        for index, payment in enumerate(payments[:-1]):
            writer.writerow(
                [
                    payment["id"],
                    payment["net_cents"] + (125 if index == 1 else 0),
                    "USD",
                ]
            )
        return {
            "payments": payments,
            "entries": entries,
            "totals": totals,
            "sample_csv": output.getvalue(),
        }

    def seed_demo(self):
        dataset = [
            ("pay_demo_001", "Synthetic / Atlas notebook", 2450),
            ("pay_demo_002", "Synthetic / Circuit workshop", 12900),
            ("pay_demo_003", "Synthetic / Sensor kit", 8750),
            ("pay_demo_004", "Synthetic / Robotics handbook", 3200),
            ("pay_demo_005", "Synthetic / Field notes", 1800),
            ("pay_demo_006", "Synthetic / Maker membership", 4900),
            ("pay_demo_007", "Synthetic / Lab supplies", 15375),
            ("pay_demo_008", "Synthetic / Weekend workshop", 9950),
        ]
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                if db.execute("SELECT 1 FROM payments LIMIT 1").fetchone():
                    db.commit()
                    return {
                        "seeded": False,
                        "message": "The ledger already contains payments; no data was changed.",
                    }
                for index, (payment_id, description, cents) in enumerate(dataset):
                    created = f"2026-11-01T10:{index:02d}:00.000Z"
                    db.execute(
                        "INSERT INTO payments(id,description,amount_cents,created_at) VALUES (?,?,?,?)",
                        (payment_id, description, cents, created),
                    )
                    self.journal(
                        db, f"txn_demo_{index+1:03d}", payment_id, cents, created
                    )
                for index, (payment_id, cents) in enumerate(
                    [("pay_demo_002", 2900), ("pay_demo_005", 1800)]
                ):
                    created = f"2026-11-01T11:{index:02d}:00.000Z"
                    db.execute(
                        "INSERT INTO refunds VALUES (?,?,?,?)",
                        (f"ref_demo_{index+1:03d}", payment_id, cents, created),
                    )
                    self.journal(
                        db,
                        f"txn_demo_refund_{index+1:03d}",
                        payment_id,
                        -cents,
                        created,
                    )
                db.commit()
            except BaseException:
                db.rollback()
                raise
        return {"seeded": True, "message": "Loaded 8 synthetic payments and 2 refunds."}

    def reconcile(self, content):
        if not isinstance(content, str) or not content.strip():
            raise LedgerError(
                "invalid_csv",
                "Provide CSV text with payment_id,net_cents,currency columns.",
            )
        try:
            encoded = content.encode("utf-8")
        except UnicodeError as exc:
            raise LedgerError(
                "invalid_csv", "CSV text must contain valid Unicode."
            ) from exc
        if len(encoded) > 256_000:
            raise LedgerError(
                "csv_too_large", "CSV text must be at most 256000 bytes.", 413
            )
        try:
            reader = csv.DictReader(
                io.StringIO(content.lstrip("\ufeff"), newline=""), strict=True
            )
            if reader.fieldnames != ["payment_id", "net_cents", "currency"]:
                raise LedgerError(
                    "invalid_csv_header",
                    "Use exactly these columns in order: payment_id,net_cents,currency.",
                )
            records = []
            for record in reader:
                records.append(record)
                if len(records) > 2000:
                    raise LedgerError(
                        "too_many_csv_rows",
                        "CSV files may contain at most 2000 data rows.",
                        413,
                    )
        except csv.Error as exc:
            raise LedgerError("invalid_csv", f"Could not parse CSV: {exc}.") from exc
        expected = {p["id"]: p["net_cents"] for p in self.state()["payments"]}
        seen, recognized, rows = set(), set(), []
        counts = {status: 0 for status in STATUSES}

        def append(payment_id, status, actual, detail):
            value = expected.get(payment_id)
            rows.append(
                {
                    "payment_id": payment_id,
                    "status": status,
                    "expected_cents": value,
                    "actual_cents": actual,
                    "difference_cents": (
                        None if actual is None or value is None else actual - value
                    ),
                    "detail": detail,
                }
            )
            counts[status] += 1

        for number, record in enumerate(records, start=2):
            payment_id = (record.get("payment_id") or "").strip()
            raw = (record.get("net_cents") or "").strip()
            currency = (record.get("currency") or "").strip()
            actual = (
                int(raw)
                if len(raw) <= 12 and re.fullmatch(r"0|[1-9][0-9]*", raw)
                else None
            )
            if (
                None in record
                or None in record.values()
                or not payment_id
                or len(payment_id) > 128
                or actual is None
                or actual > MAX_AMOUNT
                or currency != "USD"
            ):
                append(
                    payment_id,
                    "invalid",
                    actual,
                    f"Row {number}: use a payment ID, non-negative integer cents up to {MAX_AMOUNT}, and USD, with exactly three fields.",
                )
                continue
            if payment_id in seen:
                append(
                    payment_id,
                    "duplicate",
                    actual,
                    f"Row {number}: payment ID appears more than once; duplicate rows are not summed.",
                )
                continue
            seen.add(payment_id)
            if payment_id not in expected:
                append(
                    payment_id,
                    "unknown",
                    actual,
                    f"Row {number}: no local payment has this ID.",
                )
                continue
            recognized.add(payment_id)
            if actual == expected[payment_id]:
                append(
                    payment_id,
                    "matched",
                    actual,
                    "Settlement matches the payment's current net amount.",
                )
            else:
                append(
                    payment_id,
                    "mismatched",
                    actual,
                    "Settlement differs from the payment's current net amount.",
                )
        for payment_id in expected.keys() - recognized:
            append(
                payment_id,
                "missing",
                None,
                "No valid settlement row was supplied for this local payment.",
            )
        rows.sort(key=lambda row: (row["status"] == "missing", row["payment_id"]))
        return {"rows": rows, "summary": counts}

    def export_csv(self):
        output = io.StringIO(newline="")
        writer = csv.writer(output, lineterminator="\n")
        writer.writerow(
            [
                "entry_id",
                "transaction_id",
                "payment_id",
                "account",
                "amount_cents",
                "created_at",
            ]
        )
        for entry in reversed(self.state()["entries"]):
            writer.writerow(
                [
                    entry["id"],
                    entry["transaction_id"],
                    entry["payment_id"],
                    entry["account"],
                    entry["amount_cents"],
                    entry["created_at"],
                ]
            )
        return output.getvalue()
