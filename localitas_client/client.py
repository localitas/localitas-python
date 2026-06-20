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
