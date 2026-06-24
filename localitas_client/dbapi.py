"""PEP 249 (DB-API 2.0) compliant interface for Localitas databases."""

from .client import LocalitasClient, APIError

apilevel = "2.0"
threadsafety = 1
paramstyle = "qmark"

_READ_PREFIXES = ("SELECT", "PRAGMA", "EXPLAIN", "WITH")


class Error(Exception):
    pass


class InterfaceError(Error):
    pass


class DatabaseError(Error):
    pass


class OperationalError(DatabaseError):
    pass


class ProgrammingError(DatabaseError):
    pass


def _is_read_query(sql: str) -> bool:
    first_word = sql.strip().split()[0].upper() if sql.strip() else ""
    return first_word in _READ_PREFIXES


def connect(database: str, host: str = "localhost:8080", token: str = "") -> "Connection":
    scheme = "http://" if not host.startswith("http") else ""
    base_url = f"{scheme}{host}"
    client = LocalitasClient(base_url, token=token)
    return Connection(client, database)


class Connection:
    def __init__(self, client: LocalitasClient, database: str):
        self._client = client
        self._database = database
        self._closed = False
        self._autocommit = True
        self._pending: list[dict] = []

    @property
    def autocommit(self) -> bool:
        return self._autocommit

    @autocommit.setter
    def autocommit(self, value: bool):
        if self._pending and not value:
            raise ProgrammingError("Cannot disable autocommit with pending statements; commit or rollback first")
        self._autocommit = value

    def cursor(self) -> "Cursor":
        self._check_closed()
        return Cursor(self)

    def commit(self):
        self._check_closed()
        if not self._pending:
            return
        try:
            self._client.sql_transaction(self._database, self._pending)
        except APIError as e:
            raise DatabaseError(str(e)) from e
        finally:
            self._pending = []

    def rollback(self):
        self._check_closed()
        self._pending = []

    def close(self):
        if self._pending:
            self._pending = []
        self._closed = True

    def _check_closed(self):
        if self._closed:
            raise InterfaceError("Connection is closed")

    def _exec_immediate(self, sql: str, args: list):
        try:
            if _is_read_query(sql):
                return self._client.sql_query(self._database, sql, *args)
            return self._client.sql_exec(self._database, sql, *args)
        except APIError as e:
            raise DatabaseError(str(e)) from e

    def _accumulate(self, sql: str, args: list):
        self._pending.append({"sql": sql, "args": args})


class Cursor:
    arraysize = 1

    def __init__(self, connection: Connection):
        self._connection = connection
        self._closed = False
        self._description = None
        self._rowcount = -1
        self._lastrowid = None
        self._rows: list = []
        self._pos = 0

    @property
    def description(self):
        return self._description

    @property
    def rowcount(self) -> int:
        return self._rowcount

    @property
    def lastrowid(self):
        return self._lastrowid

    def execute(self, operation: str, parameters=()):
        self._check_closed()
        args = list(parameters)
        self._description = None
        self._rows = []
        self._pos = 0
        self._rowcount = -1
        self._lastrowid = None

        is_read = _is_read_query(operation)

        if not self._connection.autocommit and not is_read:
            self._connection._accumulate(operation, args)
            self._rowcount = -1
            return

        result = self._connection._exec_immediate(operation, args)

        if is_read and result:
            columns = result.get("columns", [])
            self._description = [
                (col, None, None, None, None, None, None) for col in columns
            ]
            self._rows = result.get("rows", []) or []
            self._rowcount = len(self._rows)
        elif result:
            self._rowcount = result.get("rows_affected", -1)
            self._lastrowid = result.get("last_insert_id")

    def executemany(self, operation: str, seq_of_parameters):
        self._check_closed()
        for params in seq_of_parameters:
            self.execute(operation, params)

    def fetchone(self):
        self._check_closed()
        if self._pos >= len(self._rows):
            return None
        row = tuple(self._rows[self._pos])
        self._pos += 1
        return row

    def fetchmany(self, size=None):
        self._check_closed()
        if size is None:
            size = self.arraysize
        rows = []
        for _ in range(size):
            row = self.fetchone()
            if row is None:
                break
            rows.append(row)
        return rows

    def fetchall(self):
        self._check_closed()
        remaining = self._rows[self._pos:]
        self._pos = len(self._rows)
        return [tuple(r) for r in remaining]

    def close(self):
        self._closed = True

    def _check_closed(self):
        if self._closed:
            raise InterfaceError("Cursor is closed")
