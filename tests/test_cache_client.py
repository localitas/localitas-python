"""Unit tests for the cache client against a fake that answers in core's
wire shape: data endpoints wrap their payload as {"error": "", "result": ...},
misses are 404s carrying that envelope, an unknown cache is a plain-text 404,
and create/delete cache answer unwrapped JSON."""

import json
import threading
import unittest
import urllib.parse
from http.server import BaseHTTPRequestHandler, HTTPServer

from localitas_client import LocalitasClient
from localitas_client.client import APIError

BASE = "/apps/cache/api/caches/test"


def ok(result) -> tuple[int, str]:
    return 200, json.dumps({"error": "", "result": result})


def miss(msg: str) -> tuple[int, str]:
    return 404, json.dumps({"error": msg, "result": None})


def err(status: int, msg: str) -> tuple[int, str]:
    return status, json.dumps({"error": msg, "result": None})


UNKNOWN_CACHE = (404, 'cache "test" not found')


class _CoreCacheFake(BaseHTTPRequestHandler):
    routes: dict = {}
    calls: list = []

    def do_GET(self):
        self._handle()

    def do_POST(self):
        self._handle()

    def do_PUT(self):
        self._handle()

    def do_DELETE(self):
        self._handle()

    def _handle(self):
        raw_path, _, query = self.path.partition("?")
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length).decode() if length else ""
        self.calls.append({"method": self.command, "path": raw_path, "query": query, "body": body})
        status, payload = self.routes.get(f"{self.command} {raw_path}", (404, "404 page not found"))
        self.send_response(status)
        self.send_header("Content-Type", "application/json" if payload.startswith("{") else "text/plain")
        self.end_headers()
        self.wfile.write(payload.encode())

    def log_message(self, *args):
        pass


class CacheClientTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = HTTPServer(("127.0.0.1", 0), _CoreCacheFake)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.client = LocalitasClient(f"http://127.0.0.1:{cls.server.server_address[1]}").with_token("t")

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()

    def setUp(self):
        _CoreCacheFake.routes = {}
        _CoreCacheFake.calls = []
        self.cache = self.client.cache("test")

    def route(self, method: str, path: str, reply: tuple[int, str]):
        _CoreCacheFake.routes[f"{method} {path}"] = reply

    def last(self) -> dict:
        return _CoreCacheFake.calls[-1]

    def assert_status(self, ctx, status: int):
        self.assertEqual(ctx.exception.status_code, status, ctx.exception.body)


class TestCacheKV(CacheClientTestCase):
    def test_kv_methods_unwrap_the_result(self):
        self.route("GET", f"{BASE}/keys/k1", ok({"key": "k1", "value": "v1", "ttl_remaining": 300}))
        self.route("PUT", f"{BASE}/keys/k1", (201, json.dumps({"error": "", "result": {"status": "ok"}})))
        self.route("DELETE", f"{BASE}/keys/k1", ok({"status": "deleted"}))
        self.route("POST", f"{BASE}/keys/incr/counter", ok({"key": "counter", "value": 4}))
        self.route("POST", f"{BASE}/keys/incrttl/rate", ok({"key": "rate", "value": 3}))
        self.route("POST", f"{BASE}/keys/setnx/lock", ok({"acquired": True, "key": "lock"}))
        self.route("GET", f"{BASE}/keys", ok({"keys": ["user:1", "user:2"]}))
        self.route("POST", f"{BASE}/flush", ok({"status": "flushed"}))
        stats = {"name": "test", "hits": 1, "misses": 1, "sets": 3, "deletes": 0, "evictions": 0, "key_count": 3, "hit_rate": 50}
        self.route("GET", f"{BASE}/stats", ok(stats))

        self.assertEqual(self.cache.get("k1"), "v1")
        self.cache.set("k1", "v1", ttl=300)
        self.assertEqual(json.loads(self.last()["body"]), {"value": "v1", "ttl": 300})
        self.cache.delete("k1")
        self.assertEqual(self.cache.incr("counter", delta=0), 4)
        self.assertEqual(json.loads(self.last()["body"]), {"delta": 0})
        self.assertEqual(self.cache.incr_with_ttl("rate", delta=2, ttl=60), 3)
        self.assertTrue(self.cache.set_nx("lock", "owner-a", ttl=60))
        self.assertEqual(self.cache.keys("user:*"), ["user:1", "user:2"])
        self.assertEqual(self.last()["query"], "pattern=user%3A%2A")
        self.cache.flush()
        self.assertEqual(self.cache.stats(), stats)

    def test_keys_are_escaped(self):
        for key in ["q?x=1#frag", "a/b/c", "sp ace%"]:
            escaped = urllib.parse.quote(key, safe="")
            self.route("GET", f"{BASE}/keys/{escaped}", ok({"key": key, "value": "v", "ttl_remaining": -1}))
            self.route("PUT", f"{BASE}/keys/{escaped}", ok({"status": "ok"}))
            self.route("DELETE", f"{BASE}/keys/{escaped}", ok({"status": "deleted"}))
            self.route("POST", f"{BASE}/keys/incr/{escaped}", ok({"key": key, "value": 1}))
            self.route("POST", f"{BASE}/keys/incrttl/{escaped}", ok({"key": key, "value": 1}))
            self.route("POST", f"{BASE}/keys/setnx/{escaped}", ok({"acquired": True, "key": key}))

            self.assertEqual(self.cache.get(key), "v")
            self.cache.set(key, "v")
            self.cache.delete(key)
            self.assertEqual(self.cache.incr(key), 1)
            self.assertEqual(self.cache.incr_with_ttl(key), 1)
            self.assertTrue(self.cache.set_nx(key, "v"))
            for call in _CoreCacheFake.calls[-6:]:
                self.assertEqual(urllib.parse.unquote(call["path"].rsplit("/", 1)[1]), key)

    def test_absent_key_is_none(self):
        self.route("GET", f"{BASE}/keys/absent", miss("key not found"))
        self.assertIsNone(self.cache.get("absent"))

    def test_unknown_cache_raises(self):
        self.route("GET", f"{BASE}/keys/k", UNKNOWN_CACHE)
        self.route("PUT", f"{BASE}/keys/k", UNKNOWN_CACHE)
        with self.assertRaises(APIError) as ctx:
            self.cache.get("k")
        self.assert_status(ctx, 404)
        with self.assertRaises(APIError) as ctx:
            self.cache.set("k", "v")
        self.assert_status(ctx, 404)

    def test_incr_non_integer_raises(self):
        self.route("POST", f"{BASE}/keys/incr/word", err(400, "value is not an integer"))
        with self.assertRaises(APIError) as ctx:
            self.cache.incr("word")
        self.assert_status(ctx, 400)

    def test_server_error_on_get_raises(self):
        self.route("GET", f"{BASE}/keys/k", err(500, "raft: not ready"))
        with self.assertRaises(APIError) as ctx:
            self.cache.get("k")
        self.assert_status(ctx, 500)

    def test_response_without_envelope_raises(self):
        self.route("GET", f"{BASE}/keys/k", (200, json.dumps({"key": "k", "value": "v"})))
        with self.assertRaises(ValueError):
            self.cache.get("k")


class TestCacheManagement(CacheClientTestCase):
    def test_list_caches_unwraps_the_result(self):
        caches = [{"hit_rate": 0, "key_count": 0, "name": "localitas_public_http_paths"},
                  {"hit_rate": 50, "key_count": 3, "name": "sessions"}]
        self.route("GET", "/apps/cache/api/caches", ok({"caches": caches}))
        self.assertEqual(self.client.list_caches(), caches)

    def test_create_and_delete_cache(self):
        self.route("POST", "/apps/cache/api/caches", (201, json.dumps({"created_at": 1, "name": "new"})))
        self.route("DELETE", "/apps/cache/api/caches/a%2Fb", (200, json.dumps({"status": "deleted"})))
        self.client.create_cache("new")
        self.assertEqual(json.loads(self.last()["body"]), {"name": "new"})
        self.client.delete_cache("a/b")

    def test_management_failures_raise(self):
        self.route("POST", "/apps/cache/api/caches", (409, "cache already exists"))
        self.route("DELETE", "/apps/cache/api/caches/localitas_public_http_paths", (403, "cannot delete"))
        with self.assertRaises(APIError) as ctx:
            self.client.create_cache("dup")
        self.assert_status(ctx, 409)
        with self.assertRaises(APIError) as ctx:
            self.client.delete_cache("localitas_public_http_paths")
        self.assert_status(ctx, 403)


class TestCacheStructures(CacheClientTestCase):
    def test_sorted_set_unwraps_the_result(self):
        z = f"{BASE}/zsets/board"
        self.route("POST", f"{z}/add", ok({"added": 0}))
        self.route("GET", z, ok({"count": 2, "entries": [{"member": "bob", "score": 150}, {"member": "alice", "score": 300}]}))
        self.route("GET", f"{z}/score/a%2Fb", ok({"score": 150}))
        self.route("GET", f"{z}/rank/alice", ok({"rank": 2}))
        self.route("POST", f"{z}/rem", ok({"removed": 1}))
        self.route("POST", f"{z}/incrby", ok({"member": "carol", "score": 202.5}))
        lb = self.cache.sorted_set("board")

        self.assertEqual(lb.add(("bob", 150)), 0)
        self.assertEqual(lb.range(0, -1), [{"member": "bob", "score": 150}, {"member": "alice", "score": 300}])
        self.assertEqual(lb.score("a/b"), 150)
        self.assertEqual(lb.rank("alice"), 2)
        self.assertEqual(lb.rem("dave", "nobody"), 1)
        self.assertEqual(lb.incr_by("carol", 2.5), 202.5)

    def test_sorted_set_misses_and_failures(self):
        z = f"{BASE}/zsets/board"
        self.route("GET", f"{z}/score/nobody", miss("member not found"))
        self.route("GET", f"{z}/rank/nobody", miss("member not found"))
        self.route("GET", f"{z}/score/bob", err(500, "database is locked"))
        self.route("GET", f"{z}/rank/bob", UNKNOWN_CACHE)
        lb = self.cache.sorted_set("board")

        self.assertIsNone(lb.score("nobody"))
        self.assertEqual(lb.rank("nobody"), -1)
        with self.assertRaises(APIError) as ctx:
            lb.score("bob")
        self.assert_status(ctx, 500)
        with self.assertRaises(APIError) as ctx:
            lb.rank("bob")
        self.assert_status(ctx, 404)

    def test_list_set_hash_unwrap_the_result(self):
        self.route("POST", f"{BASE}/lists/jobs/rpush", ok({"length": 2}))
        self.route("POST", f"{BASE}/lists/jobs/lpush", ok({"length": 3}))
        self.route("GET", f"{BASE}/lists/jobs", ok({"length": 3, "values": ["z", "a", "b"]}))
        self.route("POST", f"{BASE}/lists/jobs/lpop", ok({"value": "z"}))
        self.route("POST", f"{BASE}/lists/jobs/rpop", ok({"value": "b"}))
        self.route("POST", f"{BASE}/sets/tags/add", ok({"added": 2}))
        self.route("POST", f"{BASE}/sets/tags/rem", ok({"removed": 1}))
        self.route("GET", f"{BASE}/sets/tags", ok({"count": 2, "members": ["a", "b"]}))
        self.route("PUT", f"{BASE}/hashes/user%3A1", ok({"status": "ok"}))
        self.route("GET", f"{BASE}/hashes/user%3A1", ok({"count": 1, "fields": {"name": "Alicia"}}))
        self.route("GET", f"{BASE}/hashes/user%3A1/fields/name", ok({"value": "Alicia"}))
        self.route("GET", f"{BASE}/hashes/user%3A1/json", ok({"json": '{"name":"Alicia"}'}))
        l, s, h = self.cache.list("jobs"), self.cache.set_store("tags"), self.cache.hash("user:1")

        self.assertEqual(l.rpush("a", "b"), 2)
        self.assertEqual(l.lpush("z"), 3)
        self.assertEqual(l.range(), ["z", "a", "b"])
        self.assertEqual(l.lpop(), "z")
        self.assertEqual(l.rpop(), "b")
        self.assertEqual(s.add("b", "a", "a"), 2)
        self.assertEqual(s.rem("a", "zz"), 1)
        self.assertEqual(s.members(), ["a", "b"])
        h.set({"name": "Alicia"})
        self.assertEqual(h.get_all(), {"name": "Alicia"})
        self.assertEqual(h.get("name"), "Alicia")
        self.assertEqual(h.to_json(), '{"name":"Alicia"}')

    def test_empty_structures_are_none_and_unknown_cache_raises(self):
        self.route("POST", f"{BASE}/lists/jobs/lpop", miss("list is empty"))
        self.route("POST", f"{BASE}/queues/q/dequeue", miss("queue is empty"))
        self.route("GET", f"{BASE}/stacks/s", miss("stack is empty"))
        self.route("GET", f"{BASE}/hashes/h/fields/nope", miss("field not found"))
        self.route("POST", f"{BASE}/lists/jobs/rpop", UNKNOWN_CACHE)
        self.route("PUT", f"{BASE}/hashes/h/json", err(400, "invalid JSON"))

        self.assertIsNone(self.cache.list("jobs").lpop())
        self.assertIsNone(self.cache.queue("q").dequeue())
        self.assertIsNone(self.cache.stack("s").peek())
        self.assertIsNone(self.cache.hash("h").get("nope"))
        with self.assertRaises(APIError) as ctx:
            self.cache.list("jobs").rpop()
        self.assert_status(ctx, 404)
        with self.assertRaises(APIError) as ctx:
            self.cache.hash("h").from_json("{not json")
        self.assert_status(ctx, 400)

    def test_queue_stack_unwrap_the_result(self):
        self.route("POST", f"{BASE}/queues/q/enqueue", ok({"length": 2}))
        self.route("POST", f"{BASE}/queues/q/dequeue", ok({"value": "2"}))
        self.route("GET", f"{BASE}/queues/q", ok({"length": 1, "value": "3"}))
        self.route("POST", f"{BASE}/stacks/s/push", ok({"length": 2}))
        self.route("POST", f"{BASE}/stacks/s/pop", ok({"value": "c"}))
        self.route("GET", f"{BASE}/stacks/s", ok({"length": 1, "value": "b"}))
        q, s = self.cache.queue("q", max_size=2), self.cache.stack("s", max_size=2)

        self.assertEqual(q.enqueue("3"), 2)
        self.assertEqual(json.loads(self.last()["body"]), {"value": "3", "max_size": 2})
        self.assertEqual(q.dequeue(), "2")
        self.assertEqual(q.peek(), "3")
        self.assertEqual(s.push("c"), 2)
        self.assertEqual(s.pop(), "c")
        self.assertEqual(s.peek(), "b")

    def test_pubsub_unwraps_the_result(self):
        ps = f"{BASE}/pubsub/events"
        msgs = [{"seq": 1, "value": "first", "created_at": 100}]
        self.route("POST", f"{ps}/publish", ok({"seq": 7}))
        self.route("GET", f"{ps}/read", ok({"count": 1, "messages": msgs}))
        self.route("POST", f"{ps}/group/workers", (201, json.dumps({"error": "", "result": {"status": "created"}})))
        self.route("POST", f"{ps}/group/workers/claim", ok({"message": {"seq": 2, "value": "job-1", "created_at": 102}}))
        self.route("POST", f"{ps}/group/idle/claim", ok({"message": None}))
        self.route("POST", f"{ps}/group/workers/ack", ok({"status": "acked"}))
        self.route("POST", f"{ps}/group/nope/claim", miss('not found: group "nope" on channel "events"'))
        ch = self.cache.pubsub("events", max_size=2)

        self.assertEqual(ch.publish("first"), 7)
        self.assertEqual(json.loads(self.last()["body"]), {"value": "first", "max_size": 2})
        self.assertEqual(ch.read("reader 1", count=10), msgs)
        self.assertEqual(self.last()["query"], "consumer=reader%201&count=10")
        ch.create_group("workers")
        self.assertEqual(ch.claim("workers", "w1"), {"seq": 2, "value": "job-1", "created_at": 102})
        self.assertIsNone(ch.claim("idle", "w1"))
        ch.ack("workers", 2)
        with self.assertRaises(APIError) as ctx:
            ch.claim("nope", "w1")
        self.assert_status(ctx, 404)


if __name__ == "__main__":
    unittest.main()
