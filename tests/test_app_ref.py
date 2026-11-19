"""client.app(name) calls another app by name through core, at
/apps/ext/<name><path>, with the client's token."""

import unittest

from localitas_client import APIError, AppRef, LocalitasClient
from tests.local_core import LocalCore


class TestAppRef(unittest.TestCase):
    def setUp(self):
        self.core = LocalCore({
            "GET /apps/ext/weather/api/forecast": (200, {"city": "Paris"}),
            "POST /apps/ext/weather/api/sync": (200, {"synced": 3}),
            "PUT /apps/ext/weather/api/cities/paris": (200, None),
            "DELETE /apps/ext/weather/api/cities/paris": (204, None),
            "GET /apps/ext/weather/api/broken": (500, {"error": "boom"}),
        })
        self.addCleanup(self.core.close)
        self.weather = LocalitasClient(self.core.url, "lt_app").app("weather")

    def test_calls_the_app_by_name_with_the_client_token(self):
        self.assertIsInstance(self.weather, AppRef)
        self.assertEqual(self.weather.get("/api/forecast?days=2"), {"city": "Paris"})
        call = self.core.next_call()
        self.assertEqual((call.method, call.path, call.query), ("GET", "/apps/ext/weather/api/forecast", "days=2"))
        self.assertEqual(call.headers["authorization"], "Bearer lt_app")

    def test_sends_json_bodies_and_decodes_answers(self):
        self.assertEqual(self.weather.post("/api/sync", {"full": True}), {"synced": 3})
        call = self.core.next_call()
        self.assertEqual((call.body, call.headers["content-type"]), ({"full": True}, "application/json"))
        self.assertIsNone(self.weather.put("/api/cities/paris", {"name": "Paris"}))
        self.assertEqual(self.core.next_call().body, {"name": "Paris"})
        self.assertIsNone(self.weather.delete("/api/cities/paris"))
        self.assertEqual(self.core.next_call().method, "DELETE")

    def test_error_status_raises_api_error(self):
        with self.assertRaises(APIError) as ctx:
            self.weather.get("/api/broken")
        self.assertEqual(ctx.exception.status_code, 500)

    def test_refuses_invalid_names(self):
        client = LocalitasClient(self.core.url, "lt_app")
        for name in ("", "Weather", "-x", "a/b", "a.b", "../core", "a b"):
            with self.subTest(name=name), self.assertRaises(ValueError):
                client.app(name)

    def test_refuses_anything_but_a_path(self):
        for path in ("", "api/x", "//evil.example/x", "http://evil.example/x", "/a\\b"):
            with self.subTest(path=path), self.assertRaises(ValueError):
                self.weather.get(path)
        self.assertTrue(self.core.calls.empty())


if __name__ == "__main__":
    unittest.main()
