import json

import httpx
import pytest
from mcp import Client
from mcp.server import MCPServer
from test_codegen import PLAN, SOURCE

from mcpsmith.check import _import_server
from mcpsmith.codegen import generate
from mcpsmith.model import Plan, Tool, ToolArg
from mcpsmith.runtime import transform_in
from mcpsmith.runtime.interpret import make_api, make_tool, request_kwargs
from mcpsmith.runtime.server import _strip_titles, compact

pytestmark = pytest.mark.anyio


def _handler(seen):
    def handler(request):
        seen.append(request)
        return httpx.Response(200, json={"data": {"items": [{"id": 1, "tags": [{"name": "x"}]}, {"id": 2}]}, "id": 7})

    return handler


def _hub(plan, source, seen):
    api = make_api(plan, source)
    api.set_transport(httpx.MockTransport(_handler(seen)))
    mcp = MCPServer(plan.name)
    for t in plan.tools:
        mcp.add_tool(compact(make_tool(t, plan, source, api)), structured_output=False)
    for t in mcp._tool_manager.list_tools():
        _strip_titles(t.parameters)
    return mcp


@pytest.fixture
def generated(tmp_path, monkeypatch):
    monkeypatch.setenv("DEMO_MCP_TOKEN", "t")
    generate(PLAN, tmp_path / "demo-mcp", SOURCE)
    return _import_server(tmp_path / "demo-mcp" / "src", PLAN.package)


def _seen(request):
    return (request.method, str(request.url), request.content, dict(request.headers))


async def test_request_kwargs_matches_codegen(generated, monkeypatch):
    seen_gen, seen_int = [], []
    generated.api.set_transport(httpx.MockTransport(_handler(seen_gen)))
    hub = _hub(PLAN, SOURCE, seen_int)
    async with Client(generated.mcp) as a, Client(hub) as b:
        for t in PLAN.tools:
            want = await a.call_tool(t.name, t.example)
            got = await b.call_tool(t.name, t.example)
            assert got.content[0].text == want.content[0].text
    assert len(seen_gen) == len(seen_int) == 3
    for want, got in zip(seen_gen, seen_int, strict=True):
        assert _seen(got) == _seen(want)
    assert seen_int[0].headers["authorization"] == "Bearer t"
    assert json.loads(seen_int[2].content) == {"item": {"name": "x", "kind": "tea"}}


async def test_signature_and_schema(generated):
    hub = _hub(PLAN, SOURCE, [])
    async with Client(generated.mcp) as a, Client(hub) as b:
        want = {t.name: t.input_schema for t in (await a.list_tools()).tools}
        got = {t.name: t.input_schema for t in (await b.list_tools()).tools}
    assert got == want and set(got) == {t.name for t in PLAN.tools}


def test_map_and_transform_applied():
    tool = Tool(
        name="demo_x",
        title="X",
        description="Does x.",
        endpoint="GET /x",
        args=[
            ToolArg(name="sort", description="Order.", map={"newest": "date_desc"}, to="query.sort"),
            ToolArg(name="since", description="Date.", transform="date->unix", to="query.since"),
        ],
        fixed={"header.X-A": "1"},
    )
    got = request_kwargs(tool, {"sort": "newest", "since": "2024-01-01"}, "UTC")
    assert got == {
        "query": {"sort": "date_desc", "since": transform_in("date->unix", "2024-01-01", "UTC")},
        "headers": {"X-A": "1"},
    }
    assert request_kwargs(tool, {"sort": "other", "since": None}, "UTC")["query"]["sort"] == "other"


async def test_body_whole_target_merges_keys():
    plan = Plan.model_validate(
        PLAN.model_dump(mode="json")
        | {
            "tools": [
                {
                    "name": "demo_post",
                    "title": "Post",
                    "description": "Posts.",
                    "endpoint": "POST /p",
                    "read_only": False,
                    "args": [
                        {"name": "payload", "type": "object", "description": "Body.", "to": "body"},
                        {"name": "x", "description": "X.", "to": "body.x"},
                    ],
                }
            ]
        }
    )
    seen = []
    async with Client(_hub(plan, None, seen)) as c:
        await c.call_tool("demo_post", {"payload": {"a": 1}, "x": "y"})
    assert json.loads(seen[0].content) == {"a": 1, "x": "y"}
