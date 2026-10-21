import json
from typing import Annotated

import httpx
import pytest
from mcp import Client
from pydantic import Field

from mcpsmith.runtime import Api, ApiError, Registry, build_server, http, shape

pytestmark = pytest.mark.anyio

DATA = {
    "data": {
        "items": [
            {"id": 1, "name": "a", "tags": [{"name": "x"}, {"name": "y"}], "images": [{"url": "u1"}], "note": None},
            {"id": 2, "name": "", "tags": [], "images": [], "note": "long text " * 10},
            {"id": 3, "name": "c"},
        ]
    }
}


def test_shape_paths():
    out = shape(
        DATA,
        root="data.items",
        fields={"id": "id", "tag": "tags[].name", "img": "images.0.url", "last": "tags.-1.name"},
    )
    assert out["count"] == 3
    assert out["items"] == [{"id": 1, "tag": ["x", "y"], "img": "u1", "last": "y"}, {"id": 2}, {"id": 3}]


def test_shape_cap_clip_and_dict_root():
    out = shape(DATA, root="data.items", max_items=2, clip=12)
    assert out["count"] == 3 and len(out["items"]) == 2
    assert out["items"][1] == {"id": 2, "note": "long text lo...[cut]"}  # empties dropped
    assert shape(DATA, root="data.items.0", fields={"n": "name", "missing": "no.such"}) == {"n": "a"}
    assert shape(DATA, root="data.nothing") is None
    assert shape([1, None, 2]) == {"items": [1, 2], "count": 3}


def make_api(handler, **kwargs) -> Api:
    api = Api(base_url="https://api.example.com/v1/", env_prefix="EX_MCP", **kwargs)
    api.RETRY_DELAY = 0
    api.set_transport(httpx.MockTransport(handler))
    return api


async def test_call_drops_none_and_nests_body():
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, json={"ok": True})

    api = make_api(handler, headers={"Accept": "application/json"})
    await api.call(
        "POST",
        "/pets/{id}",
        path={"id": "a b"},
        query={"q": "x", "skip": None},
        body={"a.b": 1, "a.c": "two", "d": 3, "e": None},
        headers={"X-One": "1", "X-None": None},
        cookies={"sid": "s"},
    )
    r = seen[0]
    assert str(r.url) == "https://api.example.com/v1/pets/a%20b?q=x"
    assert json.loads(r.content) == {"a": {"b": 1, "c": "two"}, "d": 3}
    assert r.headers["x-one"] == "1" and "x-none" not in r.headers and r.headers["cookie"] == "sid=s"
    assert r.headers["accept"] == "application/json"


async def test_get_cached_and_never_sends_body():
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, json={"n": len(seen)})

    api = make_api(handler)
    assert await api.call("GET", "/x", query={"a": 1}, body={"b": 2}) == {"n": 1}
    assert await api.call("GET", "/x", query={"a": 1}) == {"n": 1}
    assert len(seen) == 1 and seen[0].content == b""
    assert await api.call("GET", "/x", query={"a": 2}) == {"n": 2}


async def test_retry_on_503_then_success():
    codes = iter([503, 200])

    def handler(request):
        code = next(codes)
        return httpx.Response(code, json={"ok": code == 200}, headers={"Retry-After": "0"})

    assert await make_api(handler).call("GET", "/x") == {"ok": True}


async def test_post_not_retried_on_500():
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(500)

    with pytest.raises(ApiError, match="server error"):
        await make_api(handler).call("POST", "/x", body={"a": 1})
    assert len(calls) == 1


@pytest.mark.parametrize(
    ("code", "text"),
    [
        (401, "requires authentication"),
        (403, "EX_MCP_PROXY"),
        (404, "Not found on api.example.com"),
        (429, "rate limiting"),
        (400, "rejected the request (HTTP 400): bad id"),
    ],
)
async def test_status_messages(code, text):
    api = make_api(lambda r: httpx.Response(code, json={"message": "bad id"}))
    with pytest.raises(ApiError, match=text.replace("(", r"\(").replace(")", r"\)")) as e:
        await api.call("GET", "/x")
    assert e.value.status == code


async def test_auth_env(monkeypatch):
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(401)

    api = make_api(handler, auth={"type": "query", "param": "key", "env": "EX_MCP_KEY"})
    monkeypatch.delenv("EX_MCP_KEY", raising=False)
    with pytest.raises(ApiError, match="Set EX_MCP_KEY"):
        await api.call("GET", "/x")
    assert "key=" not in str(seen[-1].url)  # unset credential: the call goes out without it
    monkeypatch.setenv("EX_MCP_KEY", "secret")
    with pytest.raises(ApiError, match="Check that EX_MCP_KEY"):
        await api.call("GET", "/x")
    assert seen[-1].url.params["key"] == "secret"

    bearer = make_api(handler, auth={"type": "bearer", "env": "EX_MCP_KEY"})
    with pytest.raises(ApiError):
        await bearer.call("GET", "/x")
    assert seen[-1].headers["authorization"] == "Bearer secret"


@pytest.mark.parametrize(
    "auth",
    [
        {"type": "bearer", "env": "EX_MCP_KEY"},
        {"type": "basic", "env": "EX_MCP_KEY"},
        {"type": "header", "param": "X-Key", "env": "EX_MCP_KEY"},
        {"type": "query", "param": "key", "env": "EX_MCP_KEY"},
        {"type": "cookie", "param": "key", "env": "EX_MCP_KEY"},
    ],
)
async def test_auth_only_to_base_host(monkeypatch, auth):
    monkeypatch.setenv("EX_MCP_KEY", "u:secret")
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, json={})

    api = make_api(handler, hosts=["cdn.example.com"], auth=auth, headers={"Accept": "application/json"})
    await api.call("GET", "/x")
    await api.call("GET", "https://cdn.example.com/y")
    for r, has in zip(seen, (True, False), strict=True):
        assert ("secret" in str(r.url) + str(r.headers) or "authorization" in r.headers) is has
        assert r.headers["accept"] == "application/json"  # static headers still go to every allowed host


async def test_path_values_cannot_escape_base_path():
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, json={})

    api = make_api(handler)
    for bad in ("..", "."):
        await api.call("GET", "/users/{id}/posts", path={"id": bad})
    await api.call("GET", "/users/{id}/{flag}", path={"id": "a/../b", "flag": True})
    await api.call("GET", "/users/{id}/{flag}", path={"id": 1, "flag": False})
    assert [r.url.raw_path for r in seen] == [
        b"/v1/users/%2E%2E/posts",
        b"/v1/users/%2E/posts",
        b"/v1/users/a%2F..%2Fb/true",
        b"/v1/users/1/false",
    ]


@pytest.mark.parametrize("bad", ["a;b", "a\rb", "a\nb"])
async def test_cookie_and_header_injection_refused(bad):
    api = make_api(lambda r: httpx.Response(200, json={}))
    with pytest.raises(ApiError, match="Refusing"):
        await api.call("GET", "/x", cookies={"sid": bad})
    if bad != "a;b":
        with pytest.raises(ApiError, match="Refusing"):
            await api.call("GET", "/x", headers={"X-One": bad})


async def test_cookie_secret_injection_refused(monkeypatch):
    monkeypatch.setenv("EX_MCP_KEY", "a; admin=1")
    api = make_api(lambda r: httpx.Response(200, json={}), auth={"type": "cookie", "param": "k", "env": "EX_MCP_KEY"})
    with pytest.raises(ApiError, match="Refusing"):
        await api.call("GET", "/x")


def test_concurrency_must_be_positive():
    with pytest.raises(ValueError, match="concurrency"):
        Api(base_url="https://api.example.com", env_prefix="EX_MCP", concurrency=0)


async def test_host_allowlist():
    api = make_api(lambda r: httpx.Response(200, json={"host": r.url.host}), hosts=["cdn.example.com"])
    with pytest.raises(ApiError, match="unexpected host evil.com"):
        await api.call("GET", "https://evil.com/x")
    assert await api.call("GET", "https://cdn.example.com/y") == {"host": "cdn.example.com"}


async def test_non_json_and_empty():
    api = make_api(lambda r: httpx.Response(200, text="<html>hi</html>" if r.url.path.endswith("/page") else ""))
    assert await api.call("GET", "/page") == {"text": "<html>hi</html>"}
    assert await api.call("DELETE", "/thing") == {"status": 200}


def test_impersonate_falls_back_without_stealth_extra(caplog):
    http._curl.cache_clear()
    api = Api(base_url="https://api.example.com", env_prefix="EX_MCP", impersonate=True)
    transport = api._make_transport(None)
    if http._curl() is None:  # curl_cffi not installed
        assert isinstance(transport, httpx.AsyncHTTPTransport) and "mcpsmith[stealth]" in caplog.text


def test_base_url_env(monkeypatch):
    monkeypatch.setenv("EX_MCP_BASE_URL", "http://127.0.0.1:9/api/")
    api = Api(base_url="https://api.example.com", env_prefix="EX_MCP")
    assert api.base_url == "http://127.0.0.1:9/api" and "127.0.0.1" in api.hosts


tool = Registry()


@tool("Echo things", read_only=True)
async def ex_echo(text: Annotated[str, Field(description="Text to echo.")], n: int | None = None):
    """Echo the text back.

    Second line.
    """
    return {"text": text, "n": n, "empty": [], "nested": {"a": None, "b": "x"}}


@tool("Delete thing", read_only=False)
async def ex_delete(id: int):
    """Delete one thing."""
    return []


async def test_build_server_compact_output_and_annotations():
    mcp = build_server("ex-mcp", title="Ex", instructions="Use it.", version="1.0", registry=tool)
    async with Client(mcp) as c:
        tools = {t.name: t for t in (await c.list_tools()).tools}
        assert tools["ex_echo"].title == "Echo things"
        assert tools["ex_echo"].description == "Echo the text back.\n\nSecond line."
        assert "title" not in json.dumps(tools["ex_echo"].input_schema)
        assert tools["ex_echo"].annotations.read_only_hint is True
        assert tools["ex_delete"].annotations.destructive_hint is True
        assert tools["ex_delete"].annotations.read_only_hint is False
        result = await c.call_tool("ex_echo", {"text": 'سلام "hi"'})
        assert not result.is_error and len(result.content) == 1
        assert result.content[0].text == '{"text":"سلام \\"hi\\"","empty":[],"nested":{"b":"x"}}'


async def _jar(host):
    return {"sid": "TOPSECRET"}


async def test_browser_auth_cookies_base_host_only():
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, json={})

    api = make_api(handler, hosts=["cdn.example.com"], auth={"type": "browser"}, cookie_provider=_jar)
    await api.call("GET", "/x")
    await api.call("GET", "https://cdn.example.com/y")
    assert seen[0].headers["cookie"] == "sid=TOPSECRET"
    assert "cookie" not in seen[1].headers
    assert not any("TOPSECRET" in k for k in api._cache for k in map(str, k))


@pytest.mark.parametrize("status,loc", [(302, "/Account/Login?ReturnUrl=%2F"), (401, "")])
async def test_browser_auth_session_expired(status, loc):
    api = make_api(
        lambda r: httpx.Response(status, headers={"location": loc} if loc else {}),
        auth={"type": "browser"},
        cookie_provider=_jar,
        login_path="/Account/Login",
    )
    with pytest.raises(ApiError, match="says the session has expired") as e:
        await api.call("GET", "/x")
    assert "TOPSECRET" not in str(e.value)


async def test_browser_auth_chrome_not_running():
    async def down(host):
        raise ConnectionError("no chrome")

    api = make_api(lambda r: httpx.Response(200, json={}), auth={"type": "browser"}, cookie_provider=down)
    with pytest.raises(ApiError, match="The mcpsmith Chrome is not running"):
        await api.call("GET", "/x")


def test_proxy_overrides_env(monkeypatch):
    monkeypatch.setenv("EX_MCP_PROXY", "http://env:1")
    assert make_api(lambda r: None, proxy="http://given:2")._proxies() == ["http://given:2"]


async def test_make_api_browser_needs_running_chrome(monkeypatch):
    from mcpsmith.model import AuthScheme, Plan
    from mcpsmith.runtime.interpret import make_api as build

    monkeypatch.setattr("mcpsmith.browser.status", lambda: None)
    plan = Plan(
        name="x-mcp",
        title="X",
        description="d",
        instructions="i",
        prefix="x",
        base_url="https://api.example.com",
        env_prefix="EX_MCP",
        auth=AuthScheme(name="b", type="browser", login_path="/Login"),
    )
    api = build(plan, None)
    api.set_transport(httpx.MockTransport(lambda r: httpx.Response(200, json={})))
    assert api.login_path == "/Login"
    with pytest.raises(ApiError, match="not running"):
        await api.call("GET", "/x")
