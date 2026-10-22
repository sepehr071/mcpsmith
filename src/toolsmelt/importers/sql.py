"""A database URL (anything SQLAlchemy 2 opens: sqlite:///shop.db, postgresql://user@host/db, ...) -> Source.

One endpoint per table or view: its columns as params, up to 3 scrubbed sample rows as the response example. The
database is read in a read-only transaction (a SQLite file is opened read-only). The password, from the URL or
TOOLSMELT_DB_PASSWORD, is used to connect and never stored.
"""

from __future__ import annotations

import decimal
from pathlib import Path
from typing import Any

from toolsmelt import getenv
from toolsmelt.importers.har import _is_secret as is_secret
from toolsmelt.importers.har import _scrub as scrub  # toolsmelt.scrub's rules (importing it here would be a cycle)
from toolsmelt.model import AuthScheme, Endpoint, Param, Source
from toolsmelt.runtime.sql import SQL_NEEDED, Db, connect_url, is_dsn, jsonable, session

MAX_TABLES = 200
SAMPLE_ROWS = 3


def sniff(text: str) -> bool:
    return is_dsn(text)


def public_dsn(url: Any) -> str:
    """The URL without its password or secret-looking query keys, a SQLite path made absolute: safe to store."""
    from sqlalchemy.engine import URL

    db = url.database
    if url.get_backend_name() == "sqlite" and db and db != ":memory:" and not db.startswith("file:"):
        db = Path(db).resolve().as_posix()  # a generated server runs from another directory
    query = {k: v for k, v in url.query.items() if not is_secret(k)}
    return URL.create(url.drivername, url.username, None, url.host, url.port, db, query).render_as_string(False)


def parse(dsn: str, max_tables: int = MAX_TABLES) -> Source:
    try:
        import sqlalchemy as sa
    except ImportError:
        raise ImportError(SQL_NEEDED) from None
    try:
        url = sa.engine.make_url(dsn.strip())
    except sa.exc.ArgumentError:  # its message repeats the URL, password included
        raise ValueError("not a database URL SQLAlchemy can read; use dialect+driver://user@host/db") from None
    public = public_dsn(url)
    password = url.password or getenv("DB_PASSWORD", "")
    try:
        engine = sa.create_engine(connect_url(url, password))
    except sa.exc.NoSuchModuleError:
        raise ValueError(f"{public}: SQLAlchemy has no dialect {url.drivername!r}") from None
    except ImportError as e:
        raise ImportError(f"{public}: {e}; install the driver for {url.drivername}") from None
    try:
        return _read(sa, engine, url, public, bool(password), max_tables)
    except sa.exc.SQLAlchemyError as e:  # the driver's message, not SQLAlchemy's echo of the statement
        raise ValueError(f"{public}: {getattr(e, 'orig', None) or e}") from None
    finally:
        engine.dispose()


def _read(sa: Any, engine: Any, url: Any, public: str, has_password: bool, max_tables: int) -> Source:
    dialect = engine.dialect.name
    notes: list[str] = []
    with session(engine, True, Db.TIMEOUT) as conn:
        insp = sa.inspect(conn)
        default = insp.default_schema_name
        schemas = [default]
        if dialect == "postgresql":  # shortcut: other dialects read the default schema only
            schemas = [s for s in insp.get_schema_names() if not s.startswith("pg_") and s != "information_schema"]
        found = [
            (s, n, kind)
            for s in schemas
            for kind, names in (("TABLE", insp.get_table_names(schema=s)), ("VIEW", insp.get_view_names(schema=s)))
            for n in names
        ]
        counts = {(s, n): _count(sa, conn, dialect, s, n, default) for s, n, kind in found if kind == "TABLE"}
        found.sort(key=lambda t: (-(counts.get(t[:2]) or 0), t[2], t[0], t[1]))  # biggest tables first
        if len(found) > max_tables:
            rest = [n for _, n, _ in found[max_tables:]]
            notes.append(f"Showing {max_tables} of {len(found)} tables and views; left out: {', '.join(rest[:50])}.")
            found = found[:max_tables]
        endpoints = [
            _endpoint(sa, insp, engine, s, n, kind, default, counts.get((s, n)), notes) for s, n, kind in found
        ]
    db = url.database or ""
    title = Path(db).stem if dialect == "sqlite" and db else db or url.host or dialect
    tables = sum(kind == "TABLE" for _, _, kind in found)
    return Source(
        kind="sql",
        title=title,
        description=" ".join([f"{dialect} database: {tables} tables, {len(found) - tables} views.", *notes]),
        base_url=public,
        # The password goes to <env_prefix>_DB_PASSWORD (env or keychain); the plan only names the variable.
        auth=[AuthScheme(name="db_password", type="basic", param="password")] if has_password else [],
        endpoints=endpoints,
    )


def _table(sa: Any, schema: str, name: str, default: str) -> Any:
    return sa.table(name, schema=None if schema == default else schema)


def _count(sa: Any, conn: Any, dialect: str, schema: str, name: str, default: str) -> int | None:
    """Rows in a table where that is cheap: COUNT(*) on SQLite (a local file), the planner's estimate elsewhere."""
    if dialect == "sqlite":
        return conn.execute(sa.select(sa.func.count()).select_from(_table(sa, schema, name, default))).scalar()
    if dialect == "postgresql":
        q = "SELECT CAST(c.reltuples AS bigint) FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace"
        n = conn.execute(sa.text(q + " WHERE n.nspname = :s AND c.relname = :t"), {"s": schema, "t": name}).scalar()
        return n if n is not None and n >= 0 else None  # -1: never analyzed
    if dialect in ("mysql", "mariadb"):
        q = "SELECT table_rows FROM information_schema.tables WHERE table_schema = :s AND table_name = :t"
        return conn.execute(sa.text(q), {"s": schema, "t": name}).scalar()
    return None


def _arg_type(t: Any) -> str:
    try:
        py = t.python_type
    except NotImplementedError:
        return "string"
    types = {bool: "boolean", int: "integer", float: "number", decimal.Decimal: "number", dict: "object", list: "array"}
    return types.get(py, "string")


def _endpoint(
    sa: Any, insp: Any, engine: Any, schema: str, name: str, kind: str, default: str, count: int | None, notes: list
) -> Endpoint:
    pk: list[str] = []
    refs: dict[str, str] = {}
    indexed: dict[str, bool] = {}  # first column of an index -> unique
    if kind == "TABLE":
        pk = (insp.get_pk_constraint(name, schema=schema) or {}).get("constrained_columns") or []
        for fk in insp.get_foreign_keys(name, schema=schema):
            to = (
                fk["referred_table"]
                if fk.get("referred_schema") in (None, default)
                else ".".join([fk["referred_schema"], fk["referred_table"]])
            )
            refs |= {c: f"{to}.{r}" for c, r in zip(fk["constrained_columns"], fk["referred_columns"], strict=False)}
        for ix in [*insp.get_indexes(name, schema=schema), *insp.get_unique_constraints(name, schema=schema)]:
            if cols := [c for c in ix.get("column_names") or [] if c]:
                indexed[cols[0]] = indexed.get(cols[0], False) or bool(ix.get("unique", True))
    rows: list[dict[str, Any]] = []
    try:  # its own transaction: on PostgreSQL one failed query would end the reflection's
        with session(engine, True, Db.TIMEOUT) as conn:
            q = sa.select(sa.text("*")).select_from(_table(sa, schema, name, default)).limit(SAMPLE_ROWS)
            rows = [{k: jsonable(v) for k, v in r.items()} for r in conn.execute(q).mappings()]
    except sa.exc.SQLAlchemyError as e:
        notes.append(f"No sample rows of {name}: {str(getattr(e, 'orig', None) or e)[:120]}.")
    rows = scrub(rows)  # personal values masked; the live tools return the real ones
    params = []
    for c in insp.get_columns(name, schema=schema):
        try:
            sql_type = str(c["type"].compile(dialect=engine.dialect))
        except sa.exc.CompileError:
            sql_type = type(c["type"]).__name__
        bits = [sql_type]
        bits += ["primary key"] if c["name"] in pk else []
        bits += [f"references {refs[c['name']]}"] if c["name"] in refs else []
        bits += ["unique" if indexed[c["name"]] else "indexed"] if c["name"] in indexed else []
        bits += [] if c.get("nullable", True) else ["not null"]
        desc = ", ".join(bits) + (f"; {c['comment']}" if c.get("comment") else "")
        example = next((r[c["name"]] for r in rows if r.get(c["name"]) is not None), None)
        # Param has no 'column' location; "query" reads as "filterable", and SQL tools never send it anywhere.
        params.append(
            Param(name=c["name"], location="query", type=_arg_type(c["type"]), description=desc, example=example)
        )
    keys = [f"Primary key: {', '.join(pk)}."] if pk else []
    keys += [f"Foreign keys: {', '.join(f'{c} -> {t}' for c, t in refs.items())}."] if refs else []
    size = f" {count} rows." if count is not None else ""
    return Endpoint(
        id=f"{kind} {schema}.{name}",
        method="SQL",
        path=name,
        tags=[] if schema == default else [schema],  # the schema to qualify the name with; empty for the default
        summary=f"{kind.lower()} {name}",
        description=" ".join([*keys, size.strip()]).strip(),
        params=params,
        response_example=rows or None,
        count=max(count or 1, 1),
    )
