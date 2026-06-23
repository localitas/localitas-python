"""Integration tests for cache client. Requires integration cluster running.

Run: make integration-cluster-start && python -m pytest tests/test_cache_integration.py -v
"""
import base64
import json
import time
import unittest

from localitas_client import LocalitasClient

INTEG_URL = "http://localhost:9090"
INTEG_USER = {
    "user_id": "11111111-1111-1111-1111-111111111111",
    "email": "alice@test.local",
    "name": "Alice Admin",
}
INTEG_TOKEN = base64.b64encode(json.dumps(INTEG_USER).encode()).decode()


def make_client():
    return LocalitasClient(INTEG_URL).with_token(INTEG_TOKEN)


def unique_name(prefix="integ"):
    return f"{prefix}_{int(time.time() * 1000000)}"


class TestCacheKV(unittest.TestCase):
    def setUp(self):
        self.client = make_client()
        self.cache_name = unique_name("kv")
        self.client.create_cache(self.cache_name)
        self.cache = self.client.cache(self.cache_name)

    def tearDown(self):
        self.client.delete_cache(self.cache_name)

    def test_set_and_get(self):
        self.cache.set("greeting", "hello world", ttl=300)
        val = self.cache.get("greeting")
        self.assertEqual(val, "hello world")

    def test_get_miss(self):
        val = self.cache.get("nonexistent")
        self.assertIsNone(val)

    def test_delete(self):
        self.cache.set("k", "v")
        self.cache.delete("k")
        self.assertIsNone(self.cache.get("k"))

    def test_incr(self):
        self.assertEqual(self.cache.incr("counter"), 1)
        self.assertEqual(self.cache.incr("counter"), 2)
        self.assertEqual(self.cache.incr("counter", delta=10), 12)

    def test_incr_with_ttl(self):
        count = self.cache.incr_with_ttl("rate", delta=1, ttl=60)
        self.assertEqual(count, 1)
        count = self.cache.incr_with_ttl("rate", delta=1, ttl=60)
        self.assertEqual(count, 2)

    def test_set_nx(self):
        self.assertTrue(self.cache.set_nx("lock", "owner1", ttl=60))
        self.assertFalse(self.cache.set_nx("lock", "owner2", ttl=60))
        self.assertEqual(self.cache.get("lock"), "owner1")

    def test_keys(self):
        self.cache.set("user:1", "a")
        self.cache.set("user:2", "b")
        self.cache.set("config:x", "c")
        keys = self.cache.keys("user:*")
        self.assertEqual(len(keys), 2)

    def test_stats(self):
        self.cache.set("k", "v")
        self.cache.get("k")
        self.cache.get("miss")
        stats = self.cache.stats()
        self.assertGreaterEqual(stats["hits"], 1)
        self.assertGreaterEqual(stats["misses"], 1)


class TestCacheList(unittest.TestCase):
    def setUp(self):
        self.client = make_client()
        self.cache_name = unique_name("list")
        self.client.create_cache(self.cache_name)
        self.cache = self.client.cache(self.cache_name)

    def tearDown(self):
        self.client.delete_cache(self.cache_name)

    def test_push_and_range(self):
        lst = self.cache.list("q")
        lst.rpush("a", "b", "c")
        lst.lpush("z")
        items = lst.range(0, -1)
        self.assertEqual(items, ["z", "a", "b", "c"])

    def test_pop(self):
        lst = self.cache.list("q")
        lst.rpush("a", "b", "c")
        self.assertEqual(lst.lpop(), "a")
        self.assertEqual(lst.rpop(), "c")

    def test_empty_pop(self):
        lst = self.cache.list("empty")
        self.assertIsNone(lst.lpop())


class TestCacheSet(unittest.TestCase):
    def setUp(self):
        self.client = make_client()
        self.cache_name = unique_name("set")
        self.client.create_cache(self.cache_name)
        self.cache = self.client.cache(self.cache_name)

    def tearDown(self):
        self.client.delete_cache(self.cache_name)

    def test_add_and_members(self):
        s = self.cache.set_store("tags")
        added = s.add("go", "rust", "python", "go")
        self.assertEqual(added, 3)
        members = s.members()
        self.assertEqual(sorted(members), ["go", "python", "rust"])

    def test_rem(self):
        s = self.cache.set_store("s")
        s.add("a", "b", "c")
        removed = s.rem("b", "missing")
        self.assertEqual(removed, 1)


class TestCacheHash(unittest.TestCase):
    def setUp(self):
        self.client = make_client()
        self.cache_name = unique_name("hash")
        self.client.create_cache(self.cache_name)
        self.cache = self.client.cache(self.cache_name)

    def tearDown(self):
        self.client.delete_cache(self.cache_name)

    def test_set_and_get(self):
        h = self.cache.hash("user")
        h.set({"name": "Alice", "email": "alice@test.com"})
        self.assertEqual(h.get("name"), "Alice")
        all_fields = h.get_all()
        self.assertEqual(len(all_fields), 2)

    def test_to_json(self):
        h = self.cache.hash("user")
        h.set({"name": "Bob"})
        j = h.to_json()
        parsed = json.loads(j)
        self.assertEqual(parsed["name"], "Bob")

    def test_from_json(self):
        h = self.cache.hash("user")
        h.from_json('{"city": "NYC", "age": "30"}')
        self.assertEqual(h.get("city"), "NYC")


class TestCacheSortedSet(unittest.TestCase):
    def setUp(self):
        self.client = make_client()
        self.cache_name = unique_name("zset")
        self.client.create_cache(self.cache_name)
        self.cache = self.client.cache(self.cache_name)

    def tearDown(self):
        self.client.delete_cache(self.cache_name)

    def test_add_and_range(self):
        lb = self.cache.sorted_set("lb")
        lb.add(("alice", 100), ("bob", 200), ("charlie", 50))
        entries = lb.range(0, -1)
        self.assertEqual(len(entries), 3)
        self.assertEqual(entries[0]["member"], "charlie")

    def test_score_and_rank(self):
        lb = self.cache.sorted_set("lb")
        lb.add(("alice", 100), ("bob", 200))
        self.assertEqual(lb.score("alice"), 100)
        self.assertEqual(lb.rank("bob"), 1)

    def test_incr_by(self):
        lb = self.cache.sorted_set("lb")
        lb.add(("alice", 100))
        new_score = lb.incr_by("alice", 50)
        self.assertEqual(new_score, 150)


class TestCacheQueue(unittest.TestCase):
    def setUp(self):
        self.client = make_client()
        self.cache_name = unique_name("queue")
        self.client.create_cache(self.cache_name)
        self.cache = self.client.cache(self.cache_name)

    def tearDown(self):
        self.client.delete_cache(self.cache_name)

    def test_fifo(self):
        q = self.cache.queue("jobs", max_size=0)
        q.enqueue("first")
        q.enqueue("second")
        self.assertEqual(q.dequeue(), "first")
        self.assertEqual(q.peek(), "second")

    def test_bounded(self):
        q = self.cache.queue("bounded", max_size=3)
        for i in range(5):
            q.enqueue(f"item{i}")
        self.assertEqual(q.dequeue(), "item2")


class TestCacheStack(unittest.TestCase):
    def setUp(self):
        self.client = make_client()
        self.cache_name = unique_name("stack")
        self.client.create_cache(self.cache_name)
        self.cache = self.client.cache(self.cache_name)

    def tearDown(self):
        self.client.delete_cache(self.cache_name)

    def test_lifo(self):
        s = self.cache.stack("undo")
        s.push("action1")
        s.push("action2")
        self.assertEqual(s.pop(), "action2")
        self.assertEqual(s.peek(), "action1")


class TestCachePubSub(unittest.TestCase):
    def setUp(self):
        self.client = make_client()
        self.cache_name = unique_name("pubsub")
        self.client.create_cache(self.cache_name)
        self.cache = self.client.cache(self.cache_name)

    def tearDown(self):
        self.client.delete_cache(self.cache_name)

    def test_publish_and_read(self):
        ch = self.cache.pubsub("events", max_size=100)
        seq = ch.publish('{"type": "test"}')
        self.assertGreater(seq, 0)

        msgs = ch.read("consumer-1", count=10)
        self.assertEqual(len(msgs), 1)
        self.assertEqual(msgs[0]["value"], '{"type": "test"}')

    def test_cursor_advances(self):
        ch = self.cache.pubsub("events")
        ch.publish("msg1")
        ch.publish("msg2")

        msgs1 = ch.read("c1", count=10)
        self.assertEqual(len(msgs1), 2)

        ch.publish("msg3")
        msgs2 = ch.read("c1", count=10)
        self.assertEqual(len(msgs2), 1)

    def test_consumer_group(self):
        ch = self.cache.pubsub("jobs")
        ch.create_group("workers")

        ch.publish("job1")
        ch.publish("job2")

        msg = ch.claim("workers", "w1")
        self.assertIsNotNone(msg)
        self.assertEqual(msg["value"], "job1")

        ch.ack("workers", msg["seq"])


class TestCacheWebSocket(unittest.TestCase):
    """WebSocket pubsub tests. Requires: pip install websocket-client"""

    def setUp(self):
        self.client = make_client()
        self.cache_name = unique_name("ws")
        self.client.create_cache(self.cache_name)

    def tearDown(self):
        self.client.delete_cache(self.cache_name)

    def test_websocket_subscribe_and_publish(self):
        try:
            import websocket
        except ImportError:
            self.skipTest("websocket-client not installed")

        ws_url = f"ws://localhost:9090/apps/cache/ws/{self.cache_name}?token={INTEG_TOKEN}"
        ws = websocket.create_connection(ws_url, timeout=5)

        connected = json.loads(ws.recv())
        self.assertEqual(connected["type"], "connected")

        ws.send(json.dumps({
            "action": "subscribe",
            "channel": "test-ws",
            "consumer": "py-test",
        }))
        sub_resp = json.loads(ws.recv())
        self.assertEqual(sub_resp["type"], "subscribed")

        ws.send(json.dumps({
            "action": "publish",
            "channel": "test-ws",
            "value": '{"hello":"from-python"}',
        }))
        pub_resp = json.loads(ws.recv())
        self.assertEqual(pub_resp["type"], "published")

        msg = json.loads(ws.recv())
        self.assertEqual(msg["type"], "message")
        self.assertEqual(msg["value"], '{"hello":"from-python"}')

        ws.send(json.dumps({"action": "unsubscribe", "channel": "test-ws"}))
        unsub = json.loads(ws.recv())
        self.assertEqual(unsub["type"], "unsubscribed")

        ws.close()


if __name__ == "__main__":
    unittest.main()
