"""Embedded-style SQL migration runner — mirrors the Go client's migrator.go.

Loads ``.sql`` files from a directory, tracks applied versions in a
``schema_migrations`` table, and applies pending migrations idempotently.
Run this at app startup and from your ``POST /migrations/run`` endpoint.

Filenames must match the platform timestamp format:
``YYYYMMDD-HHMMSS-MMM-description.sql`` (or legacy ``NNN_description.sql``).
"""

import os
import re
import time

_TIMESTAMP_RE = re.compile(r"^(\d{8})-(\d{6})-(\d{3})-(.+)\.sql$")
_LEGACY_RE = re.compile(r"^(\d+)_(.+)\.sql$")


class Migrator:
    """Applies SQL migrations from ``migrations_dir`` against a database.

    ``exec_sql(sql, *args)`` and ``query_sql(sql, *args)`` are callables — pass
    ``client.sql_exec``/``client.sql_query`` bound to a database id, e.g.::

        m = Migrator(
            "migrations",
            lambda sql, *a: client.sql_exec(db_id, sql, *a),
            lambda sql, *a: client.sql_query(db_id, sql, *a),
        )
        m.run()
    """

    def __init__(self, migrations_dir, exec_sql, query_sql):
        self._dir = migrations_dir
        self._exec = exec_sql
        self._query = query_sql
        self._migrations = _load_migrations(migrations_dir)

    def run(self):
        """Apply all pending migrations. Idempotent — already-applied
        migrations are skipped and 'already exists' errors are tolerated."""
        self._exec(
            "CREATE TABLE IF NOT EXISTS schema_migrations ("
            "version TEXT PRIMARY KEY, name TEXT NOT NULL, applied_at INTEGER NOT NULL)"
        )

        result = self._query("SELECT version FROM schema_migrations")
        applied = {row[0] for row in (result or {}).get("rows", [])}

        for version, name, sql in self._migrations:
            if version in applied:
                continue
            for stmt in _split_sql(sql):
                try:
                    self._exec(stmt)
                except Exception as exc:  # noqa: BLE001
                    if not _is_idempotent_error(exc):
                        raise
            self._exec(
                "INSERT INTO schema_migrations (version, name, applied_at) VALUES (?, ?, ?)",
                version, name, int(time.time()),
            )


def _load_migrations(migrations_dir):
    migrations = []
    for fname in sorted(os.listdir(migrations_dir)):
        if not fname.endswith(".sql"):
            continue
        version, name = _parse_filename(fname)
        with open(os.path.join(migrations_dir, fname), "r", encoding="utf-8") as fh:
            migrations.append((version, name, fh.read()))
    migrations.sort(key=lambda m: m[0])
    return migrations


def _parse_filename(fname):
    m = _TIMESTAMP_RE.match(fname)
    if m:
        return f"{m.group(1)}-{m.group(2)}-{m.group(3)}", m.group(4)
    m = _LEGACY_RE.match(fname)
    if m:
        return m.group(1), m.group(2)
    raise ValueError(
        f"migration filename {fname!r} must match YYYYMMDD-HHMMSS-MMM-name.sql or NNN_name.sql"
    )


def _split_sql(raw):
    """Split SQL into statements on top-level semicolons, respecting string
    literals, line comments, and BEGIN/END blocks (triggers)."""
    stmts = []
    buf = []
    depth = 0
    in_string = False
    string_char = ""
    i = 0
    n = len(raw)
    while i < n:
        ch = raw[i]
        if in_string:
            buf.append(ch)
            if ch == string_char:
                if i + 1 < n and raw[i + 1] == string_char:
                    buf.append(raw[i + 1])
                    i += 1
                else:
                    in_string = False
            i += 1
            continue
        if ch in ("'", '"'):
            in_string = True
            string_char = ch
            buf.append(ch)
            i += 1
            continue
        if ch == "-" and i + 1 < n and raw[i + 1] == "-":
            while i < n and raw[i] != "\n":
                i += 1
            buf.append("\n")
            continue
        upper = raw[i:].upper()
        if upper.startswith("BEGIN") and (i + 5 >= n or not _is_ident(raw[i + 5])):
            depth += 1
        if upper.startswith("END") and (i + 3 >= n or not _is_ident(raw[i + 3])) and depth > 0:
            depth -= 1
        if ch == ";" and depth == 0:
            s = "".join(buf).strip()
            if s:
                stmts.append(s)
            buf = []
            i += 1
            continue
        buf.append(ch)
        i += 1
    tail = "".join(buf).strip()
    if tail:
        stmts.append(tail)
    return stmts


def _is_ident(ch):
    return ch.isalnum() or ch == "_"


def _is_idempotent_error(exc):
    s = str(exc)
    return (
        "duplicate column name" in s
        or ("table" in s and "already exists" in s)
        or ("index" in s and "already exists" in s)
    )
