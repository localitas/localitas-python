"""client.db(name) is a DB-API connection to the app's system database by
name: its ID is resolved through core's idempotent system-dbs call on first
use, remembered once known, and asked again after a failure."""

import unittest

from localitas_client import LocalitasClient, dbapi
from tests.local_core import LocalCore


class TestDBByName(unittest.TestCase):
    def setUp(self):
        self.answers = [(200, {"id": "db-7", "name": "notes"})]
        self.core = LocalCore({
            "POST /apps/data/api/system-dbs": lambda call: self.answers.pop(0),
            "POST /apps/data/api/databases/db-7/query": (200, {"columns": ["id", "title"], "rows": [[1, "first"]]}),
            "POST /apps/data/api/databases/db-7/exec": (200, {"rows_affected": 1, "last_insert_id": 2}),
        })
        self.addCleanup(self.core.close)
        self.client = LocalitasClient(self.core.url, "lt_app")

    def calls(self):
        out = []
        while not self.core.calls.empty():
            out.append(self.core.calls.get())
        return out

    def test_resolves_the_name_once_and_uses_the_id(self):
        conn = self.client.db("notes")
        self.assertIsInstance(conn, dbapi.Connection)
        self.assertEqual(self.calls(), [])

        cur = conn.cursor()
        cur.execute("SELECT id, title FROM notes WHERE id = ?", (1,))
        self.assertEqual(cur.fetchall(), [(1, "first")])
        cur.execute("INSERT INTO notes (title) VALUES (?)", ("second",))
        self.assertEqual((cur.rowcount, cur.lastrowid), (1, 2))
        self.assertEqual(conn.database, "db-7")

        calls = self.calls()
        self.assertEqual([(c.method, c.path) for c in calls], [
            ("POST", "/apps/data/api/system-dbs"),
            ("POST", "/apps/data/api/databases/db-7/query"),
            ("POST", "/apps/data/api/databases/db-7/exec"),
        ])
        self.assertEqual(calls[0].body, {"name": "notes"})
        self.assertEqual(calls[1].body, {"sql": "SELECT id, title FROM notes WHERE id = ?", "args": [1]})
        self.assertEqual({c.headers["authorization"] for c in calls}, {"Bearer lt_app"})

    def test_a_failed_resolve_is_raised_and_retried(self):
        self.answers.insert(0, (503, {"error": "data shard electing"}))
        cur = self.client.db("notes").cursor()
        with self.assertRaises(dbapi.DatabaseError):
            cur.execute("SELECT id, title FROM notes")
        cur.execute("SELECT id, title FROM notes")
        self.assertEqual(cur.fetchone(), (1, "first"))
        self.assertEqual([c.path for c in self.calls()], [
            "/apps/data/api/system-dbs",
            "/apps/data/api/system-dbs",
            "/apps/data/api/databases/db-7/query",
        ])

    def test_commit_uses_the_resolved_id(self):
        conn = self.client.db("notes")
        conn.autocommit = False
        conn.cursor().execute("INSERT INTO notes (title) VALUES (?)", ("x",))
        conn.commit()
        calls = self.calls()
        self.assertEqual(calls[-1].path, "/apps/data/api/databases/db-7/exec")
        self.assertEqual(calls[-1].body, {"statements": [{"sql": "INSERT INTO notes (title) VALUES (?)", "args": ["x"]}]})

    def test_refuses_an_empty_name(self):
        with self.assertRaises(ValueError):
            self.client.db("")


if __name__ == "__main__":
    unittest.main()
