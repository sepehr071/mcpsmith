import json

import httpx
import pytest
from mcp import Client
from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from test_codegen import PLAN, SOURCE

from mcpsmith import server
from mcpsmith.hub import Hub
from mcpsmith.model import save_plan, save_source

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
    monkeypatch.setenv("MCPSMITH_HOME", str(tmp_path / "home"))
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
        tools = json.loads((await c.call_tool("smith_tools", {"connection": "demo-mcp"})).content[0].text)
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
