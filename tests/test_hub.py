import json
from unittest.mock import AsyncMock

import httpx
import pytest
from mcp import Client
from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from test_codegen import PLAN, SOURCE

from toolsmelt import secrets, server
from toolsmelt.hub import Hub
from toolsmelt.model import AuthScheme, save_plan, save_source
from toolsmelt.runtime import signin
from toolsmelt.runtime.signin import xiaomi

pytestmark = pytest.mark.anyio


def upstream(request):
    return httpx.Response(200, json={"data": {"items": [{"id": 1, "tags": [{"name": "x"}]}]}, "id": 7})


def workdir(tmp_path, plan=PLAN, name="wd"):
    wd = tmp_path / name
    wd.mkdir()
    save_plan(plan, wd / "plan.yaml")
    save_source(SOURCE, wd / "source.json")
    return wd


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("TOOLSMELT_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("DEMO_MCP_TOKEN", "t")


@pytest.fixture
def hub():
    h = server.hub
    yield h
    for c in h.connections():
        h.disconnect(c["name"])


def other_plan():
    return PLAN.model_copy(update={"name": "other-mcp", "tools": [PLAN.tools[0].model_copy()]})


async def test_connect_lists_and_calls(tmp_path, hub):
    wd = workdir(tmp_path)
    (wd / "capture.har").write_text("{}")
    r = hub.connect(wd)
    assert r["connection"] == "demo-mcp" and r["tools"] == ["demo_item", "demo_search", "demo_create"]
    assert not (wd / "capture.har").exists()
    assert (tmp_path / "home" / "connections" / "demo-mcp" / "plan.yaml").exists()
    assert json.loads((tmp_path / "home" / "connections" / "demo-mcp" / "meta.json").read_text())["proxy"] is None
    hub._conns["demo-mcp"].api.set_transport(httpx.MockTransport(upstream))
    async with Client(server.mcp) as c:
        names = {t.name for t in (await c.list_tools()).tools}
        assert {"demo_item", "smith_connect"} <= names
        got = await c.call_tool("smith_call", {"connection": "demo-mcp", "tool": "demo_item", "args": {"item_id": 7}})
        assert json.loads(got.content[0].text)["id"] == 7
        direct = await c.call_tool("demo_item", {"item_id": 7})
        assert direct.content[0].text == got.content[0].text
        conns = json.loads((await c.call_tool("smith_connections", {})).content[0].text)
        assert conns[0]["name"] == "demo-mcp" and conns[0]["ok"] and conns[0]["auth"] == "bearer"
        listed = json.loads((await c.call_tool("smith_tools", {"connection": "demo-mcp"})).content[0].text)
        assert listed["instructions"] == PLAN.instructions  # what an exported server would announce
        tools = listed["tools"]
        assert [t["read_only"] for t in tools] == [True, True, False]
        assert "title" not in json.dumps(tools[0]["input_schema"])


async def test_write_tool_refused_by_smith_call(tmp_path, hub):
    hub.connect(workdir(tmp_path))
    with pytest.raises(ToolError, match="own tool"):
        await hub.call("demo-mcp", "demo_create", {"name": "x"})
    with pytest.raises(ToolError, match="unknown"):
        await hub.call("demo-mcp", "nope", {})
    with pytest.raises(ToolError, match="unknown"):
        await hub.call("nope", "demo_item", {})


async def test_smith_call_validates_args_like_the_tool(tmp_path, hub):
    hub.connect(workdir(tmp_path))
    seen = []
    hub._conns["demo-mcp"].api.set_transport(httpx.MockTransport(lambda r: seen.append(r) or upstream(r)))
    for bad in ({"query": "x", "sort": "DROP"}, {"query": "x", "limit": 10_000}, {"limit": 5}):
        with pytest.raises(ToolError, match="bad arguments"):
            await hub.call("demo-mcp", "demo_search", bad)
    assert seen == []
    await hub.call("demo-mcp", "demo_search", {"query": "x", "sort": "cheap", "limit": "5"})
    assert seen[0].url.params["sort"] == "price_asc" and seen[0].url.params["size"] == "5"


async def test_load_all_skips_plan_edited_into_errors(tmp_path, monkeypatch):
    monkeypatch.setenv("OTHER_SERVICE_API_KEY", "sk-not-for-this-site")
    Hub(MCPServer("a")).connect(workdir(tmp_path))
    saved = tmp_path / "home" / "connections" / "demo-mcp" / "plan.yaml"
    other = AuthScheme(name="token", type="bearer", env="OTHER_SERVICE_API_KEY")
    save_plan(PLAN.model_copy(update={"auth": other}), saved)
    m = MCPServer("b")
    h = Hub(m)
    warnings = h.load_all()
    assert len(warnings) == 1 and "demo-mcp" in warnings[0] and "must start with" in warnings[0]
    assert m._tool_manager.list_tools() == [] and not h.connections()[0]["ok"]


async def test_clash_names_tool_and_keeps_first(tmp_path, hub):
    hub.connect(workdir(tmp_path))
    with pytest.raises(ValueError, match="demo_item"):
        hub.connect(workdir(tmp_path, other_plan(), "wd2"))
    assert [c["name"] for c in hub.connections()] == ["demo-mcp"]
    assert not (tmp_path / "home" / "connections" / "other-mcp").exists()


async def test_validation_errors_block_connect(tmp_path, hub):
    bad = PLAN.model_copy(update={"tools": [PLAN.tools[0].model_copy(update={"endpoint": "missing"})]})
    with pytest.raises(ValueError, match="missing"):
        hub.connect(workdir(tmp_path, bad))
    assert hub.connections() == []


async def test_restart_and_corrupt_connection(tmp_path):
    h = Hub(MCPServer("a"))
    h.connect(workdir(tmp_path))
    tool = PLAN.tools[0].model_copy(update={"name": "other_item"})
    h.connect(workdir(tmp_path, other_plan().model_copy(update={"prefix": "other", "tools": [tool]}), "wd2"))
    bad = tmp_path / "home" / "connections" / "demo-mcp" / "plan.yaml"
    bad.write_text("tools: [", encoding="utf-8")
    m = MCPServer("b")
    h2 = Hub(m)
    warnings = h2.load_all()
    assert len(warnings) == 1 and "demo-mcp" in warnings[0]
    assert {t.name for t in m._tool_manager.list_tools()} == {"other_item"}
    by = {c["name"]: c for c in h2.connections()}
    assert by["other-mcp"]["ok"] and not by["demo-mcp"]["ok"] and by["demo-mcp"]["warning"]


async def test_mutation_looking_post_warns(tmp_path, hub):
    t = PLAN.tools[2].model_copy(update={"read_only": True})
    r = hub.connect(workdir(tmp_path, PLAN.model_copy(update={"tools": [t]})))
    assert any("demo_create" in w for w in r["warnings"])


async def test_listeners_notified(tmp_path, hub):
    wd = workdir(tmp_path)
    async with Client(server.mcp) as c:
        async with c.listen(tools_list_changed=True) as sub:
            await c.call_tool("smith_connect", {"workdir": str(wd)})
            event = await sub.__anext__()
        assert event is not None
        assert "demo_item" in {t.name for t in (await c.list_tools()).tools}


async def test_reconnect_from_saved_dir_keeps_plan(tmp_path):
    h = Hub(MCPServer("a"))
    h.connect(workdir(tmp_path))
    saved = tmp_path / "home" / "connections" / "demo-mcp"
    h.connect(saved)
    assert (saved / "plan.yaml").exists() and (saved / "source.json").exists()
    h2 = Hub(MCPServer("b"))
    assert h2.load_all() == [] and h2.connections()[0]["ok"]


async def test_connection_proxy_has_no_credentials(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    (home / "browser.json").write_text(json.dumps({"port": 1, "proxy": "u:p@h:1"}))
    h = Hub(MCPServer("a"))
    h.connect(workdir(tmp_path))
    meta = (home / "connections" / "demo-mcp" / "meta.json").read_text()
    assert "u:p" not in meta and json.loads(meta)["proxy"] == "http://h:1"


async def test_login_token_step_and_signin_link(tmp_path, hub, monkeypatch):
    hub.connect(workdir(tmp_path))
    assert "toolsmelt secret set demo-mcp DEMO_MCP_TOKEN" in (await hub.login("demo-mcp"))["next"]

    saved = {}
    monkeypatch.setattr(xiaomi, "start_login", AsyncMock(return_value=("https://login.example", "https://poll")))
    monkeypatch.setattr(xiaomi, "finish_login", AsyncMock(return_value={"userId": "1"}))
    monkeypatch.setattr(secrets, "get", lambda conn, env: None)  # not signed in yet
    monkeypatch.setattr(secrets, "set", lambda conn, env, value: saved.update({(conn, env): value}))
    auth = AuthScheme(name="mi", type="signin", provider="xiaomi", env="DEMO_MCP_SESSION")
    hub.connect(workdir(tmp_path, PLAN.model_copy(update={"auth": auth}), "mi"))
    r = await hub.login("demo-mcp")
    assert r["open"] == "https://login.example" and not r["signed_in"]
    await signin._logins["demo-mcp"]
    assert json.loads(saved["demo-mcp", "DEMO_MCP_SESSION"]) == {"userId": "1"}
