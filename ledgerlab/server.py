"""Loopback-only HTTP adapter. Run: python3 -m ledgerlab.server --port 8011"""

from __future__ import annotations
import argparse
import json
import logging
import mimetypes
import sqlite3
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlsplit
from .core import Ledger, LedgerError

ROOT = Path(__file__).resolve().parents[1]
MAX_BODY = 300_000


class LedgerServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, ledger, web_root=None):
        self.ledger = ledger
        self.web_root = (web_root or ROOT / "web").resolve()
        super().__init__(address, Handler)


class Handler(BaseHTTPRequestHandler):
    server_version = "LedgerLab/0.1"

    def setup(self):
        super().setup()
        self.connection.settimeout(10)

    def log_message(self, format, *args):
        logging.info("%s %s", self.address_string(), format % args)

    def send_data(self, status, data, content_type, headers=None):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; font-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'",
        )
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(data)

    def json(self, status, value):
        self.send_data(
            status,
            json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8"),
            "application/json; charset=utf-8",
        )

    def check_origin(self):
        # Binding locally alone does not stop unrelated websites from sending
        # browser requests to localhost. Check Host and browser Origin too.
        allowed = {
            f"127.0.0.1:{self.server.server_port}",
            f"localhost:{self.server.server_port}",
        }
        if self.headers.get("Host") not in allowed:
            raise LedgerError(
                "invalid_host", "Open the demo using its localhost URL.", 403
            )
        origin = self.headers.get("Origin")
        if origin and origin not in {f"http://{host}" for host in allowed}:
            raise LedgerError(
                "invalid_origin", "Cross-origin requests are not accepted.", 403
            )
        if self.headers.get("Sec-Fetch-Site") == "cross-site":
            raise LedgerError(
                "invalid_origin", "Cross-site requests are not accepted.", 403
            )

    def body(self):
        if self.headers.get("Transfer-Encoding"):
            raise LedgerError("invalid_body", "Chunked requests are not supported.")
        if (
            self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
            != "application/json"
        ):
            raise LedgerError(
                "unsupported_media_type", "Send Content-Type: application/json.", 415
            )
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError as exc:
            raise LedgerError(
                "invalid_body", "Content-Length must be an integer."
            ) from exc
        if not 0 <= length <= MAX_BODY:
            raise LedgerError(
                "body_too_large",
                f"Request bodies must be at most {MAX_BODY} bytes.",
                413,
            )

        def reject_constant(value):
            raise ValueError("Non-finite JSON number")

        def unique_keys(pairs):
            result = {}
            for key, value in pairs:
                if key in result:
                    raise ValueError("Duplicate JSON key")
                result[key] = value
            return result

        try:
            body = json.loads(
                self.rfile.read(length),
                parse_constant=reject_constant,
                object_pairs_hook=unique_keys,
            )
        except (ValueError, UnicodeDecodeError) as exc:
            raise LedgerError(
                "invalid_json",
                "Send a valid JSON object without duplicate keys or non-finite numbers.",
            ) from exc
        if not isinstance(body, dict):
            raise LedgerError("invalid_json", "Request body must be a JSON object.")
        return body

    @staticmethod
    def fields(body, required):
        if set(body) != required:
            raise LedgerError(
                "invalid_fields",
                "Expected exactly these fields: " + ", ".join(sorted(required)) + ".",
            )

    def do_GET(self):
        try:
            self.check_origin()
            path = urlsplit(self.path).path
            if path == "/api/state":
                self.json(200, self.server.ledger.state())
            elif path == "/api/export":
                self.send_data(
                    200,
                    self.server.ledger.export_csv().encode(),
                    "text/csv; charset=utf-8",
                    {
                        "Content-Disposition": 'attachment; filename="ledgerlab-journal.csv"'
                    },
                )
            elif path == "/api/health":
                self.json(200, {"status": "ok", "mode": "local-demo"})
            elif path.startswith("/api/"):
                raise LedgerError("not_found", "API route not found.", 404)
            else:
                requested = "index.html" if path == "/" else unquote(path).lstrip("/")
                target = (self.server.web_root / requested).resolve()
                if (
                    not target.is_relative_to(self.server.web_root)
                    or not target.is_file()
                ):
                    raise LedgerError("not_found", "Page not found.", 404)
                kind = (
                    mimetypes.guess_type(target.name)[0] or "application/octet-stream"
                )
                if kind.startswith("text/") or kind in {
                    "application/javascript",
                    "application/json",
                }:
                    kind += "; charset=utf-8"
                self.send_data(200, target.read_bytes(), kind)
        except LedgerError as exc:
            self.json(exc.status, {"error": exc.code, "message": exc.message})
        except (sqlite3.Error, OSError):
            logging.exception("Local read failed")
            self.json(
                500,
                {
                    "error": "storage_error",
                    "message": "Could not read local data. Check the server terminal and database permissions.",
                },
            )

    def do_POST(self):
        try:
            self.check_origin()
            body = self.body()
            path = urlsplit(self.path).path
            key = self.headers.get("Idempotency-Key", "")
            if path == "/api/payments":
                self.fields(body, {"amount_cents", "description"})
                result = self.server.ledger.create_payment(
                    body["amount_cents"], body["description"], key
                )
                self.json(200 if result["replayed"] else 201, result)
            elif path.startswith("/api/payments/") and path.endswith("/refunds"):
                parts = path.split("/")
                if len(parts) != 5:
                    raise LedgerError("not_found", "API route not found.", 404)
                self.fields(body, {"amount_cents"})
                result = self.server.ledger.refund_payment(
                    unquote(parts[3]), body["amount_cents"], key
                )
                self.json(200 if result["replayed"] else 201, result)
            elif path == "/api/reconcile":
                self.fields(body, {"csv"})
                self.json(200, self.server.ledger.reconcile(body["csv"]))
            elif path == "/api/demo/seed":
                self.fields(body, set())
                self.json(200, self.server.ledger.seed_demo())
            else:
                raise LedgerError("not_found", "API route not found.", 404)
        except LedgerError as exc:
            self.json(exc.status, {"error": exc.code, "message": exc.message})
        except (sqlite3.Error, OSError, RuntimeError):
            logging.exception("Local mutation failed")
            self.json(
                500,
                {
                    "error": "storage_error",
                    "message": "The operation could not be saved. Retry it with the same idempotency key.",
                },
            )

    def do_OPTIONS(self):
        self.json(
            405,
            {
                "error": "method_not_allowed",
                "message": "Cross-origin access is not supported.",
            },
        )


def main():
    parser = argparse.ArgumentParser(
        description="Local LedgerLab demo. No real money is processed."
    )
    parser.add_argument("--port", type=int, default=8011)
    parser.add_argument("--db", type=Path, default=ROOT / "data" / "ledger.sqlite3")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    server = LedgerServer(("127.0.0.1", args.port), Ledger(args.db))
    print(
        f"LedgerLab: http://127.0.0.1:{server.server_port} (local synthetic-money demo)",
        flush=True,
    )
    print(f"Database: {args.db.resolve()}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
