"""run_async echoes the Idempotency-Key core put on an async call into the
completion it posts, so core restricts the completion to that try; the
run-ID-only form posts none."""

import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.request import Request, urlopen

from localitas_client import AUTOMATION_RUN_ID_HEADER, LocalitasClient
from tests.local_core import LocalCore

COMPLETE = "POST /apps/automation/api/runs/r1/complete"


class TestRunAsync(unittest.TestCase):
    def setUp(self):
        self.core = LocalCore({COMPLETE: (200, {"ok": True})})
        self.addCleanup(self.core.close)
        self.automation = LocalitasClient(self.core.url, "lt_app").automation()

    def completion(self):
        call = self.core.next_call()
        self.assertEqual(f"{call.method} {call.path}", COMPLETE)
        return call.body

    def test_echoes_the_key_of_the_request_it_serves(self):
        automation = self.automation

        class App(BaseHTTPRequestHandler):
            def do_POST(self):
                started = automation.run_async(self, lambda: {"synced": 3})
                self.send_response(202 if started else 200)
                self.end_headers()

            def log_message(self, *args):
                pass

        app = HTTPServer(("127.0.0.1", 0), App)
        threading.Thread(target=app.serve_forever, daemon=True).start()
        self.addCleanup(app.server_close)
        self.addCleanup(app.shutdown)

        req = Request(f"http://127.0.0.1:{app.server_address[1]}/api/sync", data=b"{}", method="POST",
                      headers={AUTOMATION_RUN_ID_HEADER: "r1", "Idempotency-Key": "r1/fetch/2"})
        with urlopen(req, timeout=5) as resp:
            self.assertEqual(resp.status, 202)
        body = self.completion()
        self.assertEqual(body["idempotency_key"], "r1/fetch/2")
        self.assertEqual((body["status"], body["result"]["synced"]), ("completed", 3))

    def test_reads_a_plain_dict_case_insensitively(self):
        headers = {"localitas-automation-run-id": "r1", "idempotency-key": "r1/report/1"}
        self.assertTrue(self.automation.run_async(headers, lambda: None))
        self.assertEqual(self.completion()["idempotency_key"], "r1/report/1")

    def test_run_id_alone_posts_no_key(self):
        self.assertTrue(self.automation.run_async("r1", lambda: None))
        self.assertNotIn("idempotency_key", self.completion())

    def test_headers_without_a_run_are_not_async(self):
        self.assertFalse(self.automation.run_async({"Idempotency-Key": "x"}, lambda: None))
        self.assertFalse(self.automation.run_async("", lambda: None))
        self.assertTrue(self.core.calls.empty())

    def test_publishes_a_list_result(self):
        self.assertTrue(self.automation.run_async("r1", lambda: ["inbox", "sent"]))
        self.assertEqual(self.completion(), {"status": "completed", "result": ["inbox", "sent"], "error": ""})

    def test_publish_result_keeps_an_empty_list(self):
        self.automation.publish_result("r1", "completed", [], "")
        self.assertEqual(self.completion(), {"status": "completed", "result": [], "error": ""})

    def test_publish_result_sends_the_key_when_given(self):
        self.automation.publish_result("r1", "failed", None, "boom", idempotency_key="r1/fetch/1")
        self.assertEqual(self.completion(), {"status": "failed", "result": {}, "error": "boom",
                                             "idempotency_key": "r1/fetch/1"})


if __name__ == "__main__":
    unittest.main()
