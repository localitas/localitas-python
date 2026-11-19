"""client.cache(name).subscribe / watch / watch_latest open the cache's
WebSocket from the client's own base URL and token: no socket URL to know."""

import queue
import re
import unittest
from pathlib import Path

from localitas_client import ConfigError, LocalitasClient, PubSubWS, websocket_url
from tests.test_pubsub_ws import PubSubWSTestCase, _CacheWSHandler

PACKAGE = Path(__file__).resolve().parent.parent / "localitas_client"


class TestWebsocketURL(unittest.TestCase):
    def test_http_is_ws_and_https_is_wss(self):
        self.assertEqual(websocket_url("http://core.test:7000", "/apps/cache/ws/c"), "ws://core.test:7000/apps/cache/ws/c")
        self.assertEqual(websocket_url("https://core.test/", "/apps/cache/ws/c"), "wss://core.test/apps/cache/ws/c")

    def test_no_or_odd_base_url_fails_clearly(self):
        with self.assertRaisesRegex(ConfigError, "CORE_URL is not set"):
            websocket_url("", "/apps/cache/ws/c")
        with self.assertRaises(ConfigError):
            websocket_url("ftp://core.test", "/apps/cache/ws/c")

    def test_no_socket_url_is_spelled_out_in_the_package(self):
        found = [f"{p.name}:{n}" for p in sorted(PACKAGE.glob("*.py"))
                 for n, line in enumerate(p.read_text().splitlines(), 1) if re.search(r"wss?://", line)]
        self.assertEqual(found, [])


class TestCacheSubscribe(PubSubWSTestCase):
    def setUp(self):
        super().setUp()
        self.client = LocalitasClient(f"http://127.0.0.1:{self.server.server_address[1]}", "t")

    def test_subscribe_opens_the_cache_socket_with_the_client_token(self):
        got = queue.Queue()
        ref = self.client.cache("my cache")
        ws = ref.subscribe("events", "worker-1", got.put)
        self.addCleanup(ws.close)

        self.assertIsInstance(ws, PubSubWS)
        self.assertEqual(got.get(timeout=5), {"seq": 1, "value": "hello", "channel": "events"})
        headers = _CacheWSHandler.headers.get(timeout=5)
        self.assertEqual(headers[":path"].partition("?")[0], "/apps/cache/ws/my%20cache")
        self.assertEqual(headers["authorization"], "Bearer t")

    def test_watches_reuse_the_ref_socket(self):
        after, latest = queue.Queue(), queue.Queue()
        ref = self.client.cache("test")
        ws = ref.watch("runs", 41, after.put)
        self.addCleanup(ws.close)
        self.assertEqual(after.get(timeout=5), {"seq": 42, "value": "next", "channel": "runs"})

        self.assertIs(ref.watch_latest("news", latest.put), ws)
        self.assertEqual(latest.get(timeout=5), {"seq": 6, "value": "next", "channel": "news"})
        self.assertEqual(_CacheWSHandler.headers.qsize(), 1)


if __name__ == "__main__":
    unittest.main()
