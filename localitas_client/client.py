"""Localitas Python SDK — mirrors the Go client at /client/client.go."""

import json
import os
import re
import threading
import time
import urllib.parse
from collections.abc import Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

if TYPE_CHECKING:
    from .automation import CreateAutomationRequest
    from .dbapi import Connection

# Header the platform sets on automation-triggered HTTP calls; carries the run
# ID a handler publishes its async result back to (see AutomationClient.run_async).
AUTOMATION_RUN_ID_HEADER = "Localitas-Automation-Run-ID"

# How close to its expiry a client refreshes the identity token it acts with.
_REFRESH_BEFORE_SECONDS = 60

# The phases of a node's lifecycle wait_ready can wait for: every shard has a
# leader; core serves its own calls (users still get 503); every route serves.
PHASE_RAFT_READY = "raft_ready"
PHASE_SERVING = "serving"
PHASE_READY = "ready"

# How long core holds one readiness question before answering that the node
# is not there yet: under core's shortest HTTP write timeout (30 s), which
# would cut a longer one.
_READY_POLL_TIMEOUT = 25
# How long core holds one wait for a run before answering that it has not
# finished, under the same write timeout.
_RUN_WAIT_HOLD = 25
_REDIAL_FIRST = 0.1
_REDIAL_MAX = 5.0


def _now() -> float:
    return time.time()


# How a process learns where core is and which token it acts with: core
# gives an app it starts (a container, a FaaS function) CORE_URL and
# LOCALITAS_API_TOKEN, and writes the same variables, with its loopback URL,
# to core.env in its home for the other processes of its machine (an app
# launchd runs, a tool). Each is read from the environment first, from the
# home's file second; the Go and Node SDKs read them the same way.
CORE_URL_VAR = "CORE_URL"
TOKEN_VAR = "LOCALITAS_API_TOKEN"
HOME_VAR = "LOCALITAS_HOME"
CORE_ENV_FILE = "core.env"

_NO_CORE_URL = (
    "CORE_URL is not set: core sets it for every app it starts and writes it to core.env in its home "
    "($LOCALITAS_HOME, else ~/.localitas) for the other processes of its machine; outside core pass the URL "
    "to the constructor"
)


def core_home() -> str:
    """The home of the core this process belongs to: $LOCALITAS_HOME, else
    ~/.localitas."""
    return os.environ.get(HOME_VAR) or str(Path.home() / ".localitas")


def _core_settings() -> tuple[str, str, str, str]:
    """(url, token, where the URL came from, the home's core.env path): each
    variable from the environment when set, else from the home's core.env."""
    url = os.environ.get(CORE_URL_VAR, "").strip()
    token = os.environ.get(TOKEN_VAR, "").strip()
    path = os.path.join(core_home(), CORE_ENV_FILE)
    source = "environment"
    if not url or not token:
        try:
            lines = Path(path).read_text().splitlines()
        except OSError:
            lines = []
        for line in lines:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = (part.strip() for part in line.split("=", 1))
            if key == CORE_URL_VAR and not url:
                url, source = value, path
            elif key == TOKEN_VAR and not token:
                token = value
    return url, token, source, path


def core_url_from_env() -> str:
    """Core's base URL: CORE_URL, else the one in the home's core.env.
    Raises ConfigError, naming the variable and the file, when there is none
    or it is not an absolute http(s) URL."""
    url, _, source, path = _core_settings()
    if not url:
        raise ConfigError(f"{_NO_CORE_URL} ({path} has none)")
    parts = urllib.parse.urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.netloc:
        raise ConfigError(f"CORE_URL {url!r} (from the {source}) is not an absolute http(s) URL")
    return url.rstrip("/")


def websocket_url(base_url: str, path: str) -> str:
    """The WebSocket URL of ``path`` on core at ``base_url``: ws for an http
    URL, wss for an https one. Raises ConfigError for an empty or non-http(s)
    base URL."""
    if not base_url:
        raise ConfigError(_NO_CORE_URL)
    parts = urllib.parse.urlsplit(base_url)
    scheme = {"http": "ws", "https": "wss"}.get(parts.scheme)
    if not scheme or not parts.netloc:
        raise ConfigError(f"core URL {base_url!r} is not an absolute http(s) URL")
    return urllib.parse.urlunsplit((scheme, parts.netloc, parts.path.rstrip("/") + path, "", ""))


def default_token() -> str:
    """The API token LocalitasClient.default() uses: LOCALITAS_API_TOKEN,
    else the one in the home's core.env; empty string if neither has one."""
    return _core_settings()[1]


class ConfigError(Exception):
    """The SDK lacks a setting it needs to reach core, such as CORE_URL."""


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
        client = LocalitasClient.default()
        databases = client.list_databases()
    """

    def __init__(self, base_url: str, token: str = ""):
        self.base_url = base_url.rstrip("/")
        self.token = token

    @classmethod
    def default(cls) -> "LocalitasClient":
        """The client of an app: core's URL (CORE_URL) and the app's token
        (LOCALITAS_API_TOKEN), each from the environment core gives an app it
        starts, else from the core.env core writes in its home for the other
        processes of its machine. Raises ConfigError naming the variable and
        the file when one is missing or malformed."""
        url = core_url_from_env()
        _, token, _, path = _core_settings()
        if not token:
            raise ConfigError(
                f"LOCALITAS_API_TOKEN is not set: core sets it for every app it starts and writes it to {path} "
                "for the other processes of its machine; outside core pass the token to the constructor"
            )
        return cls(url, token)

    def with_token(self, token: str) -> "LocalitasClient":
        return LocalitasClient(self.base_url, token)

    def _url(self, path: str) -> str:
        if not self.base_url:
            raise ConfigError(_NO_CORE_URL)
        return self.base_url + path

    def _bearer(self) -> str:
        """The token to send. An identity token within a minute of its expiry
        is first refreshed with core (POST /api/auth/identity/refresh), up to
        the max claim core fixed when it minted it; if the refresh fails the
        current token is sent and core decides."""
        from .scope import unverified_claims

        claims = unverified_claims(self.token) if self.token else {}
        exp, limit = int(claims.get("exp") or 0), int(claims.get("max") or 0)
        if exp and limit > exp and exp - _now() < _REFRESH_BEFORE_SECONDS:
            req = Request(self._url("/api/auth/identity/refresh"), data=b"", method="POST")
            req.add_header("Authorization", f"Bearer {self.token}")
            try:
                with urlopen(req, timeout=10) as resp:
                    refreshed = json.loads(resp.read().decode()).get("token")
                if refreshed:
                    self.token = refreshed
            except (HTTPError, OSError, ValueError):
                pass
        return self.token

    def identity_for_user(self, user_id: str, permission: str = "read", ttl_seconds: int = 300) -> str:
        """Ask core for a short-lived identity token for an existing user, so
        the app's background work can act as that user. Only core's system
        token may ask; core grants at most write, never more than the user's
        own permission, caps the lifetime at 15 minutes and audit-logs it."""
        resp = self._do("POST", "/api/auth/identity/for-user", {"user_id": user_id, "permission": permission, "ttl_seconds": ttl_seconds})
        return resp.get("token", "")

    # ── Databases ──────────────────────────────────────────────

    def list_databases(self) -> list[dict]:
        return self._do("GET", "/apps/data/api/databases")

    def create_database(self, name: str, system: bool = False) -> dict:
        if system:
            return self._do("POST", "/apps/data/api/system-dbs", {"name": name})
        return self._do("POST", "/apps/data/api/databases", {"name": name})

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

    def sql_transaction(self, db_id: str, statements: list[dict], unless_exists: dict | None = None) -> dict:
        """Apply statements ({"sql", "args"}) as one atomic transaction.

        ``unless_exists`` ({"sql", "args"}) makes it conditional: core runs
        that read-only query inside the replicated transaction, on the state
        every node holds at its place in the log, and applies nothing when it
        returns a row. The result then has ``"skipped": True`` — a success,
        not an error — so of writers racing on one condition exactly one
        applies. Core refuses a query that is not one read-only SELECT, WITH
        or VALUES statement."""
        body = {"statements": statements}
        if unless_exists is not None:
            body["unless_exists"] = unless_exists
        return self._do("POST", f"/apps/data/api/databases/{_esc(db_id)}/exec", body)

    # ── Search ─────────────────────────────────────────────────

    def search_fts(self, query: str, limit: int = 100, database_id: str = "") -> dict:
        path = f"/apps/data/api/search?q={_esc(query)}&limit={limit}"
        if database_id:
            path += f"&database_id={_esc(database_id)}"
        return self._do("GET", path)

    def announce(self, port: int) -> "Announcement":
        """Register the app listening on port with core, so core proxies to it
        and runs its jobs. The app tells core only where it is (this host,
        port); core reads the rest from its health.json,
        cron.json and public-paths.json. Returns at once and, in a
        background thread, waits until core is ready (wait_ready) and
        registers the app once, since core reads the app while registering
        it: call it before serving. A registration core answers 503 (not
        ready: it restarted) waits again; one it answers 502 (the app does
        not answer yet) is asked again after a bounded backoff; any other
        failure is logged at ERROR and not tried again. Call withdraw() on
        shutdown.

        In a container it does nothing: core registers app containers itself
        when Docker reports them healthy, and only core knows the port it
        published."""
        return Announcement(self, port)

    def wait_ready(self, phase: str = PHASE_READY, stop: "Optional[threading.Event]" = None) -> bool:
        """Wait until the node this client talks to is at phase (PHASE_READY,
        PHASE_SERVING or PHASE_RAFT_READY) with every hook of it finished.
        Asks core's readiness endpoint, a long poll core answers at the
        transition itself, and asks again when core answers that the node is
        not there yet: the poll's timeout passed, or the node drains (its
        next boot follows). Returns True once ready, False once stop is set
        (a poll in flight ends first, within core's hold). Any other
        failure, a fault no waiting mends, is raised."""
        stop = stop or threading.Event()
        redial = _REDIAL_FIRST
        while not stop.is_set():
            try:
                ready, phase_now, retry_after = self._ask_ready(phase)
            except OSError as e:
                if not _core_down(e):
                    raise
                # Core's process is not running or not yet listening: the one
                # case with no event to wait on, so dial again after a
                # bounded backoff.
                if stop.wait(redial):
                    return False
                redial = min(2 * redial, _REDIAL_MAX)
                continue
            redial = _REDIAL_FIRST
            if ready:
                return True
            # A draining node answers at once until its HTTP stops; core says
            # when to ask again.
            if phase_now in ("draining", "stopped") and stop.wait(retry_after):
                return False
        return False

    def _ask_ready(self, phase: str) -> tuple[bool, str, float]:
        """Ask core's readiness endpoint once, held up to _READY_POLL_TIMEOUT:
        whether the node is ready, its phase now, and core's Retry-After."""
        query = urllib.parse.urlencode({"phase": phase, "timeout": f"{_READY_POLL_TIMEOUT}s"})
        path = f"/api/system/ready?{query}"
        try:
            with urlopen(Request(self._url(path)), timeout=_READY_POLL_TIMEOUT + 30) as resp:
                status, body, headers = resp.status, resp.read().decode(), resp.headers
        except HTTPError as e:
            status, body, headers = e.code, e.read().decode(), e.headers
            if status != 503:
                raise APIError("GET", path, status, body) from None
        try:
            answer = json.loads(body)
        except ValueError:
            answer = None
        if not isinstance(answer, dict) or answer.get("ready") is not (status == 200):
            raise APIError("GET", path, status, f"not core's readiness answer: {body}")
        try:
            retry_after = max(0.0, float(headers.get("Retry-After", "1")))
        except ValueError:
            retry_after = 1.0
        return answer["ready"], answer.get("phase", ""), retry_after

    # ── Permissions ────────────────────────────────────────────

    def claim_resource(self, app: str, resource_type: str, resource_id: str) -> None:
        """Make the calling user the owner of an unowned resource (or one it already owns)."""
        self._do("POST", "/api/permissions/set-owner", {
            "app": app, "resource_type": resource_type, "resource_id": resource_id,
        })

    def check_permission(self, app: str, resource_type: str, resource_id: str) -> str:
        """The calling user's permission on a resource ("" for none)."""
        result = self._do("POST", "/api/permissions/check", {
            "app": app, "resource_type": resource_type, "resource_id": resource_id,
        })
        return result.get("permission", "")

    # The *_as_user methods let an app backend, holding an admin-scoped system
    # token, act for the user making the request.

    def set_resource_owner_as_user(self, user_id: str, app: str, resource_type: str, resource_id: str) -> None:
        self._do("POST", f"/api/permissions/users/{_esc(user_id)}/set-owner", {
            "app": app, "resource_type": resource_type, "resource_id": resource_id,
        })

    def check_permission_as_user(self, user_id: str, app: str, resource_type: str, resource_id: str) -> str:
        result = self._do("POST", f"/api/permissions/users/{_esc(user_id)}/check", {
            "app": app, "resource_type": resource_type, "resource_id": resource_id,
        })
        return result.get("permission", "")

    def delete_resource_permissions_as_user(self, user_id: str, app: str, resource_type: str, resource_id: str) -> None:
        path = f"/api/permissions/users/{_esc(user_id)}/resources/{_esc(app)}/{_esc(resource_type)}/{_esc(resource_id)}"
        self._do("DELETE", path)

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

    def get_resource_owner(self, app: str, resource_type: str, resource_id: str) -> str:
        path = f"/api/permissions/{_esc(app)}/{_esc(resource_type)}/{_esc(resource_id)}/owner"
        result = self._do("GET", path)
        return result.get("owner_id", "")

    def delete_resource_permissions(self, app: str, resource_type: str, resource_id: str) -> None:
        path = f"/api/permissions/{_esc(app)}/{_esc(resource_type)}/{_esc(resource_id)}"
        self._do("DELETE", path)

    def list_accessible_resources(self, app: str, resource_type: str) -> list[dict]:
        path = f"/api/permissions/accessible?app={_esc(app)}&resource_type={_esc(resource_type)}"
        result = self._do("GET", path)
        return result.get("resources", [])

    def get_user_group_ids(self, user_id: str) -> list[str]:
        path = f"/api/users/{_esc(user_id)}/groups"
        result = self._do("GET", path)
        return result.get("group_ids", [])

    def list_users(self) -> list[dict]:
        result = self._do("GET", "/api/users")
        return result.get("users", [])

    def list_groups(self) -> list[dict]:
        result = self._do("GET", "/api/groups")
        return result.get("groups", [])

    # ── Vault ──────────────────────────────────────────────────

    def vault_list_credentials(self) -> list[dict]:
        result = self._do("GET", "/apps/vault/api/credentials")
        return result.get("credentials", [])

    def vault_get_secrets(self, public_id: str) -> dict[str, str]:
        return self._do("GET", f"/apps/vault/api/credentials/{_esc(public_id)}/secrets")

    def vault_create_credential(self, name: str, url: str = "", keychain_sync: bool = False, data: Optional[dict] = None) -> dict:
        return self._do("POST", "/apps/vault/api/credentials", {
            "name": name, "url": url, "keychain_sync": keychain_sync, "data": data or {},
        })

    def vault_update_credential(self, public_id: str, name: str, url: str = "", keychain_sync: bool = False, data: Optional[dict] = None) -> None:
        self._do("PUT", f"/apps/vault/api/credentials/{_esc(public_id)}", {
            "name": name, "url": url, "keychain_sync": keychain_sync, "data": data or {},
        })

    def vault_delete_credential(self, public_id: str) -> None:
        self._do("DELETE", f"/apps/vault/api/credentials/{_esc(public_id)}")

    # ── Metrics (TSDB) ─────────────────────────────────────────

    def ingest_metrics(self, metrics: list[dict]) -> int:
        """Ingest structured metric points. Each metric is a dict with keys
        name, value, and optionally type and tags. Returns the accepted count."""
        result = self._do("POST", "/apps/tsdb/api/ingest", {"metrics": metrics})
        return (result or {}).get("accepted", 0)

    def ingest_dogstatsd(self, lines: str) -> int:
        """Ingest DogStatsD-format metric lines (text/plain). Returns accepted count."""
        req = Request(self._url("/apps/tsdb/api/ingest"), data=lines.encode(), method="POST")
        bearer = self._bearer()
        if bearer:
            req.add_header("Authorization", f"Bearer {bearer}")
        req.add_header("Content-Type", "text/plain")
        try:
            with urlopen(req, timeout=30) as resp:
                resp_body = resp.read().decode()
                return (json.loads(resp_body).get("accepted", 0) if resp_body else 0)
        except HTTPError as e:
            raise APIError("POST", "/apps/tsdb/api/ingest", e.code, e.read().decode()) from None

    # ── Notifications ──────────────────────────────────────────

    def create_notification(self, title: str, message: str = "", app: str = "", level: str = "") -> None:
        self._do("POST", "/api/notifications", {
            "app": app, "level": level, "title": title, "message": message,
        })

    # ── Cache ──────────────────────────────────────────────────

    def create_cache(self, name: str) -> None:
        """Create a named in-memory cache."""
        self._do("POST", "/apps/cache/api/caches", {"name": name})

    def list_caches(self) -> list[dict]:
        """List all named caches."""
        return self._cache_do("GET", "/apps/cache/api/caches").get("caches", [])

    def delete_cache(self, name: str) -> None:
        """Delete a named cache. Cannot delete 'public_paths'."""
        self._do("DELETE", f"/apps/cache/api/caches/{_esc(name)}")

    def automation(self) -> "AutomationClient":
        """Return an AutomationClient for run management and async job signaling."""
        return AutomationClient(self)

    def cache(self, name: str) -> "CacheRef":
        """Return a CacheRef for key-value and data structure operations.

        Usage:
            sessions = client.cache("sessions")
            sessions.set("user:abc", '{"name":"Alice"}', ttl=1800)
            val = sessions.get("user:abc")
        """
        return CacheRef(self, name)

    def app(self, name: str) -> "AppRef":
        """Return an AppRef that calls the app ``name`` through core.

        Usage:
            forecast = client.app("weather").get("/api/forecast")
        """
        return AppRef(self, name)

    def db(self, name: str) -> "Connection":
        """A DB-API connection (``dbapi.Connection``) to the app's system
        database ``name``, through this client. Its ID is asked of core on
        first use, so making one never fails.

        Usage:
            cur = client.db("notes").cursor()
            cur.execute("SELECT id, title FROM notes")
        """
        from .dbapi import Connection

        if not name:
            raise ValueError("database name must not be empty")
        return Connection(self, name=name)

    # ── Transport ──────────────────────────────────────────────

    def _do(self, method: str, path: str, body: Any = None, timeout: float = 30) -> Any:
        url = self._url(path)
        data = None
        if body is not None:
            data = json.dumps(body).encode()

        req = Request(url, data=data, method=method)
        bearer = self._bearer()
        if bearer:
            req.add_header("Authorization", f"Bearer {bearer}")
        if data is not None:
            req.add_header("Content-Type", "application/json")

        try:
            with urlopen(req, timeout=timeout) as resp:
                resp_body = resp.read().decode()
                if resp_body:
                    return json.loads(resp_body)
                return None
        except HTTPError as e:
            raise APIError(method, path, e.code, e.read().decode()) from None

    def _cache_do(self, method: str, path: str, body: Any = None) -> Any:
        """Call a cache data endpoint and return the result of core's envelope."""
        resp = self._do(method, path, body)
        if not isinstance(resp, dict) or "result" not in resp:
            raise ValueError(f"{method} {path}: response has no result envelope: {resp!r}")
        return resp["result"]


def _core_down(e: BaseException) -> bool:
    """Whether e says core's process is not there: it refused the connection,
    or dropped it without answering."""
    if isinstance(e, URLError) and not isinstance(e, HTTPError):
        e = e.reason if isinstance(e.reason, BaseException) else e
    return isinstance(e, (ConnectionRefusedError, ConnectionResetError))


def _app_not_answering(e: BaseException) -> bool:
    """Whether core could not take the app because the app did not answer it
    yet (502): its port closed, or health.json not served yet."""
    return isinstance(e, APIError) and e.status_code == 502


def _not_ready(e: BaseException) -> bool:
    """Whether a call failed because core could not take it yet: it answered
    503 (not ready), or its process is not there."""
    if isinstance(e, APIError):
        return e.status_code == 503
    return _core_down(e)


def _in_container() -> bool:
    if Path("/.dockerenv").exists():
        return True
    try:
        cgroup = Path("/proc/1/cgroup").read_text()
    except OSError:
        return False
    return any(k in cgroup for k in ("docker", "containerd", "kubepods"))


class Announcement:
    """An app's registration with core, made by LocalitasClient.announce."""

    def __init__(self, client: LocalitasClient, port: int):
        self._client = client
        self._url = f"http://localhost:{port}"
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._registering = False
        self._name = ""
        self._thread = None
        if _in_container():
            return
        self._thread = threading.Thread(target=self._run, name="localitas-announce", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        import logging

        log = logging.getLogger("localitas")
        redial = _REDIAL_FIRST
        while True:
            try:
                if not self._client.wait_ready(PHASE_READY, self._stop):
                    return
            except (APIError, OSError, ValueError) as e:
                log.error("waiting for core to be ready failed; the app at %s is not announced: %s", self._url, e)
                return
            with self._lock:
                if self._stop.is_set():
                    return
                self._registering = True
            name, err = "", None
            try:
                name = (self._client._do("POST", "/apps/ext", {"url": self._url}) or {}).get("name", "")
            except (APIError, OSError, ValueError) as e:
                err = e
            with self._lock:
                self._registering = False
                if err is None:
                    self._name = name
            if err is None:
                log.info("announced %s at %s", name, self._url)
                return
            if self._stop.is_set():
                return
            if _app_not_answering(err):
                # The app announces before it serves: nothing tells this
                # thread when it starts answering, so core is asked again
                # after a bounded backoff.
                log.info("core cannot reach the app at %s yet; asking again in %.1fs: %s", self._url, redial, err)
                if self._stop.wait(redial):
                    return
                redial = min(2 * redial, _REDIAL_MAX)
                continue
            if not _not_ready(err):
                log.error("core refused the app at %s; it is not announced: %s", self._url, err)
                return
            log.info("core is not ready to take the app at %s; waiting for it: %s", self._url, err)

    @property
    def name(self) -> str:
        """The app core registered, or "" while core has not taken it."""
        if self._thread is not None and self._thread.is_alive():
            return ""
        return self._name

    def withdraw(self) -> None:
        """Stop announcing and take the app out of core, if core had taken it.
        A registration in flight is waited for, since core answers after
        taking the app and only its answer names it; a wait for core's
        readiness is left to end on its own, as it registers nothing."""
        with self._lock:
            self._stop.set()
            registering = self._registering
        if self._thread is not None and registering:
            self._thread.join()
        if self._name:
            self._client._do("DELETE", f"/apps/ext/{_esc(self._name)}")
            self._name = ""


_APP_NAME = re.compile(r"^[a-z0-9][a-z0-9_-]*$")


def _check_app_name(name: str) -> None:
    if not isinstance(name, str) or not _APP_NAME.match(name):
        raise ValueError(f"app name {name!r} must match {_APP_NAME.pattern}")


def _check_app_path(path: str) -> None:
    if not isinstance(path, str) or not path.startswith("/") or path.startswith("//"):
        raise ValueError(f"app path {path!r} must start with a single /: a path, never an address")
    if "\\" in path:
        raise ValueError(f"app path {path!r} must not contain a backslash")
    parts = urllib.parse.urlsplit(path)
    if parts.scheme or parts.netloc:
        raise ValueError(f"app path {path!r} must not name a scheme or host")


class AppRef:
    """Another app, called by name through core at /apps/ext/<name><path>
    with the client's token. Each call returns the decoded JSON answer (None
    for an empty one) and raises APIError for a 4xx or 5xx."""

    def __init__(self, client: LocalitasClient, name: str):
        _check_app_name(name)
        self._client = client
        self.name = name

    def _call(self, method: str, path: str, body: Any = None) -> Any:
        _check_app_path(path)
        return self._client._do(method, f"/apps/ext/{self.name}{path}", body)

    def get(self, path: str) -> Any:
        return self._call("GET", path)

    def post(self, path: str, body: Any = None) -> Any:
        return self._call("POST", path, body)

    def put(self, path: str, body: Any = None) -> Any:
        return self._call("PUT", path, body)

    def delete(self, path: str) -> Any:
        return self._call("DELETE", path)


def _esc(s: str) -> str:
    return urllib.parse.quote(str(s), safe="")


def _is_miss(e: APIError) -> bool:
    """A 404 the cache answered for an absent key, member or element.
    An unknown cache is a plain-text 404 and is not a miss."""
    if e.status_code != 404:
        return False
    try:
        body = json.loads(e.body)
    except ValueError:
        return False
    return isinstance(body, dict) and bool(body.get("error"))


class CacheRef:
    """Reference to a named cache. Provides Redis-like key-value operations
    and typed data structure accessors.

    Example::

        client = LocalitasClient.default()
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

        # Live delivery over the cache's WebSocket
        ws = cache.subscribe("notifications", "audit-service", print)
        ws.close()
    """

    def __init__(self, client: LocalitasClient, name: str):
        import threading

        self._client = client
        self._name = name
        self._base = f"/apps/cache/api/caches/{_esc(name)}"
        self._socket: Optional["PubSubWS"] = None
        self._socket_lock = threading.Lock()

    # ── Live pub/sub ───────────────────────────────────────────

    def _pubsub_ws(self) -> "PubSubWS":
        with self._socket_lock:
            if self._socket is None:
                url = websocket_url(self._client.base_url, f"/apps/cache/ws/{_esc(self._name)}")
                self._socket = PubSubWS(url, self._client._bearer())
            return self._socket

    def subscribe(self, channel: str, consumer: str, callback) -> "PubSubWS":
        """``PubSubWS.subscribe`` on this cache's WebSocket, opened from the
        client's URL and token on first use and shared by later calls on
        this ref. Returns the socket, to ``close()`` it."""
        ws = self._pubsub_ws()
        ws.subscribe(channel, consumer, callback)
        return ws

    def watch(self, channel: str, after_seq: int, callback) -> "PubSubWS":
        """``PubSubWS.watch`` on this cache's shared WebSocket; returns it."""
        ws = self._pubsub_ws()
        ws.watch(channel, after_seq, callback)
        return ws

    def watch_latest(self, channel: str, callback) -> "PubSubWS":
        """``PubSubWS.watch_latest`` on this cache's shared WebSocket; returns it."""
        ws = self._pubsub_ws()
        ws.watch_latest(channel, callback)
        return ws

    # ── KV ─────────────────────────────────────────────────────

    def get(self, key: str) -> Optional[str]:
        """Get a key's value. Returns None on miss."""
        return self._get(f"{self._base}/keys/{_esc(key)}")

    def get_consistent(self, key: str) -> Optional[str]:
        """``get`` that sees every write committed before it, through whichever
        node it entered: a plain read answers from that node's own copy, which
        may not yet hold a write made through another node a moment ago. Use
        it where a stale value is wrong, such as a count of failed logons."""
        return self._get(f"{self._base}/keys/{_esc(key)}?consistent=true")

    def _get(self, path: str) -> Optional[str]:
        try:
            return self._client._cache_do("GET", path).get("value")
        except APIError as e:
            if _is_miss(e):
                return None
            raise

    def set(self, key: str, value: str, ttl: int = 0) -> None:
        """Set a key with optional TTL in seconds. TTL 0 = no expiry."""
        self._client._cache_do("PUT", f"{self._base}/keys/{_esc(key)}", {"value": value, "ttl": ttl})

    def ttl(self, key: str) -> int:
        """Remaining time-to-live in seconds: -1 if the key has no expiry, -2
        if it doesn't exist or has expired. An unknown cache raises."""
        try:
            result = self._client._cache_do("GET", f"{self._base}/keys/{_esc(key)}")
            return result.get("ttl_remaining", 0)
        except APIError as e:
            if _is_miss(e):
                return -2
            raise

    def expire(self, key: str, ttl: int) -> bool:
        """Set or update the TTL (seconds) of an existing key. Returns False
        if the key doesn't exist; a non-positive ttl raises (400)."""
        try:
            self._client._cache_do("POST", f"{self._base}/keys/expire/{_esc(key)}", {"ttl": ttl})
            return True
        except APIError as e:
            if _is_miss(e):
                return False
            raise

    def get_set(self, key: str, value: str, ttl: int = 0) -> Optional[str]:
        """Atomically set a key and return its old value, or None if it had none."""
        result = self._client._cache_do("POST", f"{self._base}/keys/getset/{_esc(key)}", {"value": value, "ttl": ttl})
        return result.get("old_value") if result.get("had_old") else None

    def delete(self, key: str) -> None:
        """Delete a key."""
        self._client._cache_do("DELETE", f"{self._base}/keys/{_esc(key)}")

    def incr(self, key: str, delta: int = 1) -> int:
        """Atomically increment a key. Creates with delta if missing."""
        result = self._client._cache_do("POST", f"{self._base}/keys/incr/{_esc(key)}", {"delta": delta})
        return result.get("value", 0)

    def incr_with_ttl(self, key: str, delta: int = 1, ttl: int = 60) -> int:
        """Atomic increment + set TTL only on first call. For rate limiting."""
        result = self._client._cache_do("POST", f"{self._base}/keys/incrttl/{_esc(key)}", {"delta": delta, "ttl": ttl})
        return result.get("value", 0)

    def set_nx(self, key: str, value: str, ttl: int = 0) -> bool:
        """Set only if key doesn't exist. Returns True if set. For distributed locks."""
        result = self._client._cache_do("POST", f"{self._base}/keys/setnx/{_esc(key)}", {"value": value, "ttl": ttl})
        return result.get("acquired", False)

    def keys(self, pattern: str = "*") -> list[str]:
        """List keys matching glob pattern (* = any, ? = single char)."""
        result = self._client._cache_do("GET", f"{self._base}/keys?pattern={_esc(pattern)}")
        return result.get("keys", [])

    def flush(self) -> None:
        """Delete all keys, lists, sets, hashes in this cache."""
        self._client._cache_do("POST", f"{self._base}/flush")

    def stats(self) -> dict:
        """Get cache stats: hits, misses, sets, deletes, evictions, key_count, hit_rate."""
        return self._client._cache_do("GET", f"{self._base}/stats")

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
        self._base = f"{cache._base}/lists/{_esc(name)}"

    def lpush(self, *values: str) -> int:
        """Prepend values to head. Returns new length."""
        r = self._cache._client._cache_do("POST", f"{self._base}/lpush", {"values": list(values)})
        return r.get("length", 0)

    def rpush(self, *values: str) -> int:
        """Append values to tail. Returns new length."""
        r = self._cache._client._cache_do("POST", f"{self._base}/rpush", {"values": list(values)})
        return r.get("length", 0)

    def lpop(self) -> Optional[str]:
        """Remove and return first element. None if empty."""
        try:
            r = self._cache._client._cache_do("POST", f"{self._base}/lpop")
            return r.get("value")
        except APIError as e:
            if _is_miss(e): return None
            raise

    def rpop(self) -> Optional[str]:
        """Remove and return last element. None if empty."""
        try:
            r = self._cache._client._cache_do("POST", f"{self._base}/rpop")
            return r.get("value")
        except APIError as e:
            if _is_miss(e): return None
            raise

    def range(self, start: int = 0, stop: int = -1) -> list[str]:
        """Return elements from start to stop (inclusive, 0-based, negative from end)."""
        r = self._cache._client._cache_do("GET", f"{self._base}?start={start}&stop={stop}")
        return r.get("values", [])

    def len(self) -> int:
        """Return the length of the list."""
        r = self._cache._client._cache_do("GET", f"{self._base}?start=0&stop=-1")
        return r.get("length", 0)

    def delete(self) -> None:
        """Delete the entire list."""
        self._cache._client._cache_do("DELETE", self._base)


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
        self._base = f"{cache._base}/sets/{_esc(name)}"

    def add(self, *members: str) -> int:
        """Add members. Returns count of new members added."""
        r = self._cache._client._cache_do("POST", f"{self._base}/add", {"members": list(members)})
        return r.get("added", 0)

    def rem(self, *members: str) -> int:
        """Remove members. Returns count removed."""
        r = self._cache._client._cache_do("POST", f"{self._base}/rem", {"members": list(members)})
        return r.get("removed", 0)

    def members(self) -> list[str]:
        """Return all members, sorted."""
        r = self._cache._client._cache_do("GET", self._base)
        return r.get("members", [])

    def delete(self) -> None:
        """Delete the entire set."""
        self._cache._client._cache_do("DELETE", self._base)


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
        self._base = f"{cache._base}/hashes/{_esc(name)}"

    def set(self, fields: dict[str, str]) -> None:
        """Set one or more fields. Upserts existing."""
        self._cache._client._cache_do("PUT", self._base, {"fields": fields})

    def get(self, field: str) -> Optional[str]:
        """Get a single field's value. None if missing."""
        try:
            r = self._cache._client._cache_do("GET", f"{self._base}/fields/{_esc(field)}")
            return r.get("value")
        except APIError as e:
            if _is_miss(e): return None
            raise

    def get_all(self) -> dict[str, str]:
        """Get all fields."""
        r = self._cache._client._cache_do("GET", self._base)
        return r.get("fields", {})

    def to_json(self) -> str:
        """Serialize hash to JSON string."""
        r = self._cache._client._cache_do("GET", f"{self._base}/json")
        return r.get("json", "{}")

    def from_json(self, json_str: str) -> None:
        """Populate hash from JSON object string."""
        self._cache._client._cache_do("PUT", f"{self._base}/json", {"json": json_str})

    def delete_fields(self, *fields: str) -> int:
        """Remove fields from the hash. Returns the number removed."""
        removed = 0
        for field in fields:
            r = self._cache._client._cache_do("DELETE", f"{self._base}/fields/{_esc(field)}")
            removed += r.get("removed", 0)
        return removed

    def delete(self) -> None:
        """Delete the entire hash."""
        self._cache._client._cache_do("DELETE", self._base)


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
        self._base = f"{cache._base}/zsets/{_esc(name)}"

    def add(self, *entries: tuple[str, float]) -> int:
        """Add (member, score) pairs. Returns count of new members."""
        r = self._cache._client._cache_do("POST", f"{self._base}/add", {
            "entries": [{"member": m, "score": s} for m, s in entries],
        })
        return r.get("added", 0)

    def score(self, member: str) -> Optional[float]:
        """Get score of a member. None if not found."""
        try:
            r = self._cache._client._cache_do("GET", f"{self._base}/score/{_esc(member)}")
            return r.get("score")
        except APIError as e:
            if _is_miss(e):
                return None
            raise

    def rank(self, member: str) -> int:
        """Get 0-based rank (lowest score = 0). -1 if not found."""
        try:
            r = self._cache._client._cache_do("GET", f"{self._base}/rank/{_esc(member)}")
            return r.get("rank", -1)
        except APIError as e:
            if _is_miss(e):
                return -1
            raise

    def range(self, start: int = 0, stop: int = -1) -> list[dict]:
        """Return entries by rank range. Each: {"member": str, "score": float}."""
        r = self._cache._client._cache_do("GET", f"{self._base}?start={start}&stop={stop}")
        return r.get("entries", [])

    def range_by_score(self, min_score: float, max_score: float) -> list[dict]:
        """Return entries with scores between min_score and max_score (inclusive)."""
        query = urllib.parse.urlencode({"min": min_score, "max": max_score})
        r = self._cache._client._cache_do("GET", f"{self._base}/byscore?{query}")
        return r.get("entries", [])

    def rem(self, *members: str) -> int:
        """Remove members. Returns count removed."""
        r = self._cache._client._cache_do("POST", f"{self._base}/rem", {"members": list(members)})
        return r.get("removed", 0)

    def incr_by(self, member: str, delta: float) -> float:
        """Increment member's score. Creates with delta if new."""
        r = self._cache._client._cache_do("POST", f"{self._base}/incrby", {"member": member, "delta": delta})
        return r.get("score", 0)

    def delete(self) -> None:
        """Delete the entire sorted set."""
        self._cache._client._cache_do("DELETE", self._base)


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
        self._base = f"{cache._base}/queues/{_esc(name)}"

    def enqueue(self, value: str) -> int:
        """Add to back. Returns new length."""
        r = self._cache._client._cache_do("POST", f"{self._base}/enqueue", {
            "value": value, "max_size": self._max_size,
        })
        return r.get("length", 0)

    def dequeue(self) -> Optional[str]:
        """Remove and return front element (oldest). None if empty."""
        try:
            r = self._cache._client._cache_do("POST", f"{self._base}/dequeue")
            return r.get("value")
        except APIError as e:
            if _is_miss(e): return None
            raise

    def peek(self) -> Optional[str]:
        """Return front element without removing. None if empty."""
        try:
            r = self._cache._client._cache_do("GET", self._base)
            return r.get("value")
        except APIError as e:
            if _is_miss(e): return None
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
        self._base = f"{cache._base}/stacks/{_esc(name)}"

    def push(self, value: str) -> int:
        """Push to top. Returns new length."""
        r = self._cache._client._cache_do("POST", f"{self._base}/push", {
            "value": value, "max_size": self._max_size,
        })
        return r.get("length", 0)

    def pop(self) -> Optional[str]:
        """Pop top element (newest). None if empty."""
        try:
            r = self._cache._client._cache_do("POST", f"{self._base}/pop")
            return r.get("value")
        except APIError as e:
            if _is_miss(e): return None
            raise

    def peek(self) -> Optional[str]:
        """Return top element without removing. None if empty."""
        try:
            r = self._cache._client._cache_do("GET", self._base)
            return r.get("value")
        except APIError as e:
            if _is_miss(e): return None
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
        r = self._cache._client._cache_do("POST", f"{self._base}/publish", body)
        return r.get("seq", 0)

    def read(self, consumer_id: str, count: int = 50) -> list[dict]:
        """Read new messages since consumer's last position."""
        r = self._cache._client._cache_do("GET",
            f"{self._base}/read?consumer={_esc(consumer_id)}&count={count}")
        return r.get("messages", [])

    def create_group(self, group_name: str) -> None:
        """Create a consumer group (starts from current position)."""
        self._cache._client._cache_do("POST", f"{self._base}/group/{_esc(group_name)}")

    def claim(self, group_name: str, consumer_id: str) -> Optional[dict]:
        """Claim next unclaimed message in group. None if no messages."""
        r = self._cache._client._cache_do("POST",
            f"{self._base}/group/{_esc(group_name)}/claim?consumer={_esc(consumer_id)}")
        msg = r.get("message")
        return msg

    def ack(self, group_name: str, seq: int) -> None:
        """Acknowledge a claimed message."""
        self._cache._client._cache_do("POST",
            f"{self._base}/group/{_esc(group_name)}/ack", {"seq": seq})

    def reclaim(self, group_name: str, consumer_id: str, timeout: int = 30) -> list[dict]:
        """Re-assign to consumer_id the group's messages pending longer than
        timeout seconds without an ack, and return them."""
        query = urllib.parse.urlencode({"consumer": consumer_id, "timeout": timeout})
        r = self._cache._client._cache_do("POST",
            f"{self._base}/group/{_esc(group_name)}/reclaim?{query}")
        return r.get("messages", [])

    def trim(self, max_age_seconds: int) -> int:
        """Remove messages older than max_age_seconds. Returns the number
        removed; a non-positive age raises (400)."""
        r = self._cache._client._cache_do("POST", f"{self._base}/trim", {"max_age": max_age_seconds})
        return r.get("removed", 0)

    def delete(self) -> None:
        """Delete channel and all messages."""
        self._cache._client._cache_do("DELETE", self._base)

    def info(self) -> dict:
        """Describe the channel on the node asked: name, message_count,
        last_message_at and last_seq (all zero for an empty channel)."""
        return self._cache._client._cache_do("GET", self._base)

    def last_seq(self) -> int:
        """The newest message's sequence number on the node asked. Note it
        before starting something whose outcome is published here, then
        ``PubSubWS.watch`` after it, and the outcome cannot be missed."""
        return self.info().get("last_seq", 0)


IDEMPOTENCY_KEY_HEADER = "Idempotency-Key"


def _header(headers, name: str) -> str:
    value = headers.get(name)
    if value is None and isinstance(headers, Mapping):
        wanted = name.lower()
        value = next((v for k, v in headers.items() if k.lower() == wanted), None)
    return value or ""


def _run_and_key(source) -> tuple[str, str]:
    """The run ID and Idempotency-Key of a run ID string, a request, or its
    headers."""
    if isinstance(source, str) or not source:
        return source or "", ""
    try:
        headers = source.headers
    except AttributeError:
        headers = source
    return _header(headers, AUTOMATION_RUN_ID_HEADER), _header(headers, IDEMPOTENCY_KEY_HEADER)


class AutomationClient:
    """Client for automation run management and async job signaling."""

    def __init__(self, client: "LocalitasClient"):
        self._client = client

    def create(self, request: "CreateAutomationRequest") -> dict:
        """Create an automation from a typed request (see
        ``localitas_client.automation``) and return the one core made."""
        return self._client._do("POST", "/apps/automation/api/automations", request.to_dict())

    def trigger(self, automation_id: str) -> dict:
        """Trigger an automation and return the run."""
        return self._client._do("POST", f"/apps/automation/api/automations/{automation_id}/trigger")

    def get_run(self, run_id: str) -> dict:
        """Get a run by ID."""
        return self._client._do("GET", f"/apps/automation/api/runs/{run_id}")

    def publish_result(self, run_id: str, status: str = "completed", result: Any = None, error: str = "",
                       idempotency_key: str = ""):
        """Signal async job completion. Called from inside the background job.
        ``result`` is any JSON value (dict, list, str, number). ``idempotency_key``
        (the call's Idempotency-Key header) restricts the completion to the try
        that call was."""
        payload = {"status": status, "result": {} if result is None else result, "error": error}
        if idempotency_key:
            payload["idempotency_key"] = idempotency_key
        return self._client._do("POST", f"/apps/automation/api/runs/{_esc(run_id)}/complete", payload)

    def run_async(self, run_id_or_request, work) -> bool:
        """Run ``work`` in a background thread and publish its result back to the
        automation run when done. ``work`` is a zero-arg callable returning any
        JSON value (dict, list, str, number) or None; core times the run itself.
        Returns True if the call belongs to a run (so the handler should respond
        202 immediately), False otherwise — mirroring the Go RunAsync
        fire-and-202 pattern.

        Pass the incoming request (anything with ``.headers``) or its headers
        (any mapping; names match case-insensitively): the run comes from
        ``AUTOMATION_RUN_ID_HEADER`` and the Idempotency-Key is echoed in the
        completion. A run ID string also works, without the key.
        """
        run_id, key = _run_and_key(run_id_or_request)
        if not run_id:
            return False

        import threading

        def _run():
            status, error, result = "completed", "", None
            try:
                result = work()
            except Exception as exc:  # noqa: BLE001 - report failure to the run
                status, error = "failed", str(exc)
            self.publish_result(run_id, status, result, error, idempotency_key=key)

        threading.Thread(target=_run, daemon=True).start()
        return True

    def wait_for_run(self, run_id: str, timeout: float = 3600) -> dict:
        """Return the run once it has finished, without polling it.

        Asks core's wait for the run, a long poll any node answers from the
        committed run: at once when the run has finished, else as soon as a
        commit ends it, or after core's hold that it has not, when it asks
        again. It depends on nothing being published: a node that records
        the run's end and dies before telling anyone still leaves the end
        committed. A core that cannot answer yet (503, or its process not
        there) is waited for (wait_ready) and asked again. A run that does
        not exist, or is not the caller's, raises core's APIError; the wait
        raises TimeoutError after timeout seconds.
        """
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(f"Timeout waiting for run {run_id}")
            hold = min(_RUN_WAIT_HOLD, remaining)
            query = urllib.parse.urlencode({"timeout": f"{hold:.3f}s"})
            try:
                answer = self._client._do("GET", f"/apps/automation/api/runs/{_esc(run_id)}/wait?{query}",
                                          timeout=hold + 30)
            except (APIError, OSError) as e:
                if not _not_ready(e):
                    raise
                stop = threading.Event()
                timer = threading.Timer(remaining, stop.set)
                timer.start()
                try:
                    if not self._client.wait_ready(stop=stop):
                        raise TimeoutError(f"Timeout waiting for run {run_id}") from None
                finally:
                    timer.cancel()
                continue
            if answer.get("finished"):
                return answer["run"]


class PubSubWS:
    """WebSocket pub/sub client with automatic reconnection.

    Provides real-time message delivery with exponential backoff reconnect.
    Re-subscribes to all channels after reconnect. Cursor-based delivery
    ensures no missed messages.

    Apps get one from ``CacheRef.subscribe``/``watch``/``watch_latest``,
    which build its URL from the client's.

    Example::

        def on_msg(msg):
            print(f"seq={msg['seq']} value={msg['value']}")

        ws = LocalitasClient.default().cache("my-cache").subscribe("events", "consumer-1", on_msg)
        ws.publish("events", '{"type":"test"}')
        # ... later
        ws.close()
    """

    def __init__(self, url: str, token: str = "",
                 reconnect_interval: float = 2.0,
                 max_reconnect_interval: float = 30.0,
                 batch_size: int = 50):
        import threading
        self._url = url
        self._token = token
        self._reconnect_interval = reconnect_interval
        self._max_reconnect_interval = max_reconnect_interval
        self._batch_size = batch_size
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
        self._start(channel, {"consumer": consumer, "callback": callback})

    def watch(self, channel: str, after_seq: int, callback):
        """Call ``callback`` with each message published to ``channel`` after
        ``after_seq``, as the server's node applies it. A watch keeps no cursor
        and asks the cluster for nothing. To miss nothing a later action
        publishes, note ``PubSubRef.last_seq()`` before the action and watch
        after it. A reconnect resumes after the last message delivered."""
        self._start(channel, {"callback": callback, "watch": True,
                              "after": after_seq, "from_latest": False})

    def watch_latest(self, channel: str, callback):
        """``watch`` from the channel's newest message when the watch starts."""
        self._start(channel, {"callback": callback, "watch": True,
                              "after": 0, "from_latest": True})

    def _start(self, channel: str, sub: dict):
        with self._lock:
            self._subscriptions[channel] = sub
        self._send(self._request(channel, sub))

    def _request(self, channel: str, sub: dict) -> dict:
        count = self._batch_size
        if not sub.get("watch"):
            return {"action": "subscribe", "channel": channel, "consumer": sub["consumer"], "count": count}
        if sub["from_latest"]:
            return {"action": "watch", "channel": channel, "from_latest": True, "count": count}
        return {"action": "watch", "channel": channel, "seq": sub["after"], "count": count}

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
            self._send(self._request(channel, sub))

    def _on_message(self, ws, message: str):
        try:
            msg = json.loads(message)
        except json.JSONDecodeError:
            return
        # The server's greeting is the socket opening, already emitted as
        # "connected": a second one would run connect handlers twice.
        if msg.get("type") != "connected":
            self._emit(msg.get("type", ""), msg)
        if msg.get("type") == "subscribed" and msg.get("channel"):
            with self._lock:
                sub = self._subscriptions.get(msg["channel"])
                if sub and sub.get("watch") and sub["from_latest"]:
                    sub["after"], sub["from_latest"] = msg.get("seq", 0), False
        if msg.get("type") == "message" and msg.get("channel"):
            with self._lock:
                sub = self._subscriptions.get(msg["channel"])
                if sub and sub.get("watch"):
                    # A resumed watch may resend what came before the drop.
                    if msg.get("seq", 0) <= sub["after"]:
                        return
                    sub["after"] = msg.get("seq", 0)
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
