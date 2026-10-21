import json

import pytest
from fake_cdp import FakeCdp

from mcpsmith import browser

COOKIES = {
    "cookies": [
        {"name": "sid", "value": "a", "domain": ".example.test"},
        {"name": "csrf", "value": "b", "domain": "za.example.test"},
        {"name": "other", "value": "c", "domain": "notexample.test"},
        {"name": "x", "value": "d", "domain": "evil.test"},
    ]
}


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture(autouse=True)
def hub_home(tmp_path, monkeypatch):
    monkeypatch.setenv("MCPSMITH_HOME", str(tmp_path))
    monkeypatch.delenv("HTTPS_PROXY", raising=False)
    monkeypatch.delenv("https_proxy", raising=False)
    return tmp_path


def test_status_none_then_json(hub_home):
    assert browser.status() is None
    with FakeCdp() as fake:
        (hub_home / "browser.json").write_text(json.dumps({"port": fake.port, "proxy": None}))
        assert browser.status()["port"] == fake.port
    assert browser.status() is None


@pytest.mark.anyio
async def test_cookies_for_matches_domain():
    with FakeCdp({"Storage.getCookies": COOKIES}) as fake:
        assert await browser.cookies_for("za.example.test", fake.port) == {"sid": "a", "csrf": "b"}


@pytest.mark.anyio
async def test_cdp_call_session_and_events():
    with FakeCdp({"Target.attachToTarget": {"sessionId": "s1"}}) as fake:
        async with await browser.Cdp.connect(fake.port) as cdp:
            assert await cdp.call("Target.attachToTarget", {"targetId": "t"}) == {"sessionId": "s1"}
            await cdp.call("Network.enable", session="s1")
            fake.emit("Network.requestWillBeSent", {"requestId": "1"}, session="s1")
            ev = await anext(cdp.events())
        assert ev["method"] == "Network.requestWillBeSent" and ev["sessionId"] == "s1"
        assert fake.calls[1] == ("Network.enable", {}, "s1")


@pytest.fixture
def popen(monkeypatch, tmp_path):
    seen = {}
    monkeypatch.setattr(browser, "find_chrome", lambda: tmp_path / "chrome.exe")
    monkeypatch.setattr(browser.subprocess, "Popen", lambda cmd, **kw: seen.update(cmd=cmd, kw=kw))
    return seen


def test_launch_uses_env_proxy(popen, hub_home, monkeypatch):
    monkeypatch.setenv("HTTPS_PROXY", "http://p:1")
    info = browser.launch(port=1)
    assert "--proxy-server=http://p:1" in popen["cmd"]
    assert "--remote-debugging-port=1" in popen["cmd"]
    assert f"--user-data-dir={hub_home / 'chrome-profile'}" in popen["cmd"]
    assert json.loads((hub_home / "browser.json").read_text()) == info == {"port": 1, "proxy": "http://p:1"}


def test_launch_no_proxy(popen, monkeypatch):
    monkeypatch.setenv("HTTPS_PROXY", "http://p:1")
    info = browser.launch(port=1, no_proxy=True)
    assert not any(a.startswith("--proxy-server") for a in popen["cmd"])
    assert info["proxy"] is None


def test_launch_reuses_running(popen):
    with FakeCdp() as fake:
        assert browser.launch(port=fake.port)["port"] == fake.port
    assert popen == {}


def test_find_chrome_env_and_missing(monkeypatch, tmp_path):
    exe = tmp_path / "c.exe"
    exe.write_text("")
    monkeypatch.setenv("MCPSMITH_CHROME", str(exe))
    assert browser.find_chrome() == exe
    monkeypatch.setenv("MCPSMITH_CHROME", str(tmp_path / "nope"))
    monkeypatch.setattr(browser, "_CANDIDATES", [str(tmp_path / "nada")])
    with pytest.raises(RuntimeError, match="nada"):
        browser.find_chrome()


def test_launch_drops_proxy_credentials(popen, hub_home, monkeypatch, capsys):
    monkeypatch.setenv("HTTPS_PROXY", "http://u:p@h:1")
    info = browser.launch(port=1)
    assert info["proxy"] == "http://h:1"
    assert "--proxy-server=http://h:1" in popen["cmd"]
    out = capsys.readouterr().out
    assert "credentials dropped" in out
    assert "u:p" not in out + (hub_home / "browser.json").read_text() + " ".join(popen["cmd"])


def test_launch_schemeless_proxy(popen, monkeypatch):
    monkeypatch.setenv("HTTPS_PROXY", "h:1")
    assert browser.launch(port=1)["proxy"] == "http://h:1"
