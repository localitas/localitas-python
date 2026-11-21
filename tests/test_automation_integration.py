"""Integration tests for waiting on automation runs. Requires the integration
cluster (make integration-cluster-start).
"""
import threading
import time
import unittest

from localitas_client import LocalitasClient

INTEG_URL = "http://localhost:9090"
INTEG_TOKEN = "lt_integ_a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6e7f8a9b0c1d2e3f4a5b6c7d8e9f0a1b2"


def make_client():
    return LocalitasClient(INTEG_URL).with_token(INTEG_TOKEN)


def slow_automation(client, name):
    """A manual automation whose only call fails slowly: retried with 30 s
    backoff, it runs for minutes unless its outcome is published."""
    settings = {
        "url": f"/api/system/no_such_route_{name}",
        "method": "GET",
        "timeout_ms": 5000,
        "max_retries": 3,
        "backoff_ms": 30000,
        "max_backoff_ms": 30000,
    }
    return client._do("POST", "/apps/automation/api/automations", {
        "name": name,
        "description": "python sdk wait test",
        "dag_config": {"dag_id": name, "name": name, "description": "", "nodes": [{
            "node_id": "slow", "node_type": "http-api", "execution_strategy": "any_node",
            "metadata": settings,
        }]},
        "trigger_type": "manual",
        "trigger_config": {},
        "is_enabled": True,
    })


class TestWaitForRun(unittest.TestCase):
    def setUp(self):
        self.client = make_client()
        self.auto = slow_automation(self.client, f"py_wait_{int(time.time() * 1000000)}")
        self.addCleanup(self.client._do, "DELETE", f"/apps/automation/api/automations/{self.auto['id']}")

    def _running_run(self):
        run = self.client.automation().trigger(self.auto["id"])
        deadline = time.time() + 30
        while self.client.automation().get_run(run["id"])["status"] != "running":
            self.assertLess(time.time(), deadline, "the run never started")
            time.sleep(0.1)
        return run

    def test_a_waiting_run_returns_once_its_outcome_is_published(self):
        run = self._running_run()
        got = {}

        def wait():
            got["run"] = self.client.automation().wait_for_run(run["id"], timeout=30)

        waiter = threading.Thread(target=wait)
        waiter.start()
        time.sleep(0.5)
        self.client.automation().publish_result(run["id"], "completed", {"answer": 42})
        published = time.time()
        waiter.join(timeout=30)
        self.assertFalse(waiter.is_alive(), "wait_for_run did not return")
        self.assertLess(time.time() - published, 5, "wait_for_run returned long after the outcome")
        self.assertEqual(got["run"]["status"], "completed")
        self.assertEqual(got["run"]["result"]["answer"], 42)

    def test_a_finished_run_returns_at_once(self):
        run = self._running_run()
        self.client.automation().publish_result(run["id"], "failed", None, "stopped")
        start = time.time()
        done = self.client.automation().wait_for_run(run["id"], timeout=30)
        self.assertLess(time.time() - start, 5)
        self.assertEqual((done["status"], done["error"]), ("failed", "stopped"))


if __name__ == "__main__":
    unittest.main()
