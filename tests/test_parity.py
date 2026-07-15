"""Unit tests for the parity additions: migrator, scope/auth, crypto,
and the new client methods (vault writes, metrics, register_external_app)."""

import base64
import json
import os
import tempfile
import threading
import unittest
from http.server import HTTPServer, BaseHTTPRequestHandler

from localitas_client import LocalitasClient, AUTOMATION_RUN_ID_HEADER
from localitas_client.migrator import _split_sql, _parse_filename
from localitas_client import scope


class TestMigratorParsing(unittest.TestCase):
    def test_parse_timestamp_filename(self):
        version, name = _parse_filename("20260101-120000-000-init.sql")
        self.assertEqual(version, "20260101-120000-000")
        self.assertEqual(name, "init")

    def test_parse_legacy_filename(self):
        version, name = _parse_filename("001_init.sql")
        self.assertEqual(version, "001")
        self.assertEqual(name, "init")

    def test_split_sql_simple(self):
        stmts = _split_sql("CREATE TABLE a (id INT); CREATE TABLE b (id INT);")
        self.assertEqual(len(stmts), 2)

    def test_split_sql_respects_trigger_begin_end(self):
        sql = (
            "CREATE TRIGGER t AFTER INSERT ON a BEGIN "
            "UPDATE b SET n = n + 1; END; "
            "CREATE TABLE c (id INT);"
        )
        stmts = _split_sql(sql)
        # The trigger body's inner semicolon must not split the statement.
        self.assertEqual(len(stmts), 2)
        self.assertIn("TRIGGER", stmts[0])


class TestScope(unittest.TestCase):
    def test_hierarchy(self):
        self.assertTrue(scope.has_scope(scope.SCOPE_ADMIN, scope.SCOPE_WRITE))
        self.assertTrue(scope.has_scope(scope.SCOPE_WRITE, scope.SCOPE_READ))
        self.assertFalse(scope.has_scope(scope.SCOPE_READ, scope.SCOPE_WRITE))

    def test_parse_bearer_token(self):
        claims = {"user_id": "u1", "email": "a@b.c", "permission": "write"}
        token = base64.b64encode(json.dumps(claims).encode()).decode()
        parsed = scope.parse_bearer_token(token)
        self.assertEqual(parsed["user_id"], "u1")
        self.assertEqual(parsed["permission"], "write")

    def test_require_scope_grants_and_denies(self):
        claims = {"user_id": "u1", "email": "a@b.c", "permission": "write"}
        token = base64.b64encode(json.dumps(claims).encode()).decode()
        header = f"Bearer {token}"
        self.assertEqual(scope.require_scope(header, scope.SCOPE_WRITE)["user_id"], "u1")
        with self.assertRaises(PermissionError):
            scope.require_scope(header, scope.SCOPE_ADMIN)
        with self.assertRaises(PermissionError):
            scope.require_scope("", scope.SCOPE_READ)


class TestCrypto(unittest.TestCase):
    def test_roundtrip(self):
        try:
            from cryptography.hazmat.primitives.ciphers.aead import AESGCM  # noqa: F401
        except ImportError:
            self.skipTest("cryptography not installed")
        from localitas_client import crypto
        with tempfile.TemporaryDirectory() as d:
            os.environ["HOME"] = d
            crypto._key = None  # reset cached key for the temp HOME
            enc = crypto.encrypt("hello")
            self.assertTrue(enc.startswith("enc:"))
            self.assertEqual(crypto.decrypt(enc), "hello")
            # Plaintext passthrough.
            self.assertEqual(crypto.decrypt("plain"), "plain")
            self.assertEqual(crypto.encrypt(""), "")


class _MockHandler(BaseHTTPRequestHandler):
    routes = {}

    def do_GET(self):
        self._handle()

    def do_POST(self):
        self._handle()

    def do_PUT(self):
        self._handle()

    def do_DELETE(self):
        self._handle()

    def _handle(self):
        key = f"{self.command} {self.path.split('?')[0]}"
        handler = self.routes.get(key)
        if not handler:
            self.send_response(404)
            self.end_headers()
            return
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length) if length else b""
        status, resp = handler(self.headers, raw)
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        if resp is not None:
            self.wfile.write(json.dumps(resp).encode())

    def log_message(self, *args):
        pass


class TestNewClientMethods(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = HTTPServer(("127.0.0.1", 0), _MockHandler)
        cls.port = cls.server.server_address[1]
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.client = LocalitasClient(f"http://127.0.0.1:{cls.port}").with_token("t")

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()

    def test_vault_create_credential(self):
        captured = {}

        def h(headers, raw):
            captured["body"] = json.loads(raw)
            return 200, {"public_id": "cred-1", "name": "s3"}

        _MockHandler.routes["POST /apps/vault/api/credentials"] = h
        result = self.client.vault_create_credential("s3", url="s3://b", data={"key": "v"})
        self.assertEqual(result["public_id"], "cred-1")
        self.assertEqual(captured["body"]["name"], "s3")
        self.assertEqual(captured["body"]["data"]["key"], "v")

    def test_ingest_metrics(self):
        def h(headers, raw):
            body = json.loads(raw)
            return 200, {"accepted": len(body["metrics"])}

        _MockHandler.routes["POST /apps/tsdb/api/ingest"] = h
        n = self.client.ingest_metrics([{"name": "x", "value": 1.0}, {"name": "y", "value": 2.0}])
        self.assertEqual(n, 2)

    def test_register_external_app(self):
        captured = {}

        def h(headers, raw):
            captured["body"] = json.loads(raw)
            return 200, None

        _MockHandler.routes["POST /apps/ext"] = h
        self.client.register_external_app("myapp", "My App", "http://localhost:9999", icon="star")
        self.assertEqual(captured["body"]["name"], "myapp")
        self.assertEqual(captured["body"]["display_name"], "My App")

    def test_run_async_returns_false_without_run_id(self):
        # No run id → handler should respond synchronously (returns False).
        self.assertFalse(self.client.automation().run_async("", lambda: {}))


if __name__ == "__main__":
    unittest.main()
