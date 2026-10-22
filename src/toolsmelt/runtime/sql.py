"""Databases as a source: `Db` runs a plan's SQL tools (make_api builds it for a DSN base_url), and the rule that
keeps a read_only tool to one plain SELECT.

SQLAlchemy (the `toolsmelt[sql]` extra) is imported on first use, so HTTP-only installs never need it.
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime
import decimal
import os
import re
import threading
import uuid
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

from .http import ApiError

DSN = re.compile(r"([A-Za-z][A-Za-z0-9_]*)(\+[A-Za-z0-9_]+)?://.*", re.S)  # dialect[+driver]://...
NOT_DB = {"http", "https", "file", "ftp", "ws", "wss", "grpc", "grpcs"}
PLACEHOLDER = re.compile(r"(?<![:\w\\]):(\w+)(?![:\w])")  # the bind-parameter rule of SQLAlchemy's text()
# Literals and quoted names, blanked before the read-only checks so their text cannot trip (or hide) anything.
LITERAL = re.compile(r"'(?:[^']|'')*'|\"(?:[^\"]|\"\")*\"|`(?:[^`]|``)*`")
# Statement words that change data or the database; a word followed by '(' is a function (replace(), insert()).
WRITE_WORDS = re.compile(
    r"\b(insert|update|delete|merge|upsert|replace|drop|alter|create|truncate|grant|revoke|attach|detach|pragma"
    r"|vacuum|reindex|copy|call|exec|execute|lock|load|into)\b(?!\s*\()",
    re.I,
)
SQL_NEEDED = "database sources need SQLAlchemy: pip install 'toolsmelt[sql]' plus your database's driver"


def is_dsn(url: str) -> bool:
    """True for a database URL such as sqlite:///shop.db or postgresql://user@host/db (not http, file, ...)."""
    m = DSN.fullmatch(url.strip())
    return bool(m) and m.group(1).lower() not in NOT_DB


def placeholders(sql: str) -> list[str]:
    """The :name placeholders of a statement, in order, each once."""
    return list(dict.fromkeys(PLACEHOLDER.findall(sql)))


def read_only_problem(sql: str) -> str | None:
    """Why `sql` is not one plain SELECT (or WITH ... SELECT), or None.

    Backslashes and dollar quotes are refused: dialects disagree on where such a literal ends, so a statement could
    look like one SELECT here and run as two. Values belong in :placeholders anyway.
    """
    if "\\" in sql:
        return "has a backslash; pass such values through a :placeholder"
    code = LITERAL.sub("''", sql).strip()
    if re.search(r"\$\w*\$", code):
        return "uses dollar quoting; use '...' literals or a :placeholder"
    if "--" in code or "/*" in code or "#" in code:  # shortcut: also refuses PostgreSQL's #> operators
        return "has a comment; remove it (a comment can hide a second statement)"
    if ";" in code.removesuffix(";"):
        return "has more than one statement; keep one SELECT"
    if not re.match(r"(select|with)\b", code, re.I):
        return "must start with SELECT or WITH"
    if m := WRITE_WORDS.search(code):
        return f"contains {m.group(1).upper()}, which changes data; set read_only: false if it should"
    return None


def connect_url(dsn: Any, password: str = "", read_only: bool = True) -> Any:
    """The SQLAlchemy URL to connect with: `password` added, and a SQLite file opened read-only (mode=ro)."""
    from sqlalchemy.engine import make_url

    url = make_url(dsn)
    if password and not url.password:
        url = url.set(password=password)
    db = url.database or ""
    if read_only and url.get_backend_name() == "sqlite" and db and db != ":memory:":
        if not db.startswith("file:"):
            url = url.set(database=Path(db).resolve().as_uri())
        url = url.update_query_dict({"mode": "ro", "uri": "true"})  # the file must exist; never created
    return url


@contextlib.contextmanager
def session(engine: Any, read_only: bool, timeout: int) -> Iterator[Any]:
    """A connection whose transaction is read-only where the database enforces it, and always rolled back unless
    it is a write (then committed)."""
    with engine.connect() as conn:
        if engine.dialect.name == "postgresql":
            if read_only:
                conn.exec_driver_sql("SET TRANSACTION READ ONLY")
            conn.exec_driver_sql(f"SET LOCAL statement_timeout = {int(timeout * 1000)}")
        elif engine.dialect.name in ("mysql", "mariadb"):
            # session-wide, not SET TRANSACTION: DDL commits implicitly and would escape a one-transaction read-only
            # (checked on MariaDB 11.4); set on every checkout so a pooled connection never keeps the other mode
            conn.exec_driver_sql(f"SET SESSION TRANSACTION {'READ ONLY' if read_only else 'READ WRITE'}")
        # shortcut: no statement timeout on other dialects; add MySQL's max_execution_time when a plan needs it
        try:
            yield conn
            if not read_only:
                conn.commit()
        finally:
            if conn.in_transaction():
                conn.rollback()


def jsonable(v: Any) -> Any:
    """A database value as JSON: dates as ISO text, decimals as numbers, bytes as a size note."""
    if isinstance(v, dict):
        return {str(k): jsonable(x) for k, x in v.items()}
    if isinstance(v, list | tuple):
        return [jsonable(x) for x in v]
    if isinstance(v, datetime.date | datetime.time):
        return v.isoformat()
    if isinstance(v, datetime.timedelta):
        return v.total_seconds()
    if isinstance(v, decimal.Decimal):
        return int(v) if v.is_finite() and v == v.to_integral_value() else float(v)
    if isinstance(v, uuid.UUID):
        return str(v)
    if isinstance(v, bytes | bytearray | memoryview):
        return f"<{len(bytes(v))} bytes>"
    return v


class Db:
    """One database for a plan's SQL tools, the `Api` of a DSN base_url. `<env_prefix>_BASE_URL` overrides the DSN;
    the password comes from `auth.env` (else `<env_prefix>_DB_PASSWORD`): the environment, then the keychain-backed
    `secret` callback, like HTTP credentials. The engine is created on the first query."""

    TIMEOUT = 30  # seconds per statement, where the database enforces it

    def __init__(
        self,
        base_url: str,
        env_prefix: str,
        auth: dict[str, str] | None = None,
        secret: Callable[[str], str | None] | None = None,
    ):
        self.env_prefix = env_prefix
        self.base_url = os.environ.get(f"{env_prefix}_BASE_URL") or base_url
        self.password_env = (auth or {}).get("env") or f"{env_prefix}_DB_PASSWORD"
        self.secret = secret
        self._engines: dict[bool, Any] = {}
        self._lock = threading.Lock()

    async def query(self, sql: str, params: dict[str, Any], read_only: bool = True, max_rows: int = 100) -> Any:
        """Run one statement; rows come back as a list of dicts (at most `max_rows`), a write as its row count."""
        if read_only and (problem := read_only_problem(sql)):
            raise ApiError(f"Refusing the statement of a read-only tool: it {problem}.")
        try:
            from sqlalchemy.exc import SQLAlchemyError
        except ImportError:
            raise ApiError(SQL_NEEDED) from None
        try:
            return await asyncio.to_thread(self.fetch, sql, params, read_only, max_rows)
        except SQLAlchemyError as e:  # the driver's message, not SQLAlchemy's echo of statement and parameters
            raise ApiError(f"The database refused the query: {str(getattr(e, 'orig', None) or e)[:300]}") from e

    def fetch(self, sql: str, params: dict[str, Any], read_only: bool, max_rows: int) -> Any:
        from sqlalchemy import text

        with session(self.engine(read_only), read_only, self.TIMEOUT) as conn:
            result = conn.execute(text(sql), params)
            if not result.returns_rows:
                return {"rows_affected": result.rowcount}
            return [{k: jsonable(v) for k, v in row.items()} for row in result.mappings().fetchmany(max_rows)]

    def engine(self, read_only: bool) -> Any:
        from sqlalchemy import create_engine
        from sqlalchemy.engine import make_url

        key = read_only and make_url(self.base_url).get_backend_name() == "sqlite"  # only SQLite opens a ro file
        with self._lock:
            if key not in self._engines:
                env = self.password_env
                password = (os.environ.get(env) or (self.secret and self.secret(env)) or "").strip()
                self._engines[key] = create_engine(connect_url(self.base_url, password, read_only), pool_pre_ping=True)
            return self._engines[key]
