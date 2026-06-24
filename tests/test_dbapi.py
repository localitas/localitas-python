import json
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from localitas_client import dbapi
import unittest


class MockHandler(BaseHTTPRequestHandler):
    routes = {}

    def do_POST(self):
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


class TestModuleAttributes(unittest.TestCase):
    def test_apilevel(self):
        self.assertEqual(dbapi.apilevel, "2.0")

    def test_threadsafety(self):
        self.assertEqual(dbapi.threadsafety, 1)

    def test_paramstyle(self):
        self.assertEqual(dbapi.paramstyle, "qmark")


class TestExceptionHierarchy(unittest.TestCase):
    def test_interface_error_is_error(self):
        self.assertTrue(issubclass(dbapi.InterfaceError, dbapi.Error))

    def test_database_error_is_error(self):
        self.assertTrue(issubclass(dbapi.DatabaseError, dbapi.Error))

    def test_operational_error_is_database_error(self):
        self.assertTrue(issubclass(dbapi.OperationalError, dbapi.DatabaseError))

    def test_programming_error_is_database_error(self):
        self.assertTrue(issubclass(dbapi.ProgrammingError, dbapi.DatabaseError))


class TestConnect(unittest.TestCase):
    def test_connect_returns_connection(self):
        conn = dbapi.connect(database="testdb", host="localhost:9999", token="lt_test")
        self.assertIsInstance(conn, dbapi.Connection)
        conn.close()

    def test_connect_default_autocommit(self):
        conn = dbapi.connect(database="testdb", host="localhost:9999", token="lt_test")
        self.assertTrue(conn.autocommit)
        conn.close()


class TestCursor(unittest.TestCase):
    def test_cursor_creation(self):
        conn = dbapi.connect(database="testdb", host="localhost:9999", token="lt_test")
        cursor = conn.cursor()
        self.assertIsInstance(cursor, dbapi.Cursor)
        self.assertEqual(cursor.arraysize, 1)
        self.assertIsNone(cursor.description)
        self.assertEqual(cursor.rowcount, -1)
        cursor.close()
        conn.close()

    def test_cursor_closed_raises(self):
        conn = dbapi.connect(database="testdb", host="localhost:9999", token="lt_test")
        cursor = conn.cursor()
        cursor.close()
        with self.assertRaises(dbapi.InterfaceError):
            cursor.execute("SELECT 1")
        conn.close()

    def test_connection_closed_raises(self):
        conn = dbapi.connect(database="testdb", host="localhost:9999", token="lt_test")
        conn.close()
        with self.assertRaises(dbapi.InterfaceError):
            conn.cursor()


class TestSelectQuery(unittest.TestCase):
    def setUp(self):
        self.captured_body = None

        def handle_query(headers, body, path):
            self.captured_body = body
            return 200, {
                "columns": ["id", "name", "age"],
                "rows": [[1, "Alice", 30], [2, "Bob", 28]],
            }

        self.server, base_url = start_mock_server({
            "POST /apps/data/api/databases/testdb/query": handle_query,
        })
        self.conn = dbapi.connect(database="testdb", host=base_url, token="lt_test")

    def tearDown(self):
        self.conn.close()
        self.server.shutdown()

    def test_description_after_select(self):
        cursor = self.conn.cursor()
        cursor.execute("SELECT id, name, age FROM users WHERE age > ?", (25,))
        self.assertEqual(len(cursor.description), 3)
        self.assertEqual(cursor.description[0][0], "id")
        self.assertEqual(cursor.description[1][0], "name")
        self.assertEqual(cursor.description[2][0], "age")
        for col_desc in cursor.description:
            self.assertEqual(len(col_desc), 7)

    def test_fetchall(self):
        cursor = self.conn.cursor()
        cursor.execute("SELECT * FROM users")
        rows = cursor.fetchall()
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0], (1, "Alice", 30))
        self.assertEqual(rows[1], (2, "Bob", 28))

    def test_fetchone(self):
        cursor = self.conn.cursor()
        cursor.execute("SELECT * FROM users")
        row1 = cursor.fetchone()
        self.assertEqual(row1, (1, "Alice", 30))
        row2 = cursor.fetchone()
        self.assertEqual(row2, (2, "Bob", 28))
        row3 = cursor.fetchone()
        self.assertIsNone(row3)

    def test_fetchmany(self):
        cursor = self.conn.cursor()
        cursor.execute("SELECT * FROM users")
        rows = cursor.fetchmany(1)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0], (1, "Alice", 30))
        rows = cursor.fetchmany(5)
        self.assertEqual(len(rows), 1)

    def test_rowcount_for_select(self):
        cursor = self.conn.cursor()
        cursor.execute("SELECT * FROM users")
        self.assertEqual(cursor.rowcount, 2)

    def test_parameters_sent(self):
        cursor = self.conn.cursor()
        cursor.execute("SELECT * FROM users WHERE age > ?", (25,))
        self.assertEqual(self.captured_body["sql"], "SELECT * FROM users WHERE age > ?")
        self.assertEqual(self.captured_body["args"], [25])


class TestExecStatement(unittest.TestCase):
    def setUp(self):
        def handle_exec(headers, body, path):
            if "statements" in (body or {}):
                return 200, {"rows_affected": len(body["statements"])}
            return 200, {"rows_affected": 1, "last_insert_id": 42}

        self.server, base_url = start_mock_server({
            "POST /apps/data/api/databases/testdb/exec": handle_exec,
        })
        self.conn = dbapi.connect(database="testdb", host=base_url, token="lt_test")

    def tearDown(self):
        self.conn.close()
        self.server.shutdown()

    def test_insert_rowcount(self):
        cursor = self.conn.cursor()
        cursor.execute("INSERT INTO users (name) VALUES (?)", ("Carol",))
        self.assertEqual(cursor.rowcount, 1)

    def test_lastrowid(self):
        cursor = self.conn.cursor()
        cursor.execute("INSERT INTO users (name) VALUES (?)", ("Carol",))
        self.assertEqual(cursor.lastrowid, 42)

    def test_no_description_for_exec(self):
        cursor = self.conn.cursor()
        cursor.execute("INSERT INTO users (name) VALUES (?)", ("Carol",))
        self.assertIsNone(cursor.description)


class TestTransaction(unittest.TestCase):
    def setUp(self):
        self.tx_body = None

        def handle_exec(headers, body, path):
            self.tx_body = body
            if "statements" in (body or {}):
                return 200, {"rows_affected": len(body["statements"])}
            return 200, {"rows_affected": 1}

        self.server, base_url = start_mock_server({
            "POST /apps/data/api/databases/testdb/exec": handle_exec,
        })
        self.conn = dbapi.connect(database="testdb", host=base_url, token="lt_test")

    def tearDown(self):
        self.conn.close()
        self.server.shutdown()

    def test_manual_commit(self):
        self.conn.autocommit = False
        cursor = self.conn.cursor()
        cursor.execute("INSERT INTO a (x) VALUES (?)", (1,))
        cursor.execute("INSERT INTO b (y) VALUES (?)", (2,))
        self.assertIsNone(self.tx_body)
        self.conn.commit()
        self.assertIsNotNone(self.tx_body)
        self.assertEqual(len(self.tx_body["statements"]), 2)

    def test_rollback_clears_pending(self):
        self.conn.autocommit = False
        cursor = self.conn.cursor()
        cursor.execute("INSERT INTO a (x) VALUES (?)", (1,))
        self.conn.rollback()
        self.conn.commit()
        self.assertIsNone(self.tx_body)


class TestExecutemany(unittest.TestCase):
    def setUp(self):
        self.call_count = 0

        def handle_exec(headers, body, path):
            self.call_count += 1
            return 200, {"rows_affected": 1, "last_insert_id": self.call_count}

        self.server, base_url = start_mock_server({
            "POST /apps/data/api/databases/testdb/exec": handle_exec,
        })
        self.conn = dbapi.connect(database="testdb", host=base_url, token="lt_test")

    def tearDown(self):
        self.conn.close()
        self.server.shutdown()

    def test_executemany(self):
        cursor = self.conn.cursor()
        cursor.executemany("INSERT INTO users (name) VALUES (?)", [("A",), ("B",), ("C",)])
        self.assertEqual(self.call_count, 3)


class TestReadDetection(unittest.TestCase):
    def test_select(self):
        self.assertTrue(dbapi._is_read_query("SELECT * FROM t"))

    def test_select_lowercase(self):
        self.assertTrue(dbapi._is_read_query("select * from t"))

    def test_pragma(self):
        self.assertTrue(dbapi._is_read_query("PRAGMA table_info(t)"))

    def test_explain(self):
        self.assertTrue(dbapi._is_read_query("EXPLAIN SELECT 1"))

    def test_with_cte(self):
        self.assertTrue(dbapi._is_read_query("WITH cte AS (SELECT 1) SELECT * FROM cte"))

    def test_insert(self):
        self.assertFalse(dbapi._is_read_query("INSERT INTO t VALUES (1)"))

    def test_update(self):
        self.assertFalse(dbapi._is_read_query("UPDATE t SET x=1"))

    def test_delete(self):
        self.assertFalse(dbapi._is_read_query("DELETE FROM t"))

    def test_whitespace(self):
        self.assertTrue(dbapi._is_read_query("  select 1"))


if __name__ == "__main__":
    unittest.main()
