"""LocalitasClient.default() takes core's address and token from the
environment core gives every app, and nothing in the SDK ever guesses a host
or port."""

import os
import re
import unittest
from contextlib import contextmanager
from pathlib import Path

from localitas_client import ConfigError, LocalitasClient, dbapi, scope
from tests.local_core import LocalCore
from tests.test_identity import GOLDEN_TOKEN

PACKAGE = Path(__file__).resolve().parent.parent / "localitas_client"
GUESSES = re.compile(r"8080|8090|localhost|127\.0\.0\.1|host\.docker\.internal")
ALLOWED = {
    # An app announces its own listener on this host to core: the app's
    # address, not a guess at where core is.
    ("client.py", 'self._url = f"http://localhost:{port}"'),
}


@contextmanager
def env(**values):
    saved = {k: os.environ.get(k) for k in values}
    for k, v in values.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v
    try:
        yield
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


class TestDefault(unittest.TestCase):
    def setUp(self):
        self.core = LocalCore({"GET /apps/data/api/databases": (200, [{"id": "db1"}])})
        self.addCleanup(self.core.close)

    def test_without_core_url_fails_naming_it(self):
        for value in (None, ""):
            with self.subTest(core_url=value), env(CORE_URL=value, LOCALITAS_API_TOKEN="lt_x"):
                with self.assertRaisesRegex(ConfigError, "CORE_URL is not set"):
                    LocalitasClient.default()

    def test_core_url_must_be_an_absolute_http_url(self):
        for value in ("core.local:9000", "ftp://core.local", "/apps"):
            with self.subTest(core_url=value), env(CORE_URL=value, LOCALITAS_API_TOKEN="lt_x"):
                with self.assertRaisesRegex(ConfigError, "CORE_URL"):
                    LocalitasClient.default()

    def test_without_token_fails_naming_it(self):
        with env(CORE_URL=self.core.url, LOCALITAS_API_TOKEN=None):
            with self.assertRaisesRegex(ConfigError, "LOCALITAS_API_TOKEN"):
                LocalitasClient.default()

    def test_uses_core_url_and_token(self):
        with env(CORE_URL=self.core.url + "/", LOCALITAS_API_TOKEN="lt_app"):
            client = LocalitasClient.default()
        self.assertEqual(client.list_databases(), [{"id": "db1"}])
        call = self.core.next_call()
        self.assertEqual((call.method, call.path), ("GET", "/apps/data/api/databases"))
        self.assertEqual(call.headers["authorization"], "Bearer lt_app")


class TestNoGuessedAddress(unittest.TestCase):
    def test_package_source_names_no_host_or_port(self):
        found = []
        for path in sorted(PACKAGE.glob("*.py")):
            for n, line in enumerate(path.read_text().splitlines(), 1):
                if GUESSES.search(line) and (path.name, line.strip()) not in ALLOWED:
                    found.append(f"{path.name}:{n}: {line.strip()}")
        self.assertEqual(found, [])

    def test_client_without_base_url_fails_clearly(self):
        client = LocalitasClient("", "lt_x")
        with self.assertRaisesRegex(ConfigError, "CORE_URL is not set"):
            client.list_databases()
        with self.assertRaisesRegex(ConfigError, "CORE_URL is not set"):
            client.ingest_dogstatsd("x:1|c")

    def test_dbapi_connect_takes_core_url(self):
        core = LocalCore({"POST /apps/data/api/databases/db1/query": (200, {"columns": ["n"], "rows": [[1]]})})
        self.addCleanup(core.close)
        with env(CORE_URL=core.url):
            conn = dbapi.connect("db1", token="lt_x")
        cur = conn.cursor()
        cur.execute("SELECT 1")
        self.assertEqual(cur.fetchall(), [(1,)])

    def test_dbapi_connect_without_host_or_core_url_fails_clearly(self):
        with env(CORE_URL=None):
            with self.assertRaisesRegex(ConfigError, "CORE_URL is not set"):
                dbapi.connect("db1", token="lt_x")

    def test_identity_key_is_fetched_only_from_core_url(self):
        with env(CORE_URL=None, LOCALITAS_API_TOKEN="lt_x", LOCALITAS_IDENTITY_PUBLIC_KEY=None):
            scope.reset_identity_public_keys()
            self.addCleanup(scope.reset_identity_public_keys)
            with self.assertRaisesRegex(ValueError, "no identity public key"):
                scope.parse_bearer_token(GOLDEN_TOKEN)
            self.assertEqual(scope._last_fetch, 0.0)


if __name__ == "__main__":
    unittest.main()
