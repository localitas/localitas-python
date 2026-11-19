"""A real local HTTP server standing in for core in unit tests: it records
every call (method, path, headers, JSON body) and answers each "METHOD /path"
route with a status and a JSON body, or with a function of the call."""

import json
import queue
import threading
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Union


@dataclass
class Call:
    method: str
    path: str
    query: str
    headers: dict
    body: Any


Reply = tuple[int, Any]
Route = Union[Reply, Callable[[Call], Reply]]


class LocalCore:
    def __init__(self, routes: dict[str, Route] | None = None):
        self.routes: dict[str, Route] = dict(routes or {})
        self.calls: "queue.Queue[Call]" = queue.Queue()
        core = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                core._serve(self)

            def do_POST(self):
                core._serve(self)

            def do_PUT(self):
                core._serve(self)

            def do_DELETE(self):
                core._serve(self)

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()

    def next_call(self, timeout: float = 5) -> Call:
        return self.calls.get(timeout=timeout)

    def _serve(self, h: BaseHTTPRequestHandler):
        path, _, query = h.path.partition("?")
        length = int(h.headers.get("Content-Length", 0))
        raw = h.rfile.read(length) if length else b""
        call = Call(h.command, path, query, {k.lower(): v for k, v in h.headers.items()},
                    json.loads(raw) if raw else None)
        route = self.routes.get(f"{h.command} {path}", (404, {"error": "not found"}))
        status, body = route(call) if callable(route) else route
        self.calls.put(call)
        data = b"" if body is None else json.dumps(body).encode()
        h.send_response(status)
        h.send_header("Content-Type", "application/json")
        h.send_header("Content-Length", str(len(data)))
        h.end_headers()
        h.wfile.write(data)
