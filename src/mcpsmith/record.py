"""Record the API traffic of the shared Chrome over CDP into a HAR. Secret headers never reach disk."""

from __future__ import annotations

import asyncio
import contextlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit

from . import browser
from .importers.har import _is_secret, _scrub

MAX_BODY = 2_000_000
DROPPED = "<dropped>"
SECRET_HEADERS = {"cookie", "set-cookie", "authorization", "proxy-authorization"}
SECRET_PARTS = ("csrf", "xsrf", "verification")
AUTH_SCHEMES = {"bearer", "basic", "digest", "token", "negotiate"}  # kept before '<dropped>' in Authorization
SECRETISH = re.compile(r"pass|token|secret|auth|key|sig|cookie|session", re.I)
TEXTUAL = re.compile(r"json|text|xml|javascript|x-www-form-urlencoded", re.I)
API_TYPES = {"xhr", "fetch"}  # CDP resource types whose response bodies are kept


def _url(url: str) -> str:
    parts = urlsplit(url)
    if not parts.query:
        return url
    pairs = [(k, DROPPED if _is_secret(k) else v) for k, v in parse_qsl(parts.query, keep_blank_values=True)]
    return parts._replace(query=urlencode(pairs)).geturl()


def _headers(h: dict | None) -> list[dict]:
    out = []
    for k, v in (h or {}).items():
        secret = k.lower() in SECRET_HEADERS or any(p in k.lower() for p in SECRET_PARTS) or _is_secret(k)
        value = DROPPED if secret else str(v)
        # the scheme word ('Bearer', 'Basic') is no secret and tells the importer which auth this is
        scheme = str(v).split(" ", 1)[0]
        if secret and k.lower() in ("authorization", "proxy-authorization") and scheme.lower() in AUTH_SCHEMES:
            value = f"{scheme} {DROPPED}"
        out.append({"name": k, "value": value})
    return out


class Recorder:
    def __init__(self):
        self.name: str | None = None
        self.hosts: list[str] = []
        self.entries: dict[tuple, dict] = {}
        self._cdp: browser.Cdp | None = None
        self._task: asyncio.Task | None = None
        self._bodies: set[asyncio.Task] = set()
        self._targets: set[str] = set()

    @property
    def running(self) -> bool:
        return self.name is not None

    def _match(self, url: str) -> bool:
        host = (urlsplit(url).hostname or "").lower()
        return not self.hosts or any(host == h or host.endswith("." + h) for h in self.hosts)

    async def start(self, name: str, hosts: list[str]) -> None:
        if self.running:
            raise RuntimeError(f"already recording '{self.name}'; call smith_record_stop first")
        if not re.fullmatch(r"\w[\w.-]*", name):
            raise ValueError("name must be letters, digits, '_', '-' or '.'")
        hosts = [h.strip().lower().lstrip(".") for h in hosts if h.strip().strip(".")]
        if not hosts:  # no filter would record every site open in the shared Chrome (mail, bank, ...)
            raise ValueError("hosts must name at least one site to record, e.g. ['shop.example.com']")
        info = browser.status()
        if not info:
            raise RuntimeError("Chrome is not running; start it with `mcpsmith browser`")
        self.name, self.hosts = name, hosts
        self.entries.clear()
        self._targets.clear()
        try:
            self._cdp = await browser.Cdp.connect(info["port"])
            self._task = asyncio.ensure_future(self._run())
            await asyncio.wait_for(self._attach_all(), 10)
        except BaseException:
            await self._close()
            self.name = None
            raise

    async def _attach_all(self) -> None:
        await self._cdp.call("Target.setDiscoverTargets", {"discover": True})
        await self._cdp.call(
            "Target.setAutoAttach", {"autoAttach": True, "waitForDebuggerOnStart": False, "flatten": True}
        )
        # tabs already open (the logged-in one); other clients attached to them do not matter
        for t in (await self._cdp.call("Target.getTargets")).get("targetInfos", []):
            if t.get("type") == "page" and t["targetId"] not in self._targets:
                r = await self._cdp.call("Target.attachToTarget", {"targetId": t["targetId"], "flatten": True})
                if t["targetId"] not in self._targets:
                    self._targets.add(t["targetId"])
                    await self._cdp.call("Network.enable", session=r["sessionId"])

    async def _run(self) -> None:
        async for ev in self._cdp.events():
            p, s = ev.get("params", {}), ev.get("sessionId")
            match ev["method"]:
                case "Target.attachedToTarget":
                    tid = p.get("targetInfo", {}).get("targetId")
                    if p.get("targetInfo", {}).get("type") == "page" and tid not in self._targets:
                        self._targets.add(tid)
                        self._spawn(self._cdp.call("Network.enable", session=p["sessionId"]))
                case "Network.requestWillBeSent":
                    self._request(s, p)
                case "Network.responseReceived":
                    if e := self.entries.get((s, p["requestId"])):
                        e["response"] = p["response"]
                        e["type"] = p.get("type") or e["type"]
                case "Network.loadingFinished":
                    if (e := self.entries.get((s, p["requestId"]))) and "response" in e:
                        e["size"] = p.get("encodedDataLength", 0)
                        self._spawn(self._body(s, p["requestId"], e))

    def _spawn(self, coro) -> None:
        t = asyncio.ensure_future(self._swallow(coro))
        self._bodies.add(t)
        t.add_done_callback(self._bodies.discard)

    @staticmethod
    async def _swallow(coro) -> None:
        with contextlib.suppress(RuntimeError, asyncio.TimeoutError):  # tab closed mid-request
            await asyncio.wait_for(coro, 10)

    def _request(self, session: str | None, p: dict) -> None:
        if not self._match(p["request"]["url"]):
            return
        self.entries[(session, p["requestId"])] = {
            "request": p["request"],
            "type": p.get("type") or "",
            "started": p.get("wallTime") or datetime.now(timezone.utc).timestamp(),
        }

    async def _body(self, session: str | None, request_id: str, e: dict) -> None:
        mime = e["response"].get("mimeType") or ""
        # API calls only: HTML pages and scripts carry names and codes the scrubber cannot recognise
        if e["type"].lower() not in API_TYPES or e["size"] > MAX_BODY or not TEXTUAL.search(mime):
            return
        r = await self._cdp.call("Network.getResponseBody", {"requestId": request_id}, session=session)
        if len(r.get("body", "")) <= MAX_BODY:
            e["body"] = r

    async def _close(self) -> None:
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
        if self._bodies:
            await asyncio.wait(self._bodies, timeout=10)
        if self._cdp:
            await self._cdp.close()
        self._task = self._cdp = None

    async def stop(self) -> Path:
        if not self.running:
            raise RuntimeError("not recording; call smith_record_start first")
        await self._close()
        out = browser.home() / "work" / self.name / "capture.har"
        out.parent.mkdir(parents=True, exist_ok=True)
        har = {"log": {"version": "1.2", "creator": {"name": "mcpsmith", "version": "1"}, "entries": self._har()}}
        out.write_text(json.dumps(har), encoding="utf-8", newline="\n")
        self.name = None
        return out

    @property
    def count(self) -> int:
        return len(self._har())

    def _har(self) -> list[dict]:
        return [_entry(e) for e in self.entries.values() if "response" in e]


def _entry(e: dict) -> dict:
    req, resp = e["request"], e["response"]
    mime = resp.get("mimeType") or ""
    content = {"size": e.get("size", 0), "mimeType": mime}
    body = e.get("body")
    if body and not body.get("base64Encoded") and (text := _clean(body["body"])) is not None:
        content["text"] = text
    entry = {
        "startedDateTime": datetime.fromtimestamp(e["started"], timezone.utc).isoformat(),
        "time": 0,
        "request": {
            "method": req["method"],
            "url": _url(req["url"]),
            "httpVersion": "HTTP/1.1",
            "headers": _headers(req.get("headers")),
            "queryString": [],
            "cookies": [],
            "headersSize": -1,
            "bodySize": len(req.get("postData") or ""),
        },
        "response": {
            "status": resp.get("status", 0),
            "statusText": resp.get("statusText", ""),
            "httpVersion": "HTTP/1.1",
            "headers": _headers(resp.get("headers")),
            "cookies": [],
            "content": content,
            "redirectURL": "",
            "headersSize": -1,
            "bodySize": -1,
        },
        "cache": {},
        "timings": {"send": 0, "wait": 0, "receive": 0},
        "_resourceType": e["type"].lower(),
    }
    if text := _clean(req.get("postData") or ""):
        entry["request"]["postData"] = {"mimeType": (req.get("headers") or {}).get("Content-Type", ""), "text": text}
    return entry


def _clean(text: str) -> str | None:
    """The body with secret and personal values masked (JSON or form-encoded), or None to drop it."""
    try:
        return json.dumps(_scrub(json.loads(text)), ensure_ascii=False)
    except ValueError:
        pass
    pairs = parse_qsl(text, keep_blank_values=True) if "=" in text else []
    if pairs:
        return urlencode([(k, _scrub(v, k)) for k, v in pairs])
    return None if SECRETISH.search(text) else _scrub(text)
