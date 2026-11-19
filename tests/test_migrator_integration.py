"""Migrators on both nodes of the integration cluster racing one migration.
Requires the integration cluster running (make integration-cluster-start)."""

import os
import tempfile
import threading
import time
import unittest

from localitas_client import LocalitasClient, Migrator

NODE_URLS = ("http://localhost:9090", "http://localhost:9190")
# The integration cluster's seeded API token for alice@test.local
# (core seeds/005_integ_api_token.sql).
INTEG_TOKEN = "lt_integ_a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6e7f8a9b0c1d2e3f4a5b6c7d8e9f0a1b2"

MIGRATION = "CREATE TABLE items (name TEXT); INSERT INTO items (name) VALUES ('default');"


class TestRacingMigrators(unittest.TestCase):
    def setUp(self):
        self.clients = [LocalitasClient(url).with_token(INTEG_TOKEN) for url in NODE_URLS]
        self.db_id = self.clients[0].create_database(f"py_migrace_{int(time.time() * 1000000)}")["id"]
        self.dir = tempfile.mkdtemp()
        with open(os.path.join(self.dir, "20260101-000000-000-items.sql"), "w", encoding="utf-8") as fh:
            fh.write(MIGRATION)

    def tearDown(self):
        self.clients[0].delete_database(self.db_id)

    def test_racing_nodes_apply_a_migration_once_without_a_failed_write(self):
        """Both nodes find the migration pending and commit it at once: core
        applies the first entry in its log and skips the second on its
        condition, so the data migration applies once and neither fails."""
        barrier = threading.Barrier(len(self.clients))
        answers, errors = [], []

        def migrator(client):
            m = Migrator.for_database(self.dir, client, self.db_id)
            commit = m._exec_tx

            def exec_tx(statements, unless_exists):
                barrier.wait(timeout=60)
                result = commit(statements, unless_exists)
                answers.append(result)
                return result

            m._exec_tx = exec_tx
            return m

        def run(m):
            try:
                m.run()
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=run, args=(migrator(c),)) for c in self.clients]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(errors, [])
        self.assertEqual(len(answers), 2)
        self.assertEqual(sum(1 for a in answers if a.get("skipped")), 1, answers)
        for client in self.clients:
            rows = client.sql_query(
                self.db_id,
                "SELECT (SELECT group_concat(name) FROM items), (SELECT group_concat(version) FROM schema_migrations)",
            )["rows"]
            self.assertEqual(rows, [["default", "20260101-000000-000"]])


if __name__ == "__main__":
    unittest.main()
