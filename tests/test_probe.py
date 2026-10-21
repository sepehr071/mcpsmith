import json

import httpx
import pytest

from mcpsmith import probe
from mcpsmith.design import digest, draft, validate
from mcpsmith.model import AuthScheme, Endpoint, Param, Source


@pytest.fixture(autouse=True)
def _fast(monkeypatch):
    monkeypatch.setattr(probe, "DELAY", 0)


def _src(*eps):
    return Source(kind="har", title="Demo", base_url="https://api.demo.test", endpoints=list(eps))


def _ep(method, path, **kw):
    return Endpoint(id=f"{method} {path}", method=method, path=path, verified=False, **kw)


def _use(monkeypatch, handler):
    monkeypatch.setattr(probe, "TRANSPORT", httpx.MockTransport(handler))


def test_probe_verifies_get_and_scrubs(monkeypatch):
    _use(
        monkeypatch, lambda r: httpx.Response(200, json={"items": [{"id": 1, "email": "a@b.co", "token": "secret123"}]})
    )
    src = _src(_ep("GET", "/items"))
    results = probe.run(src)
    ep = src.endpoint("GET /items")
    assert results[0].ok and ep.verified
    dumped = json.dumps(ep.response_example)
    assert "secret123" not in dumped and "a@b.co" not in dumped


def test_probe_skips_unfillable_path_and_writes(monkeypatch):
    sent = []
    _use(monkeypatch, lambda r: sent.append(r) or httpx.Response(200, json={}))
    src = _src(_ep("GET", "/items/{id}"), _ep("POST", "/items"))
    r = {x.endpoint: x for x in probe.run(src)}
    assert "placeholder" in r["GET /items/{id}"].skipped_reason
    assert "write" in r["POST /items"].skipped_reason and src.endpoint("POST /items").verified is False
    assert sent == []


def test_probe_fills_path_from_example_and_sends_writes_on_opt_in(monkeypatch):
    sent = []
    _use(monkeypatch, lambda r: sent.append(r) or httpx.Response(200, json={"ok": 1}))
    src = _src(_ep("GET", "/items/{id}", params=[Param(name="id", location="path", example=7)]), _ep("POST", "/items"))
    probe.run(src, writes=True)
    assert [(r.method, r.url.path) for r in sent] == [("GET", "/items/7"), ("POST", "/items")]


def test_probe_stops_host_on_429(monkeypatch):
    sent = []
    _use(monkeypatch, lambda r: sent.append(r) or httpx.Response(429, text="slow down"))
    src = _src(_ep("GET", "/a"), _ep("GET", "/b"))
    results = probe.run(src)
    assert len(sent) == 1 and not results[0].ok and results[0].status == 429
    assert "rate" in results[1].skipped_reason and not src.endpoint("GET /b").verified


def test_unverified_endpoint_still_drafts_and_validates():
    src = _src(_ep("GET", "/items", summary="List items"))
    assert "UNVERIFIED" in digest(src)
    plan = draft(src)
    assert plan.tools and plan.tools[0].output.fields == {}
    assert any("unverified" in i.message for i in validate(plan, src))


def test_limit_counts_real_requests_not_skips(monkeypatch):
    sent = []
    _use(monkeypatch, lambda r: sent.append(r) or httpx.Response(200, json={"ok": 1}))
    src = _src(_ep("POST", "/w"), _ep("GET", "/a"), _ep("GET", "/b"), _ep("GET", "/c"))
    results = probe.run(src, limit=2)
    assert [r.ok for r in results] == [False, True, True, False]
    assert len(sent) == 2 and "limit" in results[3].skipped_reason


def test_probe_stops_host_when_session_expired(monkeypatch):
    sent = []
    _use(monkeypatch, lambda r: sent.append(r) or httpx.Response(401, text="no"))
    src = _src(_ep("GET", "/a"), _ep("GET", "/b"))
    src.auth = [AuthScheme(name="s", type="browser", session_cookie="sid")]

    async def no_cookies(host):
        return {}

    monkeypatch.setattr(probe, "COOKIES", no_cookies)
    results = probe.run(src)
    assert sent == [] and "expired" in results[1].skipped_reason


def test_probe_without_base_url_refuses_and_survives(monkeypatch):
    _use(monkeypatch, lambda r: httpx.Response(200, json={"ok": 1}))
    src = Source(kind="app", title="A", base_url="", endpoints=[_ep("GET", "/items")])
    r = probe.run(src)[0]
    assert "base URL" in r.skipped_reason and "--base-url" in r.skipped_reason and not src.endpoints[0].verified


def test_probe_unexpected_exception_fails_only_that_endpoint(monkeypatch):
    def handler(r):
        if r.url.path == "/a":
            raise RuntimeError("boom")
        return httpx.Response(200, json={"ok": 1})

    _use(monkeypatch, handler)
    src = _src(_ep("GET", "/a"), _ep("GET", "/b"))
    results = probe.run(src)
    assert "boom" in results[0].error and not results[0].ok
    assert results[1].ok and src.endpoint("GET /b").verified


def test_probe_skips_methodless_endpoint_even_with_writes(monkeypatch):
    sent = []
    _use(monkeypatch, lambda r: sent.append(r) or httpx.Response(200, json={}))
    src = _src(Endpoint(id="x", method="", path="/x", verified=False))
    assert "method" in probe.run(src, writes=True)[0].skipped_reason and sent == []


def test_methodless_endpoint_is_a_validation_error_and_cannot_be_interpreted():
    from mcpsmith.runtime.interpret import endpoint_of

    src = _src(Endpoint(id="x", method="", path="/x", verified=False, summary="x"))
    plan = draft(src)
    assert any(i.level == "error" and "no HTTP method" in i.message for i in validate(plan, src))
    with pytest.raises(ValueError, match="no HTTP method"):
        endpoint_of(plan.tools[0], plan, src)
