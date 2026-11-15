"""PubSubWS against a real WebSocket server in the test, speaking core's
cache WebSocket protocol: a "connected" greeting, then JSON actions in and
JSON events out."""

import base64
import hashlib
import json
import queue
import socket
import socketserver
import struct
import threading
import unittest

from localitas_client.client import PubSubWS

_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


def _read_exact(sock: socket.socket, n: int) -> bytes:
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("closed")
        buf += chunk
    return buf


def _read_frame(sock: socket.socket) -> tuple[int, bytes]:
    b0, b1 = _read_exact(sock, 2)
    length = b1 & 0x7F
    if length == 126:
        length = struct.unpack(">H", _read_exact(sock, 2))[0]
    elif length == 127:
        length = struct.unpack(">Q", _read_exact(sock, 8))[0]
    mask = _read_exact(sock, 4) if b1 & 0x80 else b"\0\0\0\0"
    payload = bytes(b ^ mask[i % 4] for i, b in enumerate(_read_exact(sock, length)))
    return b0 & 0x0F, payload


def _send_json(sock: socket.socket, event: dict):
    payload = json.dumps(event).encode()
    header = bytes([0x81, len(payload)]) if len(payload) < 126 else bytes([0x81, 126]) + struct.pack(">H", len(payload))
    sock.sendall(header + payload)


class _CacheWSHandler(socketserver.BaseRequestHandler):
    """Answers subscribe and watch the way core's cache app does: a
    "subscribed" event (carrying the start point for a watch), then the
    channel's next message."""

    received: "queue.Queue[dict]"
    headers: "queue.Queue[dict]"

    def handle(self):
        sock = self.request
        raw = b""
        while b"\r\n\r\n" not in raw:
            raw += sock.recv(4096)
        lines = raw.decode().split("\r\n")
        headers = {k.strip().lower(): v.strip() for k, _, v in (l.partition(":") for l in lines[1:] if l)}
        self.headers.put(headers)
        accept = base64.b64encode(hashlib.sha1((headers["sec-websocket-key"] + _GUID).encode()).digest()).decode()
        sock.sendall(("HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                      f"Sec-WebSocket-Accept: {accept}\r\n\r\n").encode())
        _send_json(sock, {"type": "connected", "data": {"cache": "test"}})
        while True:
            try:
                opcode, payload = _read_frame(sock)
            except (ConnectionError, OSError):
                return
            if opcode == 8:
                sock.sendall(b"\x88\x00")
                return
            if opcode != 1:
                continue
            action = json.loads(payload)
            self.received.put(action)
            channel = action.get("channel", "")
            if action["action"] == "subscribe":
                _send_json(sock, {"type": "subscribed", "channel": channel})
                _send_json(sock, {"type": "message", "channel": channel, "seq": 1, "value": "hello"})
            elif action["action"] == "watch":
                after = 5 if action.get("from_latest") else action.get("seq", 0)
                _send_json(sock, {"type": "subscribed", "channel": channel, "seq": after})
                _send_json(sock, {"type": "message", "channel": channel, "seq": after + 1, "value": "next"})


class PubSubWSTestCase(unittest.TestCase):
    def setUp(self):
        _CacheWSHandler.received = queue.Queue()
        _CacheWSHandler.headers = queue.Queue()
        self.server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), _CacheWSHandler)
        self.server.daemon_threads = True
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.url = f"ws://127.0.0.1:{self.server.server_address[1]}/apps/cache/ws/test"
        self.stash: list[dict] = []

    def connect(self, **kwargs) -> PubSubWS:
        ws = PubSubWS(self.url, token="t", **kwargs)
        self.addCleanup(ws.close)
        return ws

    def next_action(self, kind: str, channel: str) -> dict:
        """The first action of a kind on a channel the server got, whatever
        order actions on different channels arrived in."""
        while True:
            for action in self.stash:
                if action["action"] == kind and action.get("channel") == channel:
                    self.stash.remove(action)
                    return action
            self.stash.append(_CacheWSHandler.received.get(timeout=5))


class TestPubSubWSBatchSize(PubSubWSTestCase):
    def test_subscribe_sends_the_default_batch_size(self):
        got = queue.Queue()
        self.connect().subscribe("events", "worker-1", got.put)

        self.assertEqual(self.next_action("subscribe", "events"),
                         {"action": "subscribe", "channel": "events", "consumer": "worker-1", "count": 50})
        self.assertEqual(got.get(timeout=5), {"seq": 1, "value": "hello", "channel": "events"})
        self.assertEqual(_CacheWSHandler.headers.get(timeout=5)["authorization"], "Bearer t")

    def test_watch_and_watch_latest_send_the_batch_size(self):
        after, latest = queue.Queue(), queue.Queue()
        ws = self.connect(batch_size=7)
        ws.watch("runs", 41, after.put)
        ws.watch_latest("news", latest.put)

        self.assertEqual(self.next_action("watch", "runs"),
                         {"action": "watch", "channel": "runs", "seq": 41, "count": 7})
        self.assertEqual(after.get(timeout=5), {"seq": 42, "value": "next", "channel": "runs"})
        self.assertEqual(self.next_action("watch", "news"),
                         {"action": "watch", "channel": "news", "from_latest": True, "count": 7})
        self.assertEqual(latest.get(timeout=5), {"seq": 6, "value": "next", "channel": "news"})


if __name__ == "__main__":
    unittest.main()
