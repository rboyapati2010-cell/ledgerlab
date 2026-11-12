import http.client
import json
import tempfile
import threading
import unittest
from pathlib import Path

from ledgerlab.core import Ledger
from ledgerlab.server import LedgerServer


class HttpTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.root = Path(cls.temp.name)
        cls.web = cls.root / "web"
        cls.web.mkdir()
        (cls.web / "index.html").write_text("<h1>LedgerLab test</h1>")
        cls.server = LedgerServer(
            ("127.0.0.1", 0), Ledger(cls.root / "ledger.db"), cls.web
        )
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2)
        cls.temp.cleanup()

    def request(self, method, path, body=None, headers=None):
        connection = http.client.HTTPConnection(
            "127.0.0.1", self.server.server_port, timeout=3
        )
        data = None if body is None else json.dumps(body)
        outgoing = {"Content-Type": "application/json"} if body is not None else {}
        outgoing.update(headers or {})
        try:
            connection.request(method, path, data, outgoing)
            response = connection.getresponse()
            status, received, payload = (
                response.status,
                dict(response.getheaders()),
                response.read(),
            )
            return (
                status,
                received,
                (
                    json.loads(payload)
                    if received.get("Content-Type", "").startswith("application/json")
                    else payload.decode()
                ),
            )
        finally:
            connection.close()

    def test_static_page_and_security_headers(self):
        status, headers, body = self.request("GET", "/")
        self.assertEqual(status, 200)
        self.assertIn("LedgerLab test", body)
        self.assertIn("script-src 'self'", headers["Content-Security-Policy"])
        self.assertEqual(headers["X-Content-Type-Options"], "nosniff")

    def test_http_payment_replay_refund_and_export(self):
        body = {"amount_cents": 2345, "description": "HTTP notebook"}
        headers = {"Idempotency-Key": "http-create"}
        status, _, result = self.request("POST", "/api/payments", body, headers)
        self.assertEqual(status, 201)
        payment_id = result["payment"]["id"]
        status, _, replay = self.request("POST", "/api/payments", body, headers)
        self.assertEqual(status, 200)
        self.assertTrue(replay["replayed"])
        status, _, refund = self.request(
            "POST",
            f"/api/payments/{payment_id}/refunds",
            {"amount_cents": 345},
            {"Idempotency-Key": "http-refund"},
        )
        self.assertEqual(status, 201)
        self.assertEqual(refund["payment"]["net_cents"], 2000)
        status, _, result = self.request(
            "POST",
            "/api/reconcile",
            {"csv": f"payment_id,net_cents,currency\n{payment_id},2000,USD\n"},
        )
        self.assertEqual(status, 200)
        self.assertEqual(result["summary"]["matched"], 1)
        status, headers, body = self.request("GET", "/api/export")
        self.assertEqual(status, 200)
        self.assertIn("attachment;", headers["Content-Disposition"])
        self.assertIn(payment_id, body)

    def test_errors_are_structured(self):
        status, _, body = self.request(
            "POST", "/api/payments", {"amount_cents": 100, "description": "Missing key"}
        )
        self.assertEqual(status, 400)
        self.assertEqual(body["error"], "invalid_idempotency_key")
        self.assertIn("message", body)

    def test_rejects_unexpected_fields_and_boolean_amount(self):
        for body in [
            {"amount_cents": True, "description": "Bad"},
            {"amount_cents": 100, "description": "Bad", "currency": "EUR"},
        ]:
            with self.subTest(body=body):
                status, _, _ = self.request(
                    "POST", "/api/payments", body, {"Idempotency-Key": "http-invalid"}
                )
                self.assertEqual(status, 400)

    def test_rejects_unrelated_browser_origin(self):
        status, _, body = self.request(
            "POST", "/api/demo/seed", {}, {"Origin": "https://example.invalid"}
        )
        self.assertEqual(status, 403)
        self.assertEqual(body["error"], "invalid_origin")

    def test_rejects_wrong_host(self):
        status, _, body = self.request(
            "GET", "/api/state", headers={"Host": "example.invalid"}
        )
        self.assertEqual(status, 403)
        self.assertEqual(body["error"], "invalid_host")

    def test_unknown_route_returns_json(self):
        status, _, body = self.request("GET", "/api/missing")
        self.assertEqual(status, 404)
        self.assertEqual(body["error"], "not_found")

    def test_non_json_payload_type_rejected(self):
        status, _, body = self.request(
            "POST", "/api/demo/seed", {}, {"Content-Type": "text/plain"}
        )
        self.assertEqual(status, 415)
        self.assertEqual(body["error"], "unsupported_media_type")

    def test_seed_endpoint_never_wipes_data(self):
        self.request(
            "POST",
            "/api/payments",
            {"amount_cents": 100, "description": "Preserve me"},
            {"Idempotency-Key": "http-preserve"},
        )
        _, _, before = self.request("GET", "/api/state")
        status, _, result = self.request("POST", "/api/demo/seed", {})
        self.assertEqual(status, 200)
        self.assertFalse(result["seeded"])
        _, _, after = self.request("GET", "/api/state")
        self.assertEqual(before, after)


if __name__ == "__main__":
    unittest.main()
