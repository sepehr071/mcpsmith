import json
import os
import shutil
import sqlite3
import subprocess
import sys

import pytest
from mcp import Client

from mcpsmith.check import _import_server
from mcpsmith.codegen import generate
from mcpsmith.design import digest, draft, validate
from mcpsmith.importers import load
from mcpsmith.model import Tool, ToolArg
from mcpsmith.runtime import ApiError
from mcpsmith.runtime.interpret import make_api, make_tool, run_tool, tool_spec
from mcpsmith.runtime.sql import Db, connect_url, read_only_problem

sa = pytest.importorskip("sqlalchemy")
pytestmark = pytest.mark.anyio

SCHEMA = """
create table customers(id integer primary key, name text not null, email text unique, city text);
create table orders(id integer primary key, customer_id integer not null references customers(id), status text,
    total numeric(10, 2), created_at date);
create index ix_orders_status on orders(status);
create table order_items(id integer primary key, order_id integer references orders(id), sku text, qty integer);
insert into customers values (1, 'Ali', 'ali@shop.test', 'Tehran'), (2, 'Sara', 'sara@shop.test', 'Shiraz');
insert into orders values (10, 1, 'paid', 12.50, '2026-10-01'), (11, 1, 'new', 3, '2026-10-02'),
    (12, 2, 'paid', 7.25, '2026-10-03');
insert into order_items values (100, 10, 'tea', 2), (101, 10, 'cup', 1), (102, 11, 'tea', 1), (103, 12, 'pot', 1);
"""


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "shop.db"
    with sqlite3.connect(path) as c:
        c.executescript(SCHEMA)
    c.close()
    return path


@pytest.fixture
def flow(db):
    source = load(f"sqlite:///{db.as_posix()}")
    plan = draft(source)
    return source, plan


def _rows(db):
    with sqlite3.connect(db) as c:
        n = c.execute("select count(*) from customers").fetchone()[0]
    c.close()
    return n


def test_inspect_reads_tables_keys_and_masked_samples(flow):
    source, _ = flow
    assert source.kind == "sql" and source.title == "shop" and source.base_url.endswith("/shop.db")
    eps = {e.id: e for e in source.endpoints}
    assert list(eps) == ["TABLE main.order_items", "TABLE main.orders", "TABLE main.customers"]  # most rows first
    orders = eps["TABLE main.orders"]
    assert orders.method == "SQL" and orders.path == "orders" and orders.count == 3
    cols = {p.name: p for p in orders.params}
    assert cols["id"].type == "integer" and "primary key" in cols["id"].description
    assert "references customers.id" in cols["customer_id"].description and "indexed" in cols["status"].description
    assert cols["total"].type == "number" and orders.response_example[0]["total"] == 12.5
    customers = eps["TABLE main.customers"]
    assert "shop.test" not in json.dumps(source.model_dump())  # emails masked in the examples
    assert customers.response_example[0]["email"] == "user@example.com"
    d = digest(source)
    assert "## Tables (3" in d and "customer_id INTEGER, references customers.id" in d


async def test_draft_validates_and_runs_against_the_database(flow, db):
    source, plan = flow
    assert [i for i in validate(plan, source) if i.level == "error"] == []
    assert plan.base_url == source.base_url and plan.auth is None
    tools = {t.name: t for t in plan.tools}
    assert set(tools) == {
        *("shop_list_order_items", "shop_get_order_items_by_id", "shop_list_orders"),
        *("shop_get_orders_by_id", "shop_list_customers", "shop_get_customers_by_id"),
    }
    # optional filters put the column first: PostgreSQL cannot type a bare ':x IS NULL' (checked on a real server)
    assert '("customer_id" = :customer_id OR :customer_id IS NULL)' in tools["shop_list_orders"].sql
    api = make_api(plan, source)
    assert isinstance(api, Db)

    async def call(name, **values):
        return await make_tool(tools[name], plan, source, api)(**values)

    paid = await call("shop_list_orders", status="paid")
    assert [r["id"] for r in paid["items"]] == [10, 12] and paid["count"] == 2
    assert (await call("shop_list_orders", customer_id=1, limit=1))["items"] == [
        {"id": 10, "customer_id": 1, "status": "paid", "total": 12.5, "created_at": "2026-10-01"}
    ]
    assert (await call("shop_get_customers_by_id", id=2))["email"] == "sara@shop.test"  # live results are real


async def test_read_only_statements_refused_by_validate_and_runtime(flow, db):
    source, plan = flow
    api = make_api(plan, source)
    for sql in ("DELETE FROM customers", "SELECT 1; DROP TABLE customers", "SELECT 1 /* x */; DELETE FROM customers"):
        tool = Tool(name="shop_bad", title="Bad", description="Bad.", endpoint="TABLE main.customers", sql=sql)
        bad = plan.model_copy(update={"tools": [tool]})
        assert any("read_only tool" in i.message for i in validate(bad, source) if i.level == "error")
        with pytest.raises(ApiError, match="Refusing"):
            await run_tool(api, tool_spec(tool, bad, source), {})
    with pytest.raises(sa.exc.OperationalError, match="readonly"):  # the file itself is opened read-only too
        api.fetch("DELETE FROM customers", {}, True, 10)
    assert _rows(db) == 2


async def test_write_tool_commits(flow, db):
    source, plan = flow
    city = ToolArg(name="city", description="City, e.g. Yazd.", required=True, to="sql.city")
    tool = Tool(
        name="shop_move", title="Move", description="Move customer 1.", endpoint="TABLE main.customers",
        sql="UPDATE customers SET city = :city WHERE id = 1", args=[city], read_only=False,
    )  # fmt: skip
    plan = plan.model_copy(update={"tools": [tool]})
    assert [i for i in validate(plan, source) if i.level == "error"] == []
    assert await run_tool(make_api(plan, source), tool_spec(tool, plan, source), {"city": "Yazd"}) == {
        "rows_affected": 1
    }
    with sqlite3.connect(db) as c:
        assert c.execute("select city from customers where id = 1").fetchone() == ("Yazd",)
    c.close()


def test_read_only_rule():
    for ok in ("SELECT 1", "with t as (select 1 as x) select x from t;", "SELECT replace(name, 'a', 'b') FROM t",
               "SELECT * FROM t WHERE note = 'drop; -- it' AND d > '12:30'", "SELECT created_at, updated_by FROM t"):  # fmt: skip
        assert read_only_problem(ok) is None, ok
    for bad in ("UPDATE t SET x = 1", "SELECT 1;SELECT 2", "SELECT 1 -- x", "SELECT $$a$$", "SELECT 'a\\' FROM t",
                "WITH d AS (DELETE FROM t RETURNING *) SELECT * FROM d", "SELECT * INTO t2 FROM t", "PRAGMA x"):  # fmt: skip
        assert read_only_problem(bad), bad


def test_password_never_stored(monkeypatch):
    with pytest.raises(ValueError) as e:
        load("nosuchdialect://bob:s3cret@db.test/shop")
    assert "s3cret" not in str(e.value)
    from mcpsmith.importers.sql import public_dsn

    url = sa.engine.make_url("postgresql://bob:s3cret@db.test:5433/shop?sslmode=require&password=x")
    assert public_dsn(url) == "postgresql://bob@db.test:5433/shop?sslmode=require"
    assert connect_url("postgresql://bob@db.test/shop", "pw").password == "pw"
    seen = []
    monkeypatch.delenv("SHOP_MCP_DB_PASSWORD", raising=False)
    api = Db("sqlite://", "SHOP_MCP", auth={"type": "basic", "env": "SHOP_MCP_PW"}, secret=seen.append)
    api.engine(True)
    assert seen == ["SHOP_MCP_PW"]  # the keychain callback, like HTTP credentials


async def test_generated_project_replays_and_runs(flow, db, tmp_path):
    source, plan = flow
    out = tmp_path / "shop-mcp"
    generate(plan, out, source)
    code = (out / "src/shop_mcp/server.py").read_text(encoding="utf-8")
    assert "from mcpsmith.runtime.sql import Db" in code and "api = Db(" in code and '"sql": \'SELECT' in code
    assert '"mcpsmith[sql]>=0.6,<1"' in (out / "pyproject.toml").read_text(encoding="utf-8")
    assert "driver" in (out / "README.md").read_text(encoding="utf-8")
    assert "shop.test" not in "".join(p.read_text(encoding="utf-8") for p in (out / "tests/fixtures").iterdir())
    env = {k: v for k, v in os.environ.items() if not k.startswith("SHOP_MCP")} | {"PYTHONPATH": str(out / "src")}
    r = subprocess.run(
        [sys.executable, "-m", "pytest", str(out / "tests"), "-q", "-p", "no:cacheprovider"],
        cwd=out, env=env, capture_output=True, text=True,
    )  # fmt: skip
    assert r.returncode == 0, r.stdout + r.stderr
    assert "6 passed, 6 deselected" in r.stdout
    if shutil.which("ruff"):
        lint = subprocess.run(["ruff", "check", str(out)], capture_output=True, text=True)
        assert lint.returncode == 0, lint.stdout
    gen = _import_server(out / "src", plan.package)
    async with Client(gen.mcp) as c:
        result = await c.call_tool("shop_get_customers_by_id", {"id": 1})
    assert json.loads(result.content[0].text)["email"] == "ali@shop.test"
