"""A process core did not start (an app launchd runs, a tool) finds core
through the core.env core writes in its home, as the Go and Node SDKs do;
the environment core gives an app it starts wins over the file."""

import os
import tempfile
import unittest
from pathlib import Path

from localitas_client import ConfigError, LocalitasClient, default_token, dbapi
from tests.local_core import LocalCore
from tests.test_default_client import env


def write_home(home: str, body: str) -> None:
    Path(home).mkdir(parents=True, exist_ok=True)
    path = Path(home) / "core.env"
    path.write_text(body)
    path.chmod(0o600)


class TestNativeRun(unittest.TestCase):
    def setUp(self):
        self.core = LocalCore({"GET /apps/data/api/databases": (200, [{"id": "db1"}])})
        self.addCleanup(self.core.close)
        self.home = tempfile.mkdtemp()

    def test_default_reaches_the_core_of_its_home(self):
        write_home(self.home, f"# written by localitas-core\nCORE_URL={self.core.url}\nLOCALITAS_API_TOKEN=lt_node\n")
        with env(CORE_URL=None, LOCALITAS_API_TOKEN=None, LOCALITAS_HOME=self.home):
            client = LocalitasClient.default()
            self.assertEqual(default_token(), "lt_node")
        self.assertEqual(client.list_databases(), [{"id": "db1"}])
        self.assertEqual(self.core.next_call().headers["authorization"], "Bearer lt_node")

    def test_home_defaults_to_dot_localitas(self):
        user = tempfile.mkdtemp()
        write_home(os.path.join(user, ".localitas"), f"CORE_URL={self.core.url}\nLOCALITAS_API_TOKEN=lt_node\n")
        with env(CORE_URL=None, LOCALITAS_API_TOKEN=None, LOCALITAS_HOME=None, HOME=user):
            client = LocalitasClient.default()
        self.assertEqual(client.base_url, self.core.url)

    def test_environment_wins_over_the_home(self):
        write_home(self.home, "CORE_URL=http://core.invalid:1\nLOCALITAS_API_TOKEN=lt_node\n")
        with env(CORE_URL=self.core.url, LOCALITAS_API_TOKEN="lt_container", LOCALITAS_HOME=self.home):
            client = LocalitasClient.default()
        client.list_databases()
        self.assertEqual(self.core.next_call().headers["authorization"], "Bearer lt_container")

    def test_nothing_configured_names_variable_and_file(self):
        with env(CORE_URL=None, LOCALITAS_API_TOKEN=None, LOCALITAS_HOME=self.home):
            with self.assertRaises(ConfigError) as caught:
                LocalitasClient.default()
            with self.assertRaisesRegex(ConfigError, "CORE_URL is not set"):
                dbapi.connect("db1", token="lt_x")
        self.assertIn("CORE_URL", str(caught.exception))
        self.assertIn(os.path.join(self.home, "core.env"), str(caught.exception))

    def test_bad_url_in_the_home_names_the_file(self):
        write_home(self.home, "CORE_URL=localhost:1\nLOCALITAS_API_TOKEN=lt_node\n")
        with env(CORE_URL=None, LOCALITAS_API_TOKEN=None, LOCALITAS_HOME=self.home):
            with self.assertRaisesRegex(ConfigError, "core.env"):
                LocalitasClient.default()


if __name__ == "__main__":
    unittest.main()
