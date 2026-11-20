"""AutomationClient.wait_for_run against a real local HTTP server standing in
for core's run wait long poll and its readiness endpoint."""
import threading
import unittest
from urllib.parse import parse_qs

from localitas_client import LocalitasClient
from localitas_client.client import APIError
from tests.local_core import LocalCore

WAIT = "GET /apps/automation/api/runs/run_1/wait"


def _seconds(duration: str) -> float:
    return float(duration[:-1]) if duration.endswith("s") else 0.0


class Answers:
    """Each question gets the next answer, in order."""

    def __init__(self, *answers):
        self.lock = threading.Lock()
        self.answers = list(answers)
        self.asks = []

    def __call__(self, call):
        with self.lock:
            self.asks.append(parse_qs(call.query))
            return self.answers[len(self.asks) - 1]


def wait_answer(finished: bool, status: str):
    return 200, {"finished": finished, "run": {"id": "run_1", "status": status}}


class WaitForRunTest(unittest.TestCase):
    def serve(self, routes):
        core = LocalCore(routes)
        self.addCleanup(core.close)
        return LocalitasClient(base_url=core.url, token="t")

    def test_asks_again_until_core_answers_the_run_finished(self):
        answers = Answers(wait_answer(False, "queued"), wait_answer(False, "running"), wait_answer(True, "completed"))
        client = self.serve({WAIT: answers})
        run = client.automation().wait_for_run("run_1", timeout=10)
        self.assertEqual(run, {"id": "run_1", "status": "completed"})
        self.assertEqual(len(answers.asks), 3)
        for q in answers.asks:
            hold = _seconds(q["timeout"][0])
            self.assertTrue(0 < hold <= 25, f"asked with timeout {q['timeout'][0]}")

    def test_a_not_ready_core_is_waited_for_then_asked_again(self):
        answers = Answers((503, {"error": "node not ready: serving"}), wait_answer(True, "failed"))
        ready = Answers((200, {"phase": "ready", "want": "ready", "ready": True}))
        client = self.serve({WAIT: answers, "GET /api/system/ready": ready})
        run = client.automation().wait_for_run("run_1", timeout=10)
        self.assertEqual(run["status"], "failed")
        self.assertEqual(len(ready.asks), 1)

    def test_a_refused_run_raises_cores_answer(self):
        client = self.serve({WAIT: (403, {"error": "Access denied"})})
        with self.assertRaises(APIError) as cm:
            client.automation().wait_for_run("run_1", timeout=10)
        self.assertEqual(cm.exception.status_code, 403)


if __name__ == "__main__":
    unittest.main()
