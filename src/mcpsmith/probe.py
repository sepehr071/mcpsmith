"""Verify endpoints read from an app's code: call the read-only ones live and keep a scrubbed example response."""

from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass
from urllib.parse import urlsplit

import httpx

from . import browser
from .importers.har import _scrub, _trim
from .model import Endpoint, Source
from .runtime.http import Api, ApiError

DELAY = 0.2  # seconds between probes
STOP = (403, 429)  # statuses that mean "back off this host"
TRANSPORT: httpx.AsyncBaseTransport | None = None  # tests swap in httpx.MockTransport


@dataclass
class ProbeResult:
    endpoint: str
    ok: bool = False
    status: int | None = None
    error: str | None = None
    chars: int = 0
    skipped_reason: str | None = None


async def _from_chrome(host: str) -> dict[str, str]:
    if not (st := browser.status()):
        raise ConnectionError("mcpsmith Chrome is not running")
    return await browser.cookies_for(host, st["port"])


COOKIES = _from_chrome  # tests swap in a fake


def _api(source: Source, hosts: list[str], env: dict[str, str] | None) -> Api:
    auth, extra = None, {}
    if source.auth and (a := source.auth[0]).type == "browser":
        auth = {"type": "browser"}
        extra = {"cookie_provider": COOKIES, "login_path": a.login_path, "session_cookie": a.session_cookie}
    elif source.auth and a.env:  # a scheme without an env var has no credential to send
        auth = {"type": a.type, "env": a.env, "param": a.param}
    api = Api(
        source.base_url,
        "MCPSMITH_PROBE",
        hosts=hosts,
        headers=source.headers,
        auth=auth,
        concurrency=2,
        cache_ttl=0,
        secret=(env or {}).get,
        **extra,
    )
    api.RETRIES = 0  # a 429 must stop the probe, not be retried
    api.set_transport(TRANSPORT)
    return api


def _target(e: Endpoint) -> tuple[str, dict, dict] | str:
    """(url, path values, query) for the request, or the reason it cannot be sent."""
    path = {p.name: p.example for p in e.params if p.location == "path" and p.example is not None}
    missing = [n for n in re.findall(r"\{([^{}]+)\}", e.path) if n not in path]
    if missing:
        return f"unfilled placeholder {{{missing[0]}}} in the path; give the parameter an example value"
    query = {p.name: p.example for p in e.params if p.location == "query" and p.required and p.example is not None}
    return (e.base_url.rstrip("/") + e.path if e.base_url else e.path), path, query


def run(
    source: Source, *, writes: bool = False, limit: int = 20, env: dict[str, str] | None = None
) -> list[ProbeResult]:
    """Probe up to `limit` unverified endpoints in place; the caller saves the source."""
    return asyncio.run(_run(source, writes, limit, env))


async def _run(source: Source, writes: bool, limit: int, env: dict[str, str] | None) -> list[ProbeResult]:
    todo = [e for e in source.endpoints if not e.verified]
    hosts = [h for e in todo if e.base_url and (h := urlsplit(e.base_url).hostname)]
    api = _api(source, hosts, env)
    stopped: dict[str, str] = {}
    results: list[ProbeResult] = []
    sent = False
    sent_count = 0
    for e in todo:
        r = ProbeResult(e.id)
        results.append(r)
        method = e.method.upper()
        if method != "GET" and not writes:
            r.skipped_reason = f"{method} may write; pass writes=true to probe it"
            continue
        if isinstance(t := _target(e), str):
            r.skipped_reason = t
            continue
        url, path, query = t
        host = urlsplit(e.base_url or source.base_url).hostname or ""
        if host in stopped:
            r.skipped_reason = f"{host} {stopped[host]}; probing stopped for it"
            continue
        if sent_count >= limit:
            r.skipped_reason = f"limit of {limit} requests reached"
            continue
        if sent:
            await asyncio.sleep(DELAY)
        sent = True
        sent_count += 1
        try:
            data = await api.call(method, url, path=path, query=query)
        except ApiError as err:
            r.status, r.error = err.status, str(err)
            if err.status in STOP:
                stopped[host] = f"answered {err.status} (rate limit or blocked)"
            elif err.expired:
                stopped[host] = "session has expired"
            continue
        e.response_example = _trim(_scrub(data))  # scrub before trimming so no half-cut email survives
        e.verified = True
        r.ok, r.status, r.chars = True, 200, len(json.dumps(data, default=str))
    return results
