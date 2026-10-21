import json

import httpx
import pytest

from mcpsmith import probe
from mcpsmith.design import digest, draft, validate
from mcpsmith.model import Endpoint, Param, Source


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
