import json
import os
import socket
import subprocess
import sys
import time

import httpx
import pytest
from mcp.types import LATEST_PROTOCOL_VERSION

from toolsmelt.runtime import run


class FakeServer:
    name = "demo-mcp"

    def __init__(self):
        self.calls = []

    def run(self, transport="stdio", **kwargs):
        self.calls.append((transport, kwargs))


@pytest.fixture(autouse=True)
def no_env(monkeypatch):
    for k in ("MCP_TRANSPORT", "MCP_HOST", "MCP_PORT", "MCP_AUTH_TOKEN"):
        monkeypatch.delenv(k, raising=False)


def test_stdio_is_the_default():
    s = FakeServer()
    run(s, ["--some-other-flag"])
    assert s.calls == [("stdio", {})]


def test_http_from_argv_and_env(monkeypatch, capsys):
    s = FakeServer()
    run(s, ["--transport", "http", "--port", "9123"])
    assert s.calls == [("streamable-http", {"host": "127.0.0.1", "port": 9123})]
    assert capsys.readouterr().err == ""

    monkeypatch.setenv("MCP_TRANSPORT", "http")
    monkeypatch.setenv("MCP_PORT", "9200")
    run(s, [])
    assert s.calls[-1] == ("streamable-http", {"host": "127.0.0.1", "port": 9200})
    run(s, ["--port", "9300"])  # argv wins over env
    assert s.calls[-1][1]["port"] == 9300


def test_public_host_warns_no_auth(monkeypatch, capsys):
    s = FakeServer()
    monkeypatch.setenv("MCP_HOST", "0.0.0.0")
    run(s, ["--transport", "http"])
    assert s.calls == [("streamable-http", {"host": "0.0.0.0", "port": 8000})]
    err = capsys.readouterr().err
    assert "no authentication" in err and err.count("\n") == 1


def test_bad_transport_or_port_is_an_error(monkeypatch):
    with pytest.raises(SystemExit):
        run(FakeServer(), ["--transport", "sse"])
    monkeypatch.setenv("MCP_PORT", "eighty")
    with pytest.raises(SystemExit):
        run(FakeServer(), ["--transport", "http"])


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _rpc(resp: httpx.Response) -> dict:
    """A JSON-RPC reply, sent as JSON or as one server-sent event."""
    if resp.headers.get("content-type", "").startswith("text/event-stream"):
        data = [ln[5:].strip() for ln in resp.text.splitlines() if ln.startswith("data:")]
        return json.loads(data[-1])
    return resp.json()


INIT = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": LATEST_PROTOCOL_VERSION,
        "capabilities": {},
        "clientInfo": {"name": "t", "version": "0"},
    },
}


def _hub(tmp_path, **env):
    """Start `toolsmelt mcp --transport http` on a free port; returns (process, url)."""
    port = _free_port()
    proc = subprocess.Popen(
        [sys.executable, "-m", "toolsmelt.cli", "mcp", "--transport", "http", "--port", str(port)],
        env={**os.environ, "TOOLSMELT_HOME": str(tmp_path), **env},
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    return proc, f"http://127.0.0.1:{port}/mcp"


def _first(http, proc, url, headers) -> httpx.Response:
    """POST initialize, retrying while the server starts."""
    deadline = time.monotonic() + 60
    while True:
        try:
            return http.post(url, json=INIT, headers=headers)
        except httpx.TransportError:
            if proc.poll() is not None or time.monotonic() > deadline:
                raise AssertionError(f"server did not start: {proc.stderr.read().decode(errors='replace')}") from None
            time.sleep(0.3)


def _stop(proc) -> None:
    proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()


def test_hub_needs_the_bearer_token_when_set(tmp_path):
    proc, url = _hub(tmp_path, MCP_AUTH_TOKEN="s3cret-token")
    headers = {"accept": "application/json, text/event-stream", "content-type": "application/json"}
    try:
        with httpx.Client(trust_env=False, timeout=10) as http:
            assert _first(http, proc, url, headers).status_code == 401
            bad = http.post(url, json=INIT, headers={**headers, "authorization": "Bearer nope"})
            assert bad.status_code == 401
            ok = http.post(url, json=INIT, headers={**headers, "authorization": "Bearer s3cret-token"})
            assert ok.status_code == 200, ok.text
            assert _rpc(ok)["result"]["serverInfo"]["name"] == "toolsmelt"
    finally:
        _stop(proc)


def test_hub_serves_streamable_http(tmp_path):
    proc, url = _hub(tmp_path)
    headers = {"accept": "application/json, text/event-stream", "content-type": "application/json"}
    try:
        with httpx.Client(trust_env=False, timeout=10) as http:
            r = _first(http, proc, url, headers)
            assert r.status_code == 200, r.text
            assert _rpc(r)["result"]["serverInfo"]["name"] == "toolsmelt"
            if sid := r.headers.get("mcp-session-id"):
                headers["mcp-session-id"] = sid
            http.post(url, json={"jsonrpc": "2.0", "method": "notifications/initialized"}, headers=headers)
            r = http.post(url, json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"}, headers=headers)
            names = {t["name"] for t in _rpc(r)["result"]["tools"]}
            assert "smith_guide" in names
    finally:
        _stop(proc)
