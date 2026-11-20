"""LocalitasClient.announce against a real local HTTP server standing in for
core's readiness long poll, its door and its app registry."""
import queue
import threading
import time
import unittest
from urllib.parse import parse_qs

from localitas_client import LocalitasClient
from tests.local_core import LocalCore


def _seconds(duration: str) -> float:
    if duration.endswith("ms"):
        return float(duration[:-2]) / 1000
    if duration.endswith("s"):
        return float(duration[:-1])
    return 0.0


class FakeCore:
    """A readiness question with a timeout is held until the node is ready
    (or the timeout passes); one without is answered at once. Before Ready,
    POST /apps/ext is answered 503 by the door."""

    def __init__(self, post_status=()):
        self.lock = threading.Lock()
        self.ready = threading.Event()
        self.asks = []
        self.posts = 0
        self.post_status = list(post_status)
        self.events: "queue.Queue[str]" = queue.Queue()
        self.core = LocalCore({
            "GET /api/system/ready": self._ready,
            "POST /apps/ext": self._register,
            "DELETE /apps/ext/app": (204, None),
        })
        self.url = self.core.url

    def close(self):
        self.ready.set()
        self.core.close()

    def _ready(self, call):
        q = parse_qs(call.query)
        with self.lock:
            self.asks.append(q)
        self.events.put("ready")
        timeout = _seconds(q.get("timeout", ["0"])[0])
        if timeout > 0:
            self.ready.wait(min(timeout, 10))
        want = q.get("phase", [""])[0]
        if self.ready.is_set():
            return 200, {"phase": "ready", "want": want, "ready": True}
        return 503, {"phase": "serving", "want": want, "ready": False, "error": "not ready within the timeout"}

    def _register(self, call):
        with self.lock:
            self.posts += 1
            status = self.post_status[self.posts - 1] if self.posts <= len(self.post_status) else 201
            if not self.ready.is_set():
                status = 503
            if status == 503:
                self.ready.clear()
        self.events.put("post")
        if status == 201:
            return 201, {"name": "app"}
        if status == 503:
            return 503, {"error": "node not ready: serving"}
        if status == 502:
            return 502, {"error": "health.json: connection refused: the app does not answer health.json"}
        return status, {"error": "health.json names no app"}

    def next(self) -> str:
        try:
            return self.events.get(timeout=5)
        except queue.Empty:
            raise AssertionError("core heard nothing from the app") from None

    def post_count(self) -> int:
        with self.lock:
            return self.posts

    def ask_count(self) -> int:
        with self.lock:
            return len(self.asks)


class TestAnnounce(unittest.TestCase):
    def start(self, core: FakeCore):
        self.addCleanup(core.close)
        announcement = LocalitasClient(core.url, "token").announce(8123)
        self.addCleanup(announcement.withdraw)
        return announcement

    def ended(self, announcement):
        announcement._thread.join(5)
        self.assertFalse(announcement._thread.is_alive(), "the announcement did not end")

    def test_waits_on_cores_readiness_long_poll_before_registering(self):
        core = FakeCore()
        announcement = self.start(core)

        self.assertEqual(core.next(), "ready", "POST /apps/ext reached core before its readiness answered")
        q = core.asks[0]
        self.assertEqual(q.get("phase"), ["ready"])
        self.assertGreater(_seconds(q.get("timeout", ["0"])[0]), 0, "readiness asked without a timeout: not a long poll")

        core.ready.set()
        self.assertEqual(core.next(), "post", "after core became ready the app did not register")
        self.assertEqual(core.ask_count(), 1, "more than one readiness request: polled, not one long poll")
        self.ended(announcement)
        self.assertEqual(announcement.name, "app")
        self.assertEqual(core.post_count(), 1)

    def test_not_ready_registration_waits_again_then_registers_once(self):
        core = FakeCore(post_status=[503])
        core.ready.set()
        announcement = self.start(core)

        self.assertEqual([core.next(), core.next(), core.next()], ["ready", "post", "ready"])
        core.ready.set()
        self.assertEqual(core.next(), "post", "after core became ready again the app did not register")
        self.ended(announcement)
        self.assertEqual(announcement.name, "app")
        self.assertEqual((core.post_count(), core.ask_count()), (2, 2))

    def test_app_not_answering_yet_is_asked_again_then_registers_once(self):
        core = FakeCore(post_status=[502])
        core.ready.set()
        announcement = self.start(core)

        self.assertEqual([core.next(), core.next(), core.next(), core.next()], ["ready", "post", "ready", "post"],
                         "core found the app not answering yet (502) and the app did not register once it answered")
        self.ended(announcement)
        self.assertEqual(announcement.name, "app")
        self.assertEqual(core.post_count(), 2)

    def test_refused_registration_is_not_retried(self):
        core = FakeCore(post_status=[422])
        core.ready.set()
        announcement = self.start(core)

        self.assertEqual([core.next(), core.next()], ["ready", "post"])
        self.ended(announcement)
        self.assertEqual(announcement.name, "")
        self.assertEqual(core.post_count(), 1)

    def test_withdraw_while_waiting_on_readiness_returns_at_once(self):
        core = FakeCore()
        announcement = self.start(core)

        self.assertEqual(core.next(), "ready")
        began = time.monotonic()
        announcement.withdraw()
        self.assertLess(time.monotonic() - began, 1, "withdraw waited on core's readiness")
        core.ready.set()
        self.ended(announcement)
        self.assertEqual(core.post_count(), 0, "withdrawn before core was ready, the app registered")


if __name__ == "__main__":
    unittest.main()
