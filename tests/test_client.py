import json
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from localitas_client import LocalitasClient, APIError
import unittest


class MockHandler(BaseHTTPRequestHandler):
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
        if handler:
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length)) if length else None
            status, resp = handler(self.headers, body, self.path)
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            if resp is not None:
                self.wfile.write(json.dumps(resp).encode())
        else:
            self.send_response(404)
            self.end_headers()
            self.wfile.write(b'{"error":"not found"}')

    def log_message(self, format, *args):
        pass


def start_mock_server(routes):
    MockHandler.routes = routes
    server = HTTPServer(("127.0.0.1", 0), MockHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    port = server.server_address[1]
    return server, f"http://127.0.0.1:{port}"


class TestClient(unittest.TestCase):
    def setUp(self):
        self.captured_auth = None

        def handle_list_databases(headers, body, path):
            self.captured_auth = headers.get("Authorization")
            return 200, [{"id": "db1", "name": "mydb"}]

        def handle_create_database(headers, body, path):
            return 200, {"id": "db2", "name": body["name"], "system": body.get("system", False)}

        def handle_get_database(headers, body, path):
            return 200, {"id": "db1", "name": "mydb"}

        def handle_delete_database(headers, body, path):
            return 200, None

        def handle_sql_query(headers, body, path):
            return 200, {"columns": ["id", "name"], "rows": [[1, "Alice"], [2, "Bob"]]}

        def handle_sql_exec(headers, body, path):
            if "statements" in (body or {}):
                return 200, {"rows_affected": len(body["statements"])}
            return 200, {"rows_affected": 1}

        def handle_search_fts(headers, body, path):
            return 200, {"results": [{"id": "r1", "snippet": "match"}]}

        def handle_vault_list(headers, body, path):
            return 200, {"credentials": [{"id": "c1", "name": "aws"}]}

        self.server, self.base_url = start_mock_server({
            "GET /apps/data/api/databases": handle_list_databases,
            "POST /apps/data/api/databases": handle_create_database,
            "GET /apps/data/api/databases/db1": handle_get_database,
            "DELETE /apps/data/api/databases/db1": handle_delete_database,
            "POST /apps/data/api/databases/db1/query": handle_sql_query,
            "POST /apps/data/api/databases/db1/exec": handle_sql_exec,
            "GET /apps/data/api/search": handle_search_fts,
            "GET /apps/vault/api/credentials": handle_vault_list,
        })

    def tearDown(self):
        self.server.shutdown()

    def test_with_token(self):
        client = LocalitasClient(self.base_url)
        authed = client.with_token("mytoken123")
        self.assertEqual(authed.token, "mytoken123")
        self.assertEqual(client.token, "")

    def test_auth_header(self):
        client = LocalitasClient(self.base_url).with_token("secret")
        client.list_databases()
        self.assertEqual(self.captured_auth, "Bearer secret")

    def test_list_databases(self):
        client = LocalitasClient(self.base_url)
        dbs = client.list_databases()
        self.assertEqual(len(dbs), 1)
        self.assertEqual(dbs[0]["name"], "mydb")

    def test_create_database(self):
        client = LocalitasClient(self.base_url)
        db = client.create_database("testdb", system=True)
        self.assertEqual(db["name"], "testdb")
        self.assertTrue(db["system"])

    def test_get_database(self):
        client = LocalitasClient(self.base_url)
        db = client.get_database("db1")
        self.assertEqual(db["id"], "db1")

    def test_delete_database(self):
        client = LocalitasClient(self.base_url)
        client.delete_database("db1")

    def test_sql_query(self):
        client = LocalitasClient(self.base_url)
        result = client.sql_query("db1", "SELECT * FROM users")
        self.assertEqual(len(result["rows"]), 2)
        self.assertEqual(result["rows"][0][1], "Alice")

    def test_sql_exec(self):
        client = LocalitasClient(self.base_url)
        result = client.sql_exec("db1", "INSERT INTO users (name) VALUES (?)", "Carol")
        self.assertEqual(result["rows_affected"], 1)

    def test_sql_transaction(self):
        client = LocalitasClient(self.base_url)
        result = client.sql_transaction("db1", [
            {"sql": "INSERT INTO a VALUES (?)", "args": [1]},
            {"sql": "INSERT INTO b VALUES (?)", "args": [2]},
        ])
        self.assertEqual(result["rows_affected"], 2)

    def test_search_fts(self):
        client = LocalitasClient(self.base_url)
        result = client.search_fts("hello")
        self.assertEqual(len(result["results"]), 1)

    def test_vault_list(self):
        client = LocalitasClient(self.base_url)
        creds = client.vault_list_credentials()
        self.assertEqual(len(creds), 1)
        self.assertEqual(creds[0]["name"], "aws")

    def test_api_error(self):
        client = LocalitasClient(self.base_url)
        with self.assertRaises(APIError) as ctx:
            client.get_database("nonexistent")
        self.assertEqual(ctx.exception.status_code, 404)

    def test_trailing_slash_stripped(self):
        client = LocalitasClient(self.base_url + "///")
        self.assertFalse(client.base_url.endswith("/"))


if __name__ == "__main__":
    unittest.main()
