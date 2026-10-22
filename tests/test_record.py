import asyncio
import json

import pytest
from fake_cdp import FakeCdp

from toolsmelt import browser, importers
from toolsmelt.record import Recorder


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture(autouse=True)
def hub_home(tmp_path, monkeypatch):
    monkeypatch.setenv("TOOLSMELT_HOME", str(tmp_path))
    return tmp_path


def sent(fake, rid, url, session="s1", headers=None):
    req = {"url": url, "method": "GET", "headers": headers or {"Accept": "application/json"}}
    fake.emit(
        "Network.requestWillBeSent", {"requestId": rid, "request": req, "type": "XHR", "wallTime": 1.7e9}, session
    )


def done(fake, rid, session="s1", size=10, mime="application/json"):
    resp = {"status": 200, "statusText": "OK", "mimeType": mime, "headers": {"Set-Cookie": "sid=secret"}}
    fake.emit("Network.responseReceived", {"requestId": rid, "response": resp, "type": "XHR"}, session)
    fake.emit("Network.loadingFinished", {"requestId": rid, "encodedDataLength": size}, session)


async def until(cond):
    for _ in range(200):
        if cond():
            return
        await asyncio.sleep(0.02)
    raise AssertionError("timed out")


def body_for(params, session):
    return {"body": json.dumps({"id": params["requestId"], "name": "x"}), "base64Encoded": False}


@pytest.mark.anyio
async def test_records_matching_hosts_late_targets_and_scrubs(hub_home):
    with FakeCdp({"Network.getResponseBody": body_for}) as fake:
        (hub_home / "browser.json").write_text(json.dumps({"port": fake.port, "proxy": None}))
        rec = Recorder()
        await rec.start("shop", ["shop.test"])
        assert (
            "Target.setAutoAttach",
            {"autoAttach": True, "waitForDebuggerOnStart": False, "flatten": True},
            None,
        ) in fake.calls
        fake.emit("Target.attachedToTarget", {"sessionId": "s1", "targetInfo": {"type": "page", "targetId": "t"}})
        await until(lambda: ("Network.enable", {}, "s1") in fake.calls)
        hdr = {"Cookie": "sid=secret", "__RequestVerificationToken": "tok", "Accept": "application/json"}
        sent(fake, "1", "https://api.shop.test/items/1", headers=hdr)
        done(fake, "1")
        sent(fake, "2", "https://api.shop.test/items/2")
        done(fake, "2")
        sent(fake, "3", "https://other.test/x")
        done(fake, "3")
        await until(lambda: sum("body" in e for e in rec.entries.values()) == 2)
        har_path = await rec.stop()
    assert har_path == hub_home / "work" / "shop" / "capture.har"
    text = har_path.read_text()
    assert "secret" not in text and '"tok"' not in text
    entries = json.loads(text)["log"]["entries"]
    assert [e["request"]["url"] for e in entries] == ["https://api.shop.test/items/1", "https://api.shop.test/items/2"]
    e = entries[0]
    assert e["_resourceType"] == "xhr" and e["response"]["status"] == 200
    assert {h["name"]: h["value"] for h in e["request"]["headers"]}["Cookie"] == "<dropped>"
    assert {h["name"]: h["value"] for h in e["response"]["headers"]}["Set-Cookie"] == "<dropped>"
    assert json.loads(e["response"]["content"]["text"])["id"] == "1"
    src = importers.load(str(har_path))
    assert len(src.endpoints) >= 1 and src.base_url == "https://api.shop.test"


@pytest.mark.anyio
async def test_big_response_has_no_body_and_second_start_fails(hub_home):
    with FakeCdp({"Network.getResponseBody": body_for}) as fake:
        (hub_home / "browser.json").write_text(json.dumps({"port": fake.port, "proxy": None}))
        rec = Recorder()
        await rec.start("big", ["shop.test"])
        with pytest.raises(RuntimeError, match="already recording"):
            await rec.start("other", [])
        fake.emit("Target.attachedToTarget", {"sessionId": "s1", "targetInfo": {"type": "page", "targetId": "t"}})
        await until(lambda: ("Network.enable", {}, "s1") in fake.calls)
        sent(fake, "1", "https://api.shop.test/big")
        done(fake, "1", size=5_000_000)
        await until(lambda: rec.count == 1)
        await rec.stop()
        assert not any(c[0] == "Network.getResponseBody" for c in fake.calls)
    entry = json.loads((hub_home / "work" / "big" / "capture.har").read_text())["log"]["entries"][0]
    assert "text" not in entry["response"]["content"]


@pytest.mark.anyio
async def test_start_without_chrome():
    with pytest.raises(RuntimeError, match="toolsmelt browser"):
        await Recorder().start("x", ["shop.test"])
    assert browser.status() is None


@pytest.mark.anyio
@pytest.mark.parametrize("hosts", [[], [""], [" . "]])
async def test_no_hosts_is_refused(hosts):
    # without a host filter every site open in the shared Chrome would be recorded
    with pytest.raises(ValueError, match="at least one site"):
        await Recorder().start("x", hosts)


@pytest.mark.anyio
async def test_existing_tab_recorded_once_and_login_scrubbed(hub_home):
    responses = {
        "Target.getTargets": {"targetInfos": [{"type": "page", "targetId": "t1", "attached": True}]},
        "Target.attachToTarget": {"sessionId": "s9"},
        "Network.getResponseBody": {"body": json.dumps({"access_token": "TOKEN123", "ok": 1}), "base64Encoded": False},
    }
    with FakeCdp(responses) as fake:
        (hub_home / "browser.json").write_text(json.dumps({"port": fake.port, "proxy": None}))
        rec = Recorder()
        await rec.start("login", ["shop.test"])
        assert ("Network.enable", {}, "s9") in fake.calls
        # same target announced by auto-attach too: no second session
        fake.emit("Target.attachedToTarget", {"sessionId": "s2", "targetInfo": {"type": "page", "targetId": "t1"}})
        await asyncio.sleep(0.2)
        assert sum(c[0] == "Network.enable" for c in fake.calls) == 1
        req = {
            "url": "https://api.shop.test/login",
            "method": "POST",
            "headers": {"Content-Type": "application/x-www-form-urlencoded"},
            "postData": "user=bob&password=hunter2xyz",
        }
        fake.emit("Network.requestWillBeSent", {"requestId": "1", "request": req, "type": "XHR"}, "s9")
        done(fake, "1", session="s9")
        await until(lambda: sum("body" in e for e in rec.entries.values()) == 1)
        har_path = await rec.stop()
    text = har_path.read_text()
    assert "hunter2xyz" not in text and "TOKEN123" not in text
    assert len(json.loads(text)["log"]["entries"]) == 1


@pytest.mark.anyio
@pytest.mark.parametrize("name", [".", "..", "a/b", ""])
async def test_bad_name_rejected(name, hub_home):
    with FakeCdp() as fake:
        (hub_home / "browser.json").write_text(json.dumps({"port": fake.port, "proxy": None}))
        with pytest.raises(ValueError):
            await Recorder().start(name, [])


@pytest.mark.anyio
async def test_custom_secret_headers_and_query_values_are_dropped(hub_home):
    with FakeCdp({"Network.getResponseBody": body_for}) as fake:
        (hub_home / "browser.json").write_text(json.dumps({"port": fake.port, "proxy": None}))
        rec = Recorder()
        await rec.start("shop", ["shop.test"])
        fake.emit("Target.attachedToTarget", {"sessionId": "s1", "targetInfo": {"type": "page", "targetId": "t"}})
        await until(lambda: ("Network.enable", {}, "s1") in fake.calls)
        sent(
            fake,
            "1",
            "https://api.shop.test/s?api_key=AKEY123&access_token=ATOK456&q=shoes",
            headers={"X-Api-Key": "HKEY789", "Accept": "application/json", "Authorization": "Bearer BTOK321"},
        )
        done(fake, "1")
        await until(lambda: sum("body" in e for e in rec.entries.values()) == 1)
        har_path = await rec.stop()
    text = har_path.read_text()
    assert not any(s in text for s in ("AKEY123", "ATOK456", "HKEY789", "BTOK321"))
    assert "q=shoes" in text
    # the scheme word survives, so the importer still sees bearer auth
    assert "Bearer <dropped>" in text and "bearer" in [a.type for a in importers.load(str(har_path)).auth]


@pytest.mark.anyio
async def test_only_api_call_bodies_are_fetched(hub_home):
    with FakeCdp({"Network.getResponseBody": body_for}) as fake:
        (hub_home / "browser.json").write_text(json.dumps({"port": fake.port, "proxy": None}))
        rec = Recorder()
        await rec.start("shop", ["shop.test"])
        fake.emit("Target.attachedToTarget", {"sessionId": "s1", "targetInfo": {"type": "page", "targetId": "t"}})
        await until(lambda: ("Network.enable", {}, "s1") in fake.calls)
        page = {"url": "https://shop.test/account", "method": "GET", "headers": {}}
        fake.emit("Network.requestWillBeSent", {"requestId": "d", "request": page, "type": "Document"}, "s1")
        resp = {"status": 200, "statusText": "OK", "mimeType": "text/html", "headers": {}}
        fake.emit("Network.responseReceived", {"requestId": "d", "response": resp, "type": "Document"}, "s1")
        fake.emit("Network.loadingFinished", {"requestId": "d", "encodedDataLength": 10}, "s1")
        sent(fake, "x", "https://api.shop.test/items")
        done(fake, "x")
        await until(lambda: "body" in rec.entries.get(("s1", "x"), {}))
        await rec.stop()
    # the HTML page (names, addresses, codes the scrubber cannot see) is never fetched
    assert [c[1]["requestId"] for c in fake.calls if c[0] == "Network.getResponseBody"] == ["x"]
