"""Integration test for LocalitasClient.announce. Requires the integration
cluster running (make integration-cluster-start)."""
import json
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from localitas_client import LocalitasClient

INTEG_URL = "http://localhost:9090"
INTEG_TOKEN = "lt_integ_a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6e7f8a9b0c1d2e3f4a5b6c7d8e9f0a1b2"


def _app_server(name: str, ready: threading.Event) -> ThreadingHTTPServer:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if not ready.is_set() or self.path != "/health.json":
                self.send_response(404)
                self.end_headers()
                return
            body = json.dumps({"name": name}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def _listed_url(client: LocalitasClient, name: str) -> str:
    for app in client._do("GET", "/apps/ext") or []:
        if app.get("name") == name:
            return app.get("url", "")
    return ""


class TestAnnounce(unittest.TestCase):
    def test_a_native_app_is_registered_once_it_answers_and_withdrawn(self):
        client = LocalitasClient(INTEG_URL).with_token(INTEG_TOKEN)
        name = f"announce-py-{int(time.time() * 1000000)}"
        ready = threading.Event()
        server = _app_server(name, ready)
        self.addCleanup(server.shutdown)
        port = server.server_address[1]

        announcement = client.announce(port)
        self.addCleanup(announcement.withdraw)

        time.sleep(1.5)
        self.assertEqual(announcement.name, "", "core took the app before it answered health.json")
        ready.set()
        deadline = time.time() + 15
        while announcement.name != name:
            self.assertLess(time.time(), deadline, "the app answering health.json was not registered")
            time.sleep(0.1)
        self.assertEqual(_listed_url(client, name), f"http://localhost:{port}")

        announcement.withdraw()
        self.assertEqual(_listed_url(client, name), "", "withdrawn app still listed")


if __name__ == "__main__":
    unittest.main()
