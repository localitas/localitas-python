"""Migrator against a real SQLite database: atomic, run-once migrations."""

import os
import sqlite3
import tempfile
import threading
import unittest

from localitas_client.migrator import Migrator


SEED = {
    "20260101-000000-000-tables.sql": "CREATE TABLE items (id INTEGER PRIMARY KEY, name TEXT);",
    "20260102-000000-000-seed.sql": "INSERT INTO items (name) VALUES ('default');",
    "20260103-000000-000-column.sql": "ALTER TABLE items ADD COLUMN color TEXT;",
}


def write_migrations(files):
    d = tempfile.mkdtemp()
    for name, sql in files.items():
        with open(os.path.join(d, name), "w", encoding="utf-8") as fh:
            fh.write(sql)
    return d


def sqlite_migrator(migrations_dir, db_path):
    """A Migrator whose callables run on their own SQLite connection."""
    conn = sqlite3.connect(db_path, timeout=10, isolation_level=None, check_same_thread=False)

    def exec_sql(sql, *args):
        conn.execute(sql, args)

    def query_sql(sql, *args):
        return {"rows": [list(r) for r in conn.execute(sql, args).fetchall()]}

    def exec_tx(statements):
        conn.execute("BEGIN IMMEDIATE")
        try:
            for sql, args in statements:
                conn.execute(sql, args)
        except Exception:
            conn.execute("ROLLBACK")
            raise
        conn.execute("COMMIT")

    return Migrator(migrations_dir, exec_sql, query_sql, exec_tx), conn


def rows(db_path, sql):
    conn = sqlite3.connect(db_path)
    try:
        return [r[0] for r in conn.execute(sql).fetchall()]
    finally:
        conn.close()


class TestMigrator(unittest.TestCase):
    def setUp(self):
        self.db = os.path.join(tempfile.mkdtemp(), "app.sqlitedb")

    def test_applies_every_migration_once(self):
        m, _ = sqlite_migrator(write_migrations(SEED), self.db)
        m.run()
        m.run()
        self.assertEqual(rows(self.db, "SELECT name FROM items"), ["default"])
        self.assertEqual(
            rows(self.db, "SELECT version FROM schema_migrations ORDER BY version"),
            ["20260101-000000-000", "20260102-000000-000", "20260103-000000-000"],
        )

    def test_stale_node_does_not_reapply(self):
        d = write_migrations(SEED)
        first, _ = sqlite_migrator(d, self.db)
        first.run()
        stale, _ = sqlite_migrator(d, self.db)
        for version, name, sql in stale._migrations:
            with self.assertRaisesRegex(Exception, "UNIQUE constraint failed: schema_migrations.version"):
                stale._apply(version, name, sql)
        self.assertEqual(rows(self.db, "SELECT name FROM items"), ["default"])

    def test_concurrent_runners_apply_each_migration_once(self):
        d = write_migrations(SEED)
        sqlite3.connect(self.db).close()
        errors = []

        def run():
            m, _ = sqlite_migrator(d, self.db)
            try:
                m.run()
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=run) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])
        self.assertEqual(rows(self.db, "SELECT name FROM items"), ["default"])

    def test_failed_migration_changes_nothing(self):
        broken = write_migrations({
            "20260101-000000-000-tables.sql": "CREATE TABLE items (id INTEGER PRIMARY KEY);",
            "20260102-000000-000-two.sql": "ALTER TABLE items ADD COLUMN a TEXT; INSERT INTO missing VALUES (1);",
        })
        m, _ = sqlite_migrator(broken, self.db)
        with self.assertRaisesRegex(Exception, "no such table: missing"):
            m.run()
        self.assertEqual(rows(self.db, "SELECT name FROM pragma_table_info('items')"), ["id"])
        self.assertEqual(rows(self.db, "SELECT version FROM schema_migrations"), ["20260101-000000-000"])

    def test_records_schema_already_in_place(self):
        conn = sqlite3.connect(self.db)
        conn.execute("CREATE TABLE items (id INTEGER PRIMARY KEY, name TEXT, color TEXT)")
        conn.commit()
        conn.close()
        m, _ = sqlite_migrator(write_migrations({
            "20260101-000000-000-tables.sql": "CREATE TABLE IF NOT EXISTS items (id INTEGER PRIMARY KEY, name TEXT);",
            "20260103-000000-000-column.sql": "ALTER TABLE items ADD COLUMN color TEXT;",
        }), self.db)
        m.run()
        self.assertEqual(
            rows(self.db, "SELECT version FROM schema_migrations ORDER BY version"),
            ["20260101-000000-000", "20260103-000000-000"],
        )

    def test_reports_bad_sql(self):
        m, _ = sqlite_migrator(write_migrations({
            "20260101-000000-000-bad.sql": "CREATE TABLEX items (id INTEGER);",
        }), self.db)
        with self.assertRaisesRegex(Exception, "syntax error"):
            m.run()


if __name__ == "__main__":
    unittest.main()
