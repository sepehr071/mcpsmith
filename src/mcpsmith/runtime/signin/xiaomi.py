"""Sign-in provider for Xiaomi cloud (Mi Home): Xiaomi's own login page and the RC4-signed request format.

Every call to `<region>.api.io.mi.com/app/...` is a form POST whose `data` field is RC4-encrypted with a key derived
from the session's `ssecurity` and a fresh nonce; the response body is encrypted with the same key.
The session (userId, ssecurity, serviceToken) is stored as JSON in the plan's auth env / keychain entry.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import time
from typing import Any
from urllib.parse import parse_qsl, quote

import httpx

TITLE = "Xiaomi"
ACCOUNT = "the Xiaomi account their Mi Home app uses"
USER_AGENT = "Android-7.1.1-1.0.0-ONEPLUS A3010-136-ABCDEFGHIJKLMNO APP/xiaomi.smarthome APPV/62830"
HEADERS = {"User-Agent": USER_AGENT, "MIOT-ENCRYPT-ALGORITHM": "ENCRYPT-RC4", "Accept-Encoding": "identity"}
LOGIN_URL = "https://account.xiaomi.com/longPolling/loginUrl"
LOGIN_MINUTES = 10
REGIONS = ("cn", "de", "i2", "in", "ru", "sg", "tw", "us")
TEST_SESSION = {"userId": "1", "ssecurity": base64.b64encode(bytes(16)).decode(), "serviceToken": "test"}


def rc4(key: bytes, data: bytes) -> bytes:
    """RC4 with the first 1024 keystream bytes dropped, as the Mi Home app does."""
    s, j = list(range(256)), 0
    for i in range(256):
        j = (j + s[i] + key[i % len(key)]) % 256
        s[i], s[j] = s[j], s[i]
    i = j = 0
    out = bytearray()
    for n, b in enumerate(bytes(1024) + data):
        i = (i + 1) % 256
        j = (j + s[i]) % 256
        s[i], s[j] = s[j], s[i]
        if n >= 1024:
            out.append(b ^ s[(s[i] + s[j]) % 256])
    return bytes(out)


def _signature(path: str, signed_nonce: str, params: dict[str, str]) -> str:
    text = "&".join(["POST", path, *(f"{k}={v}" for k, v in params.items()), signed_nonce])
    return base64.b64encode(hashlib.sha1(text.encode()).digest()).decode()


def session_of(raw: str) -> dict[str, str]:
    """The stored session JSON, checked for the three fields every call needs."""
    s = json.loads(raw)
    if not all(s.get(k) for k in ("userId", "ssecurity", "serviceToken")):
        raise ValueError("Xiaomi session lacks userId, ssecurity or serviceToken")
    return s


def cookies(session: dict[str, str]) -> dict[str, str]:
    token = session["serviceToken"]
    return {"userId": str(session["userId"]), "serviceToken": token, "yetAnotherServiceToken": token}


def encrypt(url: str, session: dict[str, str], payload: Any) -> tuple[dict[str, str], bytes]:
    """(form fields, response key) for one call; `payload` becomes the `data` field as JSON."""
    path = httpx.URL(url).path.removeprefix("/app")  # the signature covers the path without /app
    nonce = base64.b64encode(os.urandom(8) + int(time.time() / 60).to_bytes(4, "big")).decode()
    key = hashlib.sha256(base64.b64decode(session["ssecurity"]) + base64.b64decode(nonce)).digest()
    signed_nonce = base64.b64encode(key).decode()
    params = {"data": json.dumps(payload if payload is not None else {}, separators=(",", ":"))}
    params["rc4_hash__"] = _signature(path, signed_nonce, params)
    params = {k: base64.b64encode(rc4(key, v.encode())).decode() for k, v in params.items()}
    params |= {"signature": _signature(path, signed_nonce, params), "ssecurity": session["ssecurity"], "_nonce": nonce}
    return params, key


def decrypt(key: bytes, content: bytes) -> bytes:
    return rc4(key, base64.b64decode(content))


def error(data: Any) -> str | None:
    """Mi cloud answers HTTP 200 with its own code; anything but 0 is a refusal."""
    if isinstance(data, dict) and data.get("code") != 0:
        return f"code {data.get('code')}: {data.get('message', '')}"
    return None


def fake_reply(request: httpx.Request, body: Any, session: dict[str, str] = TEST_SESSION) -> httpx.Response:
    """For tests: the Mi cloud answer `body`, encrypted with the key of `request`."""
    nonce = dict(parse_qsl(request.content.decode()))["_nonce"]
    key = hashlib.sha256(base64.b64decode(session["ssecurity"]) + base64.b64decode(nonce)).digest()
    return httpx.Response(200, content=base64.b64encode(rc4(key, json.dumps(body).encode())))


def _json(text: str) -> dict[str, Any]:
    return json.loads(text.removeprefix("&&&START&&&"))


async def start_login(client: httpx.AsyncClient) -> tuple[str, str]:
    """(page the user opens to sign in, long-poll URL that answers once they have)."""
    r = await client.get(
        LOGIN_URL,
        params={
            "_qrsize": 480,
            "qs": quote("?sid=xiaomiio&_json=true", safe=""),
            "callback": "https://sts.api.io.mi.com/sts",
            "_hasLogo": "false",
            "sid": "xiaomiio",
            "serviceParam": "",
            "_locale": "en_US",
            "_dc": int(time.time() * 1000),
        },
    )
    r.raise_for_status()
    d = _json(r.text)
    return d["loginUrl"], d["lp"]


async def finish_login(client: httpx.AsyncClient, poll_url: str) -> dict[str, str]:
    """Wait (up to LOGIN_MINUTES) for the user to sign in, then return the session."""
    deadline = time.monotonic() + LOGIN_MINUTES * 60
    while True:
        try:
            r = await client.get(poll_url, timeout=60)
            break
        except httpx.ReadTimeout:  # long poll: no answer yet
            if time.monotonic() > deadline:
                raise TimeoutError(f"nobody signed in within {LOGIN_MINUTES} minutes") from None
    d = _json(r.text)
    if d.get("code") != 0 or not d.get("ssecurity"):
        raise RuntimeError(f"Xiaomi sign-in failed: {d.get('desc') or d.get('code')}")
    r = await client.get(d["location"])  # sets the serviceToken cookie
    token = r.cookies.get("serviceToken") or client.cookies.get("serviceToken")
    return session_of(json.dumps({"userId": str(d["userId"]), "ssecurity": d["ssecurity"], "serviceToken": token}))


def base_url(region: str) -> str:
    return f"https://{'' if region == 'cn' else region + '.'}api.io.mi.com/app"


async def whoami(client: httpx.AsyncClient, session: dict[str, str]) -> dict[str, Any] | None:
    """The signed-in account (masked) and the regions holding its devices; None when the session is no longer valid."""

    async def devices(region: str) -> int | None:
        url = base_url(region) + "/home/device_list"
        form, key = encrypt(url, session, {"getVirtualModel": False, "getHuamiDevices": 0})
        jar = "; ".join(f"{k}={v}" for k, v in cookies(session).items())
        r = await client.post(url, data=form, headers={**HEADERS, "Cookie": jar})
        if r.status_code == 401:
            return None
        data = json.loads(decrypt(key, r.content)) if r.status_code == 200 else {}
        return len((data.get("result") or {}).get("list") or [])

    counts = dict(zip(REGIONS, await asyncio.gather(*(devices(r) for r in REGIONS)), strict=True))
    if None in counts.values():
        return None
    uid = str(session["userId"])
    return {
        "account": f"{uid[:4]}***{uid[-3:]}",
        "found": {r: {"devices": n, "base_url": base_url(r)} for r, n in counts.items() if n},
    }
