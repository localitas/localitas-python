"""Localitas Python SDK — mirrors the Go client at /client/client.go."""

import json
import time
import urllib.parse
from pathlib import Path
from typing import Any, Optional
from urllib.request import Request, urlopen
from urllib.error import HTTPError


def default_token() -> str:
    """Read the API token from ~/.localitas/config-core.yaml (core.auth.api_token).
    Returns empty string if not found."""
    config_path = Path.home() / ".localitas" / "config-core.yaml"
    if config_path.exists():
        for line in config_path.read_text().splitlines():
            stripped = line.strip()
            if stripped.startswith("api_token:"):
                val = stripped.removeprefix("api_token:").strip().strip("\"'")
                if val.startswith("lt_"):
                    return val
    return ""


class APIError(Exception):
    def __init__(self, method: str, path: str, status_code: int, body: str):
        self.method = method
        self.path = path
        self.status_code = status_code
        self.body = body
        super().__init__(f"{method} {path}: {status_code} {body}")


class LocalitasClient:
    """Client for the Localitas platform HTTP API.

    Usage:
        client = LocalitasClient("http://localhost:8090")
        authed = client.with_token(bearer_token)
        databases = authed.list_databases()
    """

    def __init__(self, base_url: str, token: str = ""):
        self.base_url = base_url.rstrip("/")
        self.token = token

    def with_token(self, token: str) -> "LocalitasClient":
        return LocalitasClient(self.base_url, token)

    # ── Databases ──────────────────────────────────────────────

    def list_databases(self) -> list[dict]:
        return self._do("GET", "/apps/data/api/databases")

    def create_database(self, name: str, system: bool = False) -> dict:
        body = {"name": name}
        if system:
            body["system"] = True
        return self._do("POST", "/apps/data/api/databases", body)

    def get_database(self, db_id: str) -> dict:
        return self._do("GET", f"/apps/data/api/databases/{_esc(db_id)}")

    def delete_database(self, db_id: str) -> None:
        self._do("DELETE", f"/apps/data/api/databases/{_esc(db_id)}")

    # ── Migrations ─────────────────────────────────────────────

    def list_migrations(self, db_id: str) -> list[dict]:
        return self._do("GET", f"/apps/data/api/databases/{_esc(db_id)}/migrations")

    def apply_migration(self, db_id: str, version: str, description: str, up_sql: str, down_sql: str = "") -> dict:
        return self._do("POST", f"/apps/data/api/databases/{_esc(db_id)}/migrations", {
            "version": version, "description": description, "up_sql": up_sql, "down_sql": down_sql,
        })

    # ── Tables & Rows ──────────────────────────────────────────

    def list_tables(self, db_id: str) -> list[dict]:
        return self._do("GET", f"/apps/data/api/databases/{_esc(db_id)}/tables")

    def insert_row(self, db_id: str, table_id: str, values: dict) -> dict:
        return self._do("POST", f"/apps/data/api/databases/{_esc(db_id)}/tables/{_esc(table_id)}/rows", {"values": values})

    def update_row(self, db_id: str, table_id: str, row_id: str, values: dict) -> None:
        self._do("PUT", f"/apps/data/api/databases/{_esc(db_id)}/tables/{_esc(table_id)}/rows/{_esc(row_id)}", {"values": values})

    def delete_row(self, db_id: str, table_id: str, row_id: str) -> None:
        self._do("DELETE", f"/apps/data/api/databases/{_esc(db_id)}/tables/{_esc(table_id)}/rows/{_esc(row_id)}")

    def list_rows(self, db_id: str, table_id: str, limit: int = 100, offset: int = 0) -> list[dict]:
        return self._do("GET", f"/apps/data/api/databases/{_esc(db_id)}/tables/{_esc(table_id)}/rows?limit={limit}&offset={offset}")

    def get_row(self, db_id: str, table_id: str, row_id: str) -> dict:
        return self._do("GET", f"/apps/data/api/databases/{_esc(db_id)}/tables/{_esc(table_id)}/rows/{_esc(row_id)}")

    # ── Raw SQL ────────────────────────────────────────────────

    def sql_exec(self, db_id: str, sql: str, *args) -> dict:
        return self._do("POST", f"/apps/data/api/databases/{_esc(db_id)}/exec", {"sql": sql, "args": list(args)})

    def sql_query(self, db_id: str, sql: str, *args) -> dict:
        return self._do("POST", f"/apps/data/api/databases/{_esc(db_id)}/query", {"sql": sql, "args": list(args)})

    def sql_transaction(self, db_id: str, statements: list[dict]) -> dict:
        return self._do("POST", f"/apps/data/api/databases/{_esc(db_id)}/exec", {"statements": statements})

    # ── Search ─────────────────────────────────────────────────

    def search_fts(self, query: str, limit: int = 100, database_id: str = "") -> dict:
        path = f"/apps/data/api/search?q={_esc(query)}&limit={limit}"
        if database_id:
            path += f"&database_id={_esc(database_id)}"
        return self._do("GET", path)

    def search_hybrid(self, query: str, limit: int = 100, database_id: str = "") -> dict:
        body: dict[str, Any] = {"q": query, "limit": limit}
        if database_id:
            body["database_id"] = database_id
        return self._do("POST", "/apps/data/api/search/hybrid", body)

    # ── Service Registry ───────────────────────────────────────

    def register_service(self, name: str, url: str) -> None:
        db = self.create_database("service_registry", system=True)
        self.apply_migration(db["id"], "20260424-000000-000-init", "service registry table",
            "CREATE TABLE IF NOT EXISTS services (name TEXT PRIMARY KEY, url TEXT NOT NULL, updated_at INTEGER NOT NULL)",
            "DROP TABLE IF EXISTS services")
        self.sql_exec(db["id"],
            "INSERT INTO services (name, url, updated_at) VALUES (?, ?, ?) ON CONFLICT(name) DO UPDATE SET url = excluded.url, updated_at = excluded.updated_at",
            name, url, int(time.time()))

    def discover_service(self, name: str) -> str:
        db = self.create_database("service_registry", system=True)
        result = self.sql_query(db["id"], "SELECT url FROM services WHERE name = ?", name)
        if not result.get("rows"):
            raise APIError("GET", f"/services/{name}", 404, f"service {name!r} not found")
        return result["rows"][0][0]

    # ── Permissions ────────────────────────────────────────────

    def set_resource_owner(self, app: str, resource_type: str, resource_id: str, owner_id: str) -> None:
        self._do("POST", "/api/permissions/set-owner", {
            "app": app, "resource_type": resource_type, "resource_id": resource_id, "owner_id": owner_id,
        })

    def check_permission(self, app: str, resource_type: str, resource_id: str, user_id: str = "") -> str:
        body: dict[str, str] = {"app": app, "resource_type": resource_type, "resource_id": resource_id}
        if user_id:
            body["user_id"] = user_id
        result = self._do("POST", "/api/permissions/check", body)
        return result.get("permission", "")

    def list_resource_members(self, app: str, resource_type: str, resource_id: str) -> list[dict]:
        path = f"/api/permissions/{_esc(app)}/{_esc(resource_type)}/{_esc(resource_id)}/members"
        result = self._do("GET", path)
        return result.get("members", [])

    def add_resource_member(self, app: str, resource_type: str, resource_id: str, user_id: str = "", group_id: str = "", permission: str = "read") -> None:
        path = f"/api/permissions/{_esc(app)}/{_esc(resource_type)}/{_esc(resource_id)}/members"
        self._do("POST", path, {"user_id": user_id, "group_id": group_id, "permission": permission})

    def remove_resource_member(self, app: str, resource_type: str, resource_id: str, user_id: str = "", group_id: str = "") -> None:
        path = f"/api/permissions/{_esc(app)}/{_esc(resource_type)}/{_esc(resource_id)}/members"
        self._do("DELETE", path, {"user_id": user_id, "group_id": group_id})

    # ── Vault ──────────────────────────────────────────────────

    def vault_list_credentials(self) -> list[dict]:
        result = self._do("GET", "/apps/vault/api/credentials")
        return result.get("credentials", [])

    def vault_get_secrets(self, public_id: str) -> dict[str, str]:
        return self._do("GET", f"/apps/vault/api/credentials/{_esc(public_id)}/secrets")

    # ── Cache ──────────────────────────────────────────────────

    def create_cache(self, name: str) -> None:
        """Create a named in-memory cache."""
        self._do("POST", "/apps/cache/api/caches", {"name": name})

    def list_caches(self) -> list[dict]:
        """List all named caches."""
        result = self._do("GET", "/apps/cache/api/caches")
        return result.get("caches", [])

    def delete_cache(self, name: str) -> None:
        """Delete a named cache. Cannot delete 'public_paths'."""
        self._do("DELETE", f"/apps/cache/api/caches/{_esc(name)}")

    def cache(self, name: str) -> "CacheRef":
        """Return a CacheRef for key-value and data structure operations.

        Usage:
            sessions = client.cache("sessions")
            sessions.set("user:abc", '{"name":"Alice"}', ttl=1800)
            val = sessions.get("user:abc")
        """
        return CacheRef(self, name)

    # ── Transport ──────────────────────────────────────────────

    def _do(self, method: str, path: str, body: Any = None) -> Any:
        url = self.base_url + path
        data = None
        if body is not None:
            data = json.dumps(body).encode()

        req = Request(url, data=data, method=method)
        if self.token:
            req.add_header("Authorization", f"Bearer {self.token}")
        if data is not None:
            req.add_header("Content-Type", "application/json")

        try:
            with urlopen(req, timeout=30) as resp:
                resp_body = resp.read().decode()
                if resp_body:
                    return json.loads(resp_body)
                return None
        except HTTPError as e:
            raise APIError(method, path, e.code, e.read().decode()) from None


def _esc(s: str) -> str:
    return urllib.parse.quote(str(s), safe="")


class CacheRef:
    """Reference to a named cache. Provides Redis-like key-value operations
    and typed data structure accessors.

    Example::

        client = LocalitasClient("http://localhost:8090").with_token(token)
        cache = client.cache("sessions")

        # Key-value with TTL (30 min)
        cache.set("user:abc", '{"name":"Alice"}', ttl=1800)
        val = cache.get("user:abc")  # '{"name":"Alice"}'
        cache.delete("user:abc")

        # Rate limiting (atomic incr + TTL on first call)
        count = cache.incr_with_ttl("rate:ip:1.2.3.4", delta=1, ttl=60)
        if count > 100:
            raise Exception("rate limited")

        # Distributed lock
        acquired = cache.set_nx("lock:resource", "owner-1", ttl=30)

        # Data structures
        recent = cache.list("recent_searches")
        recent.rpush("golang", "sqlite")
        recent.range(0, 9)  # last 10

        tags = cache.set_store("article:tags")
        tags.add("python", "go", "rust")

        user = cache.hash("user:123")
        user.set({"name": "Alice", "email": "alice@example.com"})
        user.to_json()  # '{"email":"alice@...","name":"Alice"}'

        lb = cache.sorted_set("leaderboard")
        lb.add(("alice", 1500), ("bob", 2100))
        lb.range(-10, -1)  # top 10

        jobs = cache.queue("bg_jobs", max_size=1000)
        jobs.enqueue('{"type":"email"}')
        job = jobs.dequeue()  # oldest first

        undo = cache.stack("undo", max_size=50)
        undo.push('{"action":"delete"}')
        action = undo.pop()  # newest first

        # Durable PubSub (bounded, auto-expires after 2 weeks)
        ch = cache.pubsub("notifications", max_size=1000, max_age_seconds=1209600)
        ch.publish('{"type":"user.signup"}')
        msgs = ch.read("audit-service", count=50)
    """

    def __init__(self, client: LocalitasClient, name: str):
        self._client = client
        self._name = name
        self._base = f"/apps/cache/api/caches/{_esc(name)}"

    # ── KV ─────────────────────────────────────────────────────

    def get(self, key: str) -> Optional[str]:
        """Get a key's value. Returns None on miss."""
        try:
            result = self._client._do("GET", f"{self._base}/keys/{key}")
            return result.get("result", {}).get("value")
        except APIError as e:
            if e.status_code == 404:
                return None
            raise

    def set(self, key: str, value: str, ttl: int = 0) -> None:
        """Set a key with optional TTL in seconds. TTL 0 = no expiry."""
        self._client._do("PUT", f"{self._base}/keys/{key}", {"value": value, "ttl": ttl})

    def delete(self, key: str) -> None:
        """Delete a key."""
        self._client._do("DELETE", f"{self._base}/keys/{key}")

    def incr(self, key: str, delta: int = 1) -> int:
        """Atomically increment a key. Creates with delta if missing."""
        result = self._client._do("POST", f"{self._base}/incr/{key}", {"delta": delta})
        return result.get("result", {}).get("value", 0)

    def incr_with_ttl(self, key: str, delta: int = 1, ttl: int = 60) -> int:
        """Atomic increment + set TTL only on first call. For rate limiting."""
        result = self._client._do("POST", f"{self._base}/incrttl/{key}", {"delta": delta, "ttl": ttl})
        return result.get("result", {}).get("value", 0)

    def set_nx(self, key: str, value: str, ttl: int = 0) -> bool:
        """Set only if key doesn't exist. Returns True if set. For distributed locks."""
        result = self._client._do("POST", f"{self._base}/setnx/{key}", {"value": value, "ttl": ttl})
        return result.get("result", {}).get("acquired", False)

    def keys(self, pattern: str = "*") -> list[str]:
        """List keys matching glob pattern (* = any, ? = single char)."""
        result = self._client._do("GET", f"{self._base}/keys?pattern={_esc(pattern)}")
        return result.get("result", {}).get("keys", [])

    def flush(self) -> None:
        """Delete all keys, lists, sets, hashes in this cache."""
        self._client._do("POST", f"{self._base}/flush")

    def stats(self) -> dict:
        """Get cache stats: hits, misses, sets, deletes, evictions, key_count, hit_rate."""
        result = self._client._do("GET", f"{self._base}/stats")
        return result.get("result", {})

    # ── Data structure accessors ───────────────────────────────

    def list(self, name: str) -> "ListRef":
        """Return a ListRef for double-headed deque operations."""
        return ListRef(self, name)

    def set_store(self, name: str) -> "SetRef":
        """Return a SetRef for unique unordered set operations."""
        return SetRef(self, name)

    def hash(self, name: str) -> "HashRef":
        """Return a HashRef for field→value map operations."""
        return HashRef(self, name)

    def sorted_set(self, name: str) -> "SortedSetRef":
        """Return a SortedSetRef for score-ordered member operations."""
        return SortedSetRef(self, name)

    def queue(self, name: str, max_size: int = 0) -> "QueueRef":
        """Return a QueueRef for FIFO queue operations. max_size=0 for unbounded."""
        return QueueRef(self, name, max_size)

    def stack(self, name: str, max_size: int = 0) -> "StackRef":
        """Return a StackRef for LIFO stack operations. max_size=0 for unbounded."""
        return StackRef(self, name, max_size)

    def pubsub(self, channel: str, max_size: int = 0, max_age_seconds: int = 0) -> "PubSubRef":
        """Return a PubSubRef for durable pub/sub operations.

        Args:
            channel: Channel name.
            max_size: Bound by count (0 = unbounded).
            max_age_seconds: Auto-expire messages older than this (0 = unbounded).
        """
        return PubSubRef(self, channel, max_size, max_age_seconds)


class ListRef:
    """Double-headed deque. Push/pop from both ends.

    Example::

        recent = cache.list("recent_searches")
        recent.rpush("golang", "sqlite", "raft")  # append to tail
        recent.lpush("newest")                     # prepend to head
        recent.range(0, -1)   # ["newest", "golang", "sqlite", "raft"]
        recent.lpop()         # "newest" (from head)
        recent.rpop()         # "raft" (from tail)
    """

    def __init__(self, cache: CacheRef, name: str):
        self._cache = cache
        self._name = name
        self._base = f"{cache._base}/list/{_esc(name)}"

    def lpush(self, *values: str) -> int:
        """Prepend values to head. Returns new length."""
        r = self._cache._client._do("POST", f"{self._base}/lpush", {"values": list(values)})
        return r.get("result", {}).get("length", 0)

    def rpush(self, *values: str) -> int:
        """Append values to tail. Returns new length."""
        r = self._cache._client._do("POST", f"{self._base}/rpush", {"values": list(values)})
        return r.get("result", {}).get("length", 0)

    def lpop(self) -> Optional[str]:
        """Remove and return first element. None if empty."""
        try:
            r = self._cache._client._do("POST", f"{self._base}/lpop")
            return r.get("result", {}).get("value")
        except APIError as e:
            if e.status_code == 404: return None
            raise

    def rpop(self) -> Optional[str]:
        """Remove and return last element. None if empty."""
        try:
            r = self._cache._client._do("POST", f"{self._base}/rpop")
            return r.get("result", {}).get("value")
        except APIError as e:
            if e.status_code == 404: return None
            raise

    def range(self, start: int = 0, stop: int = -1) -> list[str]:
        """Return elements from start to stop (inclusive, 0-based, negative from end)."""
        r = self._cache._client._do("GET", f"{self._base}?start={start}&stop={stop}")
        return r.get("result", {}).get("values", [])

    def delete(self) -> None:
        """Delete the entire list."""
        self._cache._client._do("DELETE", self._base)


class SetRef:
    """Unique unordered set. Duplicates silently ignored.

    Example::

        tags = cache.set_store("article:123:tags")
        tags.add("go", "rust", "python", "go")  # 3 added (go deduped)
        tags.members()  # ["go", "python", "rust"] (sorted)
        tags.rem("rust")
    """

    def __init__(self, cache: CacheRef, name: str):
        self._cache = cache
        self._base = f"{cache._base}/set/{_esc(name)}"

    def add(self, *members: str) -> int:
        """Add members. Returns count of new members added."""
        r = self._cache._client._do("POST", f"{self._base}/add", {"members": list(members)})
        return r.get("result", {}).get("added", 0)

    def rem(self, *members: str) -> int:
        """Remove members. Returns count removed."""
        r = self._cache._client._do("POST", f"{self._base}/rem", {"members": list(members)})
        return r.get("result", {}).get("removed", 0)

    def members(self) -> list[str]:
        """Return all members, sorted."""
        r = self._cache._client._do("GET", self._base)
        return r.get("result", {}).get("members", [])

    def delete(self) -> None:
        """Delete the entire set."""
        self._cache._client._do("DELETE", self._base)


class HashRef:
    """Field→value map. Store structured data without JSON serialization.

    Example::

        user = cache.hash("user:123")
        user.set({"name": "Alice", "email": "alice@example.com", "role": "admin"})
        user.get("name")     # "Alice"
        user.get_all()       # {"email": "alice@...", "name": "Alice", "role": "admin"}
        user.to_json()       # '{"email":"alice@...","name":"Alice","role":"admin"}'
        user.from_json('{"city": "NYC"}')  # adds/updates fields from JSON
    """

    def __init__(self, cache: CacheRef, name: str):
        self._cache = cache
        self._base = f"{cache._base}/hash/{_esc(name)}"

    def set(self, fields: dict[str, str]) -> None:
        """Set one or more fields. Upserts existing."""
        self._cache._client._do("PUT", self._base, {"fields": fields})

    def get(self, field: str) -> Optional[str]:
        """Get a single field's value. None if missing."""
        try:
            r = self._cache._client._do("GET", f"{self._base}/field/{_esc(field)}")
            return r.get("result", {}).get("value")
        except APIError as e:
            if e.status_code == 404: return None
            raise

    def get_all(self) -> dict[str, str]:
        """Get all fields."""
        r = self._cache._client._do("GET", self._base)
        return r.get("result", {}).get("fields", {})

    def to_json(self) -> str:
        """Serialize hash to JSON string."""
        r = self._cache._client._do("GET", f"{self._base}/json")
        return r.get("result", {}).get("json", "{}")

    def from_json(self, json_str: str) -> None:
        """Populate hash from JSON object string."""
        self._cache._client._do("PUT", f"{self._base}/json", {"json": json_str})

    def delete(self) -> None:
        """Delete the entire hash."""
        self._cache._client._do("DELETE", self._base)


class SortedSetRef:
    """Members ordered by score. For leaderboards and priority queues.

    Example::

        lb = cache.sorted_set("leaderboard")
        lb.add(("alice", 1500), ("bob", 2100), ("charlie", 1800))
        lb.range(0, -1)           # all, lowest score first
        lb.range(-3, -1)          # top 3
        lb.rank("bob")            # 2 (0-based, lowest=0)
        lb.score("alice")         # 1500.0
        lb.incr_by("alice", 300)  # 1800.0
    """

    def __init__(self, cache: CacheRef, name: str):
        self._cache = cache
        self._base = f"{cache._base}/zset/{_esc(name)}"

    def add(self, *entries: tuple[str, float]) -> int:
        """Add (member, score) pairs. Returns count of new members."""
        r = self._cache._client._do("POST", f"{self._base}/add", {
            "entries": [{"member": m, "score": s} for m, s in entries],
        })
        return r.get("result", {}).get("added", 0)

    def score(self, member: str) -> Optional[float]:
        """Get score of a member. None if not found."""
        try:
            r = self._cache._client._do("GET", f"{self._base}/score/{_esc(member)}")
            return r.get("result", {}).get("score")
        except APIError:
            return None

    def rank(self, member: str) -> int:
        """Get 0-based rank (lowest score = 0). -1 if not found."""
        try:
            r = self._cache._client._do("GET", f"{self._base}/rank/{_esc(member)}")
            return r.get("result", {}).get("rank", -1)
        except APIError:
            return -1

    def range(self, start: int = 0, stop: int = -1) -> list[dict]:
        """Return entries by rank range. Each: {"member": str, "score": float}."""
        r = self._cache._client._do("GET", f"{self._base}?start={start}&stop={stop}")
        return r.get("result", {}).get("entries", [])

    def rem(self, *members: str) -> int:
        """Remove members. Returns count removed."""
        r = self._cache._client._do("POST", f"{self._base}/rem", {"members": list(members)})
        return r.get("result", {}).get("removed", 0)

    def incr_by(self, member: str, delta: float) -> float:
        """Increment member's score. Creates with delta if new."""
        r = self._cache._client._do("POST", f"{self._base}/incrby", {"member": member, "delta": delta})
        return r.get("result", {}).get("score", 0)

    def delete(self) -> None:
        """Delete the entire sorted set."""
        self._cache._client._do("DELETE", self._base)


class QueueRef:
    """FIFO queue. Bounded queues (max_size > 0) drop oldest on overflow.

    Example::

        jobs = cache.queue("email_jobs", max_size=1000)
        jobs.enqueue('{"to": "alice@example.com"}')
        jobs.enqueue('{"to": "bob@example.com"}')
        job = jobs.dequeue()  # oldest first: alice's email
        next_job = jobs.peek()  # bob's email (not removed)
    """

    def __init__(self, cache: CacheRef, name: str, max_size: int):
        self._cache = cache
        self._name = name
        self._max_size = max_size
        self._base = f"{cache._base}/queue/{_esc(name)}"

    def enqueue(self, value: str) -> int:
        """Add to back. Returns new length."""
        r = self._cache._client._do("POST", f"{self._base}/enqueue", {
            "value": value, "max_size": self._max_size,
        })
        return r.get("result", {}).get("length", 0)

    def dequeue(self) -> Optional[str]:
        """Remove and return front element (oldest). None if empty."""
        try:
            r = self._cache._client._do("POST", f"{self._base}/dequeue")
            return r.get("result", {}).get("value")
        except APIError as e:
            if e.status_code == 404: return None
            raise

    def peek(self) -> Optional[str]:
        """Return front element without removing. None if empty."""
        try:
            r = self._cache._client._do("GET", self._base)
            return r.get("result", {}).get("value")
        except APIError as e:
            if e.status_code == 404: return None
            raise


class StackRef:
    """LIFO stack. Bounded stacks (max_size > 0) drop bottom on overflow.

    Example::

        undo = cache.stack("undo_history", max_size=50)
        undo.push('{"action": "delete", "file": "doc.txt"}')
        undo.push('{"action": "rename", "from": "a.txt", "to": "b.txt"}')
        last = undo.pop()   # rename action (newest)
        top = undo.peek()   # delete action (not removed)
    """

    def __init__(self, cache: CacheRef, name: str, max_size: int):
        self._cache = cache
        self._max_size = max_size
        self._base = f"{cache._base}/stack/{_esc(name)}"

    def push(self, value: str) -> int:
        """Push to top. Returns new length."""
        r = self._cache._client._do("POST", f"{self._base}/push", {
            "value": value, "max_size": self._max_size,
        })
        return r.get("result", {}).get("length", 0)

    def pop(self) -> Optional[str]:
        """Pop top element (newest). None if empty."""
        try:
            r = self._cache._client._do("POST", f"{self._base}/pop")
            return r.get("result", {}).get("value")
        except APIError as e:
            if e.status_code == 404: return None
            raise

    def peek(self) -> Optional[str]:
        """Return top element without removing. None if empty."""
        try:
            r = self._cache._client._do("GET", self._base)
            return r.get("result", {}).get("value")
        except APIError as e:
            if e.status_code == 404: return None
            raise


class PubSubRef:
    """Durable pub/sub channel with broadcast and consumer group support.

    Broadcast: every consumer sees every message, cursor auto-advances.
    Consumer groups: round-robin with acknowledgment.

    Example::

        # Bounded channel (last 1000, expire after 2 weeks)
        ch = cache.pubsub("notifications", max_size=1000, max_age_seconds=1209600)

        # Publish
        ch.publish('{"type": "user.signup", "user": "alice"}')

        # Broadcast read (each consumer sees ALL messages)
        msgs = ch.read("audit-service", count=50)

        # Consumer group (round-robin with ack)
        ch.create_group("email_workers")
        msg = ch.claim("email_workers", "worker-1")
        if msg:
            process(msg)
            ch.ack("email_workers", msg["seq"])
    """

    def __init__(self, cache: CacheRef, channel: str, max_size: int, max_age_seconds: int):
        self._cache = cache
        self._channel = channel
        self._max_size = max_size
        self._max_age_seconds = max_age_seconds
        self._base = f"{cache._base}/pubsub/{_esc(channel)}"

    def publish(self, value: str) -> int:
        """Publish a message. Returns sequence number."""
        body: dict[str, Any] = {"value": value}
        if self._max_size > 0:
            body["max_size"] = self._max_size
        if self._max_age_seconds > 0:
            body["max_age_seconds"] = self._max_age_seconds
        r = self._cache._client._do("POST", f"{self._base}/publish", body)
        return r.get("result", {}).get("seq", 0)

    def read(self, consumer_id: str, count: int = 50) -> list[dict]:
        """Read new messages since consumer's last position."""
        r = self._cache._client._do("GET",
            f"{self._base}/read?consumer={_esc(consumer_id)}&count={count}")
        return r.get("result", {}).get("messages", [])

    def create_group(self, group_name: str) -> None:
        """Create a consumer group (starts from current position)."""
        self._cache._client._do("POST", f"{self._base}/group/{_esc(group_name)}")

    def claim(self, group_name: str, consumer_id: str) -> Optional[dict]:
        """Claim next unclaimed message in group. None if no messages."""
        r = self._cache._client._do("POST",
            f"{self._base}/group/{_esc(group_name)}/claim?consumer={_esc(consumer_id)}")
        msg = r.get("result", {}).get("message")
        return msg

    def ack(self, group_name: str, seq: int) -> None:
        """Acknowledge a claimed message."""
        self._cache._client._do("POST",
            f"{self._base}/group/{_esc(group_name)}/ack", {"seq": seq})

    def delete(self) -> None:
        """Delete channel and all messages."""
        self._cache._client._do("DELETE", self._base)


class PubSubWS:
    """WebSocket pub/sub client with automatic reconnection.

    Provides real-time message delivery with exponential backoff reconnect.
    Re-subscribes to all channels after reconnect. Cursor-based delivery
    ensures no missed messages.

    Example::

        def on_msg(msg):
            print(f"seq={msg['seq']} value={msg['value']}")

        ws = PubSubWS("ws://localhost:8080/apps/cache/ws/my-cache", token="lt_xxx")
        ws.subscribe("events", "consumer-1", on_msg)
        ws.publish("events", '{"type":"test"}')
        # ... later
        ws.close()
    """

    def __init__(self, url: str, token: str = "",
                 reconnect_interval: float = 2.0,
                 max_reconnect_interval: float = 30.0):
        import threading
        self._url = url
        self._token = token
        self._reconnect_interval = reconnect_interval
        self._max_reconnect_interval = max_reconnect_interval
        self._ws = None
        self._subscriptions: dict[str, dict] = {}
        self._listeners: dict[str, list] = {}
        self._reconnect_attempts = 0
        self._intentional_close = False
        self._lock = threading.Lock()
        self._thread: Optional[threading.Thread] = None
        self.connect()

    def connect(self):
        """Connect to the WebSocket server. Called automatically on construction."""
        import websocket
        import threading

        url = self._url
        if self._token:
            sep = "&" if "?" in url else "?"
            url += f"{sep}token={urllib.parse.quote(self._token)}"

        header = []
        if self._token:
            header.append(f"Authorization: Bearer {self._token}")

        self._ws = websocket.WebSocketApp(
            url,
            header=header,
            on_open=self._on_open,
            on_message=self._on_message,
            on_close=self._on_close,
            on_error=self._on_error,
        )
        self._thread = threading.Thread(target=self._ws.run_forever, daemon=True)
        self._thread.start()

    def close(self):
        """Disconnect. Does not auto-reconnect."""
        self._intentional_close = True
        if self._ws:
            self._ws.close()
            self._ws = None

    def subscribe(self, channel: str, consumer: str, callback):
        """Subscribe to a channel with a callback for each message.

        Args:
            channel: Channel name.
            consumer: Consumer ID for cursor tracking.
            callback: Called with dict containing seq, value, channel.
        """
        with self._lock:
            self._subscriptions[channel] = {"consumer": consumer, "callback": callback}
        self._send({"action": "subscribe", "channel": channel, "consumer": consumer})

    def unsubscribe(self, channel: str):
        """Unsubscribe from a channel."""
        with self._lock:
            self._subscriptions.pop(channel, None)
        self._send({"action": "unsubscribe", "channel": channel})

    def publish(self, channel: str, value: str,
                max_size: int = 0, max_age_seconds: int = 0):
        """Publish a message to a channel."""
        msg: dict[str, Any] = {"action": "publish", "channel": channel, "value": value}
        if max_size > 0:
            msg["max_size"] = max_size
        if max_age_seconds > 0:
            msg["max_age_seconds"] = max_age_seconds
        self._send(msg)

    def ack(self, channel: str, group: str, seq: int):
        """Acknowledge a consumer group message."""
        self._send({"action": "ack", "channel": channel, "group": group, "seq": seq})

    def on(self, event: str, callback):
        """Register an event listener.

        Events: connected, disconnected, error, reconnecting
        """
        with self._lock:
            self._listeners.setdefault(event, []).append(callback)

    def _send(self, data: dict):
        if self._ws and self._ws.sock and self._ws.sock.connected:
            self._ws.send(json.dumps(data))

    def _emit(self, event: str, data=None):
        with self._lock:
            cbs = list(self._listeners.get(event, []))
        for cb in cbs:
            cb(data)

    def _on_open(self, ws):
        self._reconnect_attempts = 0
        self._emit("connected")
        with self._lock:
            subs = dict(self._subscriptions)
        for channel, sub in subs.items():
            self._send({"action": "subscribe", "channel": channel,
                         "consumer": sub["consumer"]})

    def _on_message(self, ws, message: str):
        try:
            msg = json.loads(message)
        except json.JSONDecodeError:
            return
        self._emit(msg.get("type", ""), msg)
        if msg.get("type") == "message" and msg.get("channel"):
            with self._lock:
                sub = self._subscriptions.get(msg["channel"])
            if sub and sub.get("callback"):
                sub["callback"]({"seq": msg.get("seq"), "value": msg.get("value"),
                                  "channel": msg["channel"]})

    def _on_close(self, ws, close_status_code=None, close_msg=None):
        self._emit("disconnected")
        if not self._intentional_close:
            self._reconnect()

    def _on_error(self, ws, error):
        self._emit("error", error)

    def _reconnect(self):
        import threading
        self._reconnect_attempts += 1
        delay = min(
            self._reconnect_interval * (1.5 ** (self._reconnect_attempts - 1)),
            self._max_reconnect_interval,
        )
        if delay >= self._max_reconnect_interval:
            self._reconnect_attempts = 0
        self._emit("reconnecting", {"attempt": self._reconnect_attempts, "delay": delay})
        timer = threading.Timer(delay, self.connect)
        timer.daemon = True
        timer.start()
