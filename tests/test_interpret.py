import json

import httpx
import pytest
from mcp import Client
from mcp.server import MCPServer
from test_codegen import PLAN, SOURCE

from mcpsmith.check import _import_server
from mcpsmith.codegen import generate
from mcpsmith.model import Output, Paginate, Plan, Step, Tool, ToolArg
from mcpsmith.runtime import ApiError, transform_in
from mcpsmith.runtime.interpret import make_api, make_tool, run_tool, tool_spec
from mcpsmith.runtime.server import _strip_titles, compact

pytestmark = pytest.mark.anyio


def _handler(seen):
    def handler(request):
        seen.append(request)
        return httpx.Response(200, json={"data": {"items": [{"id": 1, "tags": [{"name": "x"}]}, {"id": 2}]}, "id": 7})

    return handler


def _hub(plan, source, seen, handler=_handler):
    api = make_api(plan, source)
    api.set_transport(httpx.MockTransport(handler(seen)))
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


class FakeApi:
    """Records each `call` and answers with `reply(method, url, kwargs)`."""

    base_url = "https://api.demo.test/v1"

    def __init__(self, reply=lambda method, url, kw: {}):
        self.calls, self.reply = [], reply

    async def call(self, method, url, **kw):
        self.calls.append((method, url, kw))
        return self.reply(method, url, kw)


def _tool(**kw):
    return Tool(**{"name": "demo_x", "title": "X", "description": "Does x.", "endpoint": "GET /x"} | kw)


async def _run(tool, values=None, reply=lambda m, u, kw: {}, tz="UTC"):
    api = FakeApi(reply)
    result = await run_tool(api, tool_spec(tool, PLAN, None), values or {}, tz)
    return result, api.calls


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


async def test_map_and_transform_applied():
    tool = _tool(
        args=[
            ToolArg(name="sort", description="Order.", map={"newest": "date_desc"}, to="query.sort"),
            ToolArg(name="since", description="Date.", transform="date->unix", to="query.since"),
        ],
        fixed={"header.X-A": "1"},
    )
    _, calls = await _run(tool, {"sort": "newest", "since": "2024-01-01"})
    assert calls == [
        (
            "GET",
            "/x",
            {
                "query": {"sort": "date_desc", "since": transform_in("date->unix", "2024-01-01", "UTC")},
                "headers": {"X-A": "1"},
            },
        )
    ]
    _, calls = await _run(tool, {"sort": "other", "since": None})
    assert calls[0][2]["query"]["sort"] == "other"


FORM_PLAN = Plan.model_validate(
    PLAN.model_dump(mode="json")
    | {
        "tools": [
            {
                "name": "demo_login",
                "title": "Log in",
                "description": "Posts a form.",
                "endpoint": "POST /login",
                "read_only": False,
                "body_encoding": "form",
                "args": [
                    {"name": "user", "description": "User.", "required": True, "to": "body.user"},
                    {"name": "days", "type": "integer", "description": "Days.", "to": "body.days"},
                ],
                "fixed": {"body.remember": True},
                "example": {"user": "a b", "days": 3},
            }
        ]
    }
)


async def test_form_tool_matches_codegen(tmp_path, monkeypatch):
    monkeypatch.setenv("DEMO_MCP_TOKEN", "t")
    generate(FORM_PLAN, tmp_path / "demo-mcp")
    assert '"body_encoding": "form"' in (tmp_path / "demo-mcp/src/demo_mcp/server.py").read_text(encoding="utf-8")
    gen = _import_server(tmp_path / "demo-mcp" / "src", FORM_PLAN.package)
    seen_gen, seen_int = [], []
    gen.api.set_transport(httpx.MockTransport(_handler(seen_gen)))
    t = FORM_PLAN.tools[0]
    async with Client(gen.mcp) as a, Client(_hub(FORM_PLAN, None, seen_int)) as b:
        assert (await a.call_tool(t.name, t.example)).content[0].text == (await b.call_tool(t.name, t.example)).content[
            0
        ].text
    assert _seen(seen_int[0]) == _seen(seen_gen[0])
    assert seen_int[0].content == b"user=a+b&days=3&remember=true"
    assert (await _run(t, t.example | {"days": None}))[1][0][2]["encoding"] == "form"
    assert (
        "encoding"
        not in (await _run(PLAN.tools[2].model_copy(update={"endpoint": "POST /i"}), {"name": "x", "note": None}))[1][
            0
        ][2]
    )


async def test_array_body_with_body_fields_refused():
    tool = _tool(
        endpoint="POST /x",
        read_only=False,
        args=[
            ToolArg(name="ids", type="array", description="Ids.", to="body"),
            ToolArg(name="x", description="X.", to="body.x"),
        ],
    )
    with pytest.raises(ApiError, match="list body cannot also take body"):
        await _run(tool, {"ids": [1], "x": "y"})


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


# ---------- pages and steps ----------

QUERY = ToolArg(name="query", description="Text.", required=True, to="query.q")
FLOW_PLAN = PLAN.model_copy(
    update={
        "tools": [
            _tool(
                name="demo_pages",
                endpoint="GET /search",
                args=[QUERY],
                paginate=Paginate(param="query.page", start=0, max_pages=2),
                output=Output(root="hits", fields={"id": "objectID"}),
                example={"query": "x"},
            ),
            _tool(
                name="demo_details",
                endpoint="GET /search",
                args=[QUERY],
                steps=[
                    Step(endpoint="GET /items/{id}", args={"path.id": "$item.objectID"}, for_each="hits", max_items=2)
                ],
                output=Output(fields={"id": "id", "title": "title"}),
                example={"query": "x"},
            ),
        ]
    }
)


def _flow_handler(seen):
    def handler(request):
        seen.append(request)
        if request.url.path.endswith("/search"):
            page = request.url.params.get("page", "0")
            return httpx.Response(200, json={"hits": [{"objectID": f"{page}-{i}"} for i in range(3)]})
        return httpx.Response(200, json={"id": request.url.path.rsplit("/", 1)[-1], "title": "t", "html": "<p>"})

    return handler


async def test_pages_and_steps_match_codegen(tmp_path, monkeypatch):
    monkeypatch.setenv("DEMO_MCP_TOKEN", "t")
    generate(FLOW_PLAN, tmp_path / "demo-mcp")
    gen = _import_server(tmp_path / "demo-mcp" / "src", FLOW_PLAN.package)
    seen_gen, seen_int = [], []
    gen.api.set_transport(httpx.MockTransport(_flow_handler(seen_gen)))
    results = []
    async with Client(gen.mcp) as a, Client(_hub(FLOW_PLAN, None, seen_int, _flow_handler)) as b:
        for t in FLOW_PLAN.tools:
            want, got = await a.call_tool(t.name, t.example), await b.call_tool(t.name, t.example)
            assert got.content[0].text == want.content[0].text
            results.append(json.loads(got.content[0].text))
    assert [_seen(r) for r in seen_int] == [_seen(r) for r in seen_gen]
    assert [str(r.url).removeprefix(FLOW_PLAN.base_url) for r in seen_int] == [
        "/search?q=x&page=0",
        "/search?q=x&page=1",
        "/search?q=x",
        "/items/0-0",
        "/items/0-1",
    ]
    ids = [f"{p}-{i}" for p in (0, 1) for i in range(3)]
    assert results[0] == {"items": [{"id": i} for i in ids], "count": 6, "pages": 2, "more": True}
    assert results[1] == {"items": [{"id": "0-0", "title": "t"}, {"id": "0-1", "title": "t"}], "count": 2}


def _list(root, items, **more):
    return {root: items, **more}


async def test_page_styles():
    pages = {0: [1, 2], 10: [3], 20: []}
    tool = _tool(paginate=Paginate(style="offset", param="query.from", start=0, size=10), output=Output(root="r"))
    result, calls = await _run(tool, reply=lambda m, u, kw: _list("r", pages[kw["query"]["from"]]))
    assert result == {"items": [1, 2, 3], "count": 3} and len(calls) == 3  # an empty page ends it

    replies = iter([_list("r", [1], next="c2"), _list("r", [2])])
    tool = _tool(paginate=Paginate(style="cursor", param="header.X-Cursor", next="next"), output=Output(root="r"))
    result, calls = await _run(tool, reply=lambda m, u, kw: next(replies))
    assert result == {"items": [1, 2], "count": 2}  # no next: done
    assert calls[0][2]["headers"]["X-Cursor"] is None and calls[1][2]["headers"]["X-Cursor"] == "c2"

    replies = iter(
        [_list("r", [1], n="https://api.demo.test/v1/x?p=2"), _list("r", [2], n="/v1/x?p=3"), _list("r", [3])]
    )
    tool = _tool(
        paginate=Paginate(style="next_url", next="n", max_pages=5), fixed={"header.X-A": "1"}, output=Output(root="r")
    )
    result, calls = await _run(tool, reply=lambda m, u, kw: next(replies))
    assert result == {"items": [1, 2, 3], "count": 3}
    assert calls[1] == ("GET", "https://api.demo.test/v1/x?p=2", {"headers": {"X-A": "1"}, "cookies": None})
    assert calls[2][1] == "https://api.demo.test/v1/x?p=3"

    tool = _tool(paginate=Paginate(param="query.page", max_pages=5), output=Output(root="r", max_items=3))
    result, calls = await _run(tool, reply=lambda m, u, kw: _list("r", [kw["query"]["page"]] * 2))
    assert result == {"items": [1, 1, 2], "count": 4} and len(calls) == 2  # enough items: no third page


async def test_next_url_stays_on_allowed_hosts():
    api = make_api(FLOW_PLAN, None)
    api.set_transport(httpx.MockTransport(lambda r: httpx.Response(200, json={"r": [1], "n": "https://evil.test/x"})))
    tool = _tool(paginate=Paginate(style="next_url", next="n"), output=Output(root="r"))
    with pytest.raises(ApiError, match="unexpected host evil.test"):
        await run_tool(api, tool_spec(tool, PLAN, None), {})


async def test_steps_use_args_prev_and_items():
    sort = ToolArg(name="sort", description="Order.", map={"new": "date_desc"}, to="query.sort")
    tool = _tool(
        args=[sort],
        steps=[
            Step(endpoint="GET /lists/{id}", args={"path.id": "$prev.data.id", "query.o": "$arg.sort"}),
            Step(endpoint="POST /items", args={"body.ids": "$item.id"}, fixed={"body.full": True}, for_each="rows"),
        ],
        read_only=False,
        output=Output(fields={"id": "id"}),
    )
    replies = {"/x": {"data": {"id": 9}}, "/lists/{id}": {"rows": [{"id": 1}, {"id": 2}]}, "/items": {"id": "ok"}}
    result, calls = await _run(tool, {"sort": "new"}, reply=lambda m, u, kw: replies[u])
    assert calls[1] == ("GET", "/lists/{id}", {"path": {"id": 9}, "query": {"o": "date_desc"}})
    assert calls[2:] == [("POST", "/items", {"body": {"full": True, "ids": i}}) for i in (1, 2)]
    assert result == {"items": [{"id": "ok"}, {"id": "ok"}], "count": 2}
    replies["/lists/{id}"] = {"rows": "not a list"}
    assert (await _run(tool, {"sort": "new"}, reply=lambda m, u, kw: replies[u]))[0] == {"items": [], "count": 0}


async def test_failed_step_names_the_step():
    api = make_api(FLOW_PLAN, None)
    api.set_transport(
        httpx.MockTransport(lambda r: httpx.Response(404 if "/items/" in r.url.path else 200, json={"id": 1}))
    )
    tool = _tool(steps=[Step(endpoint="GET /items/{id}", args={"path.id": "$prev.id"})])
    with pytest.raises(ApiError, match=r"demo_x step 1 \(GET /items/\{id\}\): Not found") as e:
        await run_tool(api, tool_spec(tool, PLAN, None), {})
    assert e.value.status == 404


async def test_arg_without_target_feeds_only_steps(tmp_path, monkeypatch):
    detail = ToolArg(name="detail_id", type="integer", description="Item id, e.g. 7.", required=True)  # steps only
    tool = _tool(args=[detail], steps=[Step(endpoint="GET /items/{id}", args={"path.id": "$arg.detail_id"})])
    result, calls = await _run(tool, {"detail_id": 7}, reply=lambda m, u, kw: {"id": 7})
    assert calls == [("GET", "/x", {}), ("GET", "/items/{id}", {"path": {"id": 7}})]
    plan = PLAN.model_copy(update={"tools": [tool.model_copy(update={"example": {"detail_id": 7}})]})
    monkeypatch.setenv("DEMO_MCP_TOKEN", "t")
    generate(plan, tmp_path / "demo-mcp")  # the generated tool sends the same requests
    gen = _import_server(tmp_path / "demo-mcp" / "src", plan.package)
    seen = []
    gen.api.set_transport(httpx.MockTransport(lambda r: seen.append(r) or httpx.Response(200, json={"id": 7})))
    async with Client(gen.mcp) as c:
        assert not (await c.call_tool("demo_x", {"detail_id": 7})).is_error
    assert [str(r.url).removeprefix(PLAN.base_url) for r in seen] == ["/x", "/items/7"]
