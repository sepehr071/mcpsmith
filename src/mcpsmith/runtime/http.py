"""Async HTTP client for generated servers: one `Api` instance per upstream API.

Every tool goes through `Api.call`, which caps concurrency, caches GETs, retries transient failures and turns
HTTP failures into `ApiError` messages the model can act on.
"""

from __future__ import annotations

import asyncio
import base64
import copy
import functools
import hashlib
import json
import logging
import os
import random
import re
import ssl
import time
from collections.abc import Awaitable, Callable
from typing import Any
from urllib.parse import quote

import httpx
import truststore
from mcp.server.mcpserver.exceptions import ToolError

from . import signin

log = logging.getLogger("mcpsmith")

AUTH_TYPES = {"bearer", "header", "query", "cookie", "basic", "browser", "signin"}
IDEMPOTENT = {"GET", "HEAD", "OPTIONS", "PUT", "DELETE"}


class ApiError(ToolError):
    """A failed upstream call. `status` is the HTTP status, or None for network errors."""

    def __init__(self, message: str, status: int | None = None, expired: bool = False):
        super().__init__(message)
        self.status = status
        self.expired = expired  # the login session ended; every further call would fail the same way


@functools.cache
def tls_context() -> ssl.SSLContext:
    """Verify with the OS trust store (fixes incomplete chains, honors corporate CAs); verification stays on."""
    return truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)


@functools.cache
def _curl() -> tuple[type, Any] | None:
    """The curl_cffi transport class (Chrome TLS/HTTP2 fingerprint) and CurlOpt, or None without the extra."""
    try:
        from curl_cffi.const import CurlOpt
        from httpx_curl_cffi import AsyncCurlTransport
    except ImportError:
        log.warning("impersonate=True needs `pip install mcpsmith[stealth]`; falling back to plain httpx.")
        return None

    class XhrTransport(AsyncCurlTransport):
        """The Chrome profile sends navigation-only headers; a None value makes curl drop them (API calls are XHR)."""

        def _create_request_params(self, req: httpx.Request) -> dict[str, Any]:
            p = super()._create_request_params(req)
            p["headers"] = [*p["headers"], (b"upgrade-insecure-requests", None), (b"sec-fetch-user", None)]
            return p

    return XhrTransport, CurlOpt


class Api:
    """HTTP access to one API. `<env_prefix>_BASE_URL` overrides base_url, `<env_prefix>_PROXY` sets proxies."""

    JITTER = (0.15, 0.6)  # seconds before each uncached call, only when impersonating a browser
    RETRIES = 2
    RETRY_DELAY = 0.5  # seconds, doubled per retry
    MAX_RETRY_WAIT = 10  # cap for Retry-After and backoff, seconds
    MAX_BODY = 5_000_000  # bytes; larger responses are refused
    CACHE_MAX = 64
    TEXT_CLIP = 2000  # characters kept of a non-JSON response

    def __init__(
        self,
        base_url: str,
        env_prefix: str,
        hosts: list[str] | None = None,
        headers: dict[str, str] | None = None,
        auth: dict[str, str] | None = None,
        impersonate: bool = False,
        concurrency: int = 2,
        cache_ttl: int = 60,
        secret: Callable[[str], str | None] | None = None,
        cookie_provider: Callable[[str], Awaitable[dict[str, str]]] | None = None,
        login_path: str = "",
        session_cookie: str = "",
        proxy: str | None = None,
    ):
        if auth and (auth.get("type") not in AUTH_TYPES or not (auth.get("env") or auth.get("type") == "browser")):
            raise ValueError(f"auth needs type (one of {sorted(AUTH_TYPES)}) and env, got {auth!r}")
        if auth and auth["type"] == "signin":
            signin.provider(auth.get("provider", ""))  # fail at start, not on the first call
        if concurrency < 1:
            raise ValueError(f"concurrency must be at least 1, got {concurrency}")
        self.env_prefix = env_prefix
        self.base_url = (os.environ.get(f"{env_prefix}_BASE_URL") or base_url).rstrip("/")
        self.hosts = {*(hosts or []), httpx.URL(self.base_url).host}
        self.headers = dict(headers or {})
        self.auth = auth
        self.impersonate = impersonate
        self.concurrency = concurrency
        self.cache_ttl = cache_ttl
        self.secret = secret
        self.cookie_provider = cookie_provider
        self.login_path = login_path
        self.session_cookie = session_cookie
        self.proxy = proxy
        self._transport: httpx.AsyncBaseTransport | None = None
        self._client: httpx.AsyncClient | None = None
        self._limit: asyncio.Semaphore | None = None
        self._proxy_index = 0
        self._retired: set[asyncio.Task] = set()
        self._cache: dict[tuple, tuple[float, Any]] = {}

    def set_transport(self, transport: httpx.AsyncBaseTransport | None) -> None:
        """Swap the transport (tests use httpx.MockTransport). Drops the current client and the cache."""
        self._transport, self._client, self._limit, self._proxy_index = transport, None, None, 0
        self._cache.clear()

    async def call(
        self,
        method: str,
        url: str,
        path: dict[str, Any] | None = None,
        query: dict[str, Any] | None = None,
        body: Any = None,
        headers: dict[str, Any] | None = None,
        cookies: dict[str, Any] | None = None,
        encoding: str = "json",
    ) -> Any:
        """Send one request and return the parsed JSON body (`{"text": ...}` when it is not JSON).

        `url` is a path template relative to base_url ("/pet/{petId}", filled from `path`) or an absolute URL on
        an allowed host. None values are dropped; dotted `body` keys nest ({"a.b": 1} -> {"a": {"b": 1}}).
        `encoding` sends the body as "json", "form" (urlencoded) or "multipart" (text fields).
        Retried up to RETRIES times on timeout, network error, 5xx or 429, with jittered exponential backoff or
        after Retry-After when sent. 403 is never retried.
        """
        method = method.upper()
        query = _flat("query", _present(query), lists=True)
        headers = {k: _text(v) for k, v in _flat("header", _present(headers)).items()}
        cookies = {k: _text(v) for k, v in _flat("cookie", _present(cookies)).items()}
        url = self._url(url, _present(path))
        host = httpx.URL(url).host
        if host not in self.hosts:
            raise ApiError(f"Refusing to call unexpected host {host}.")
        sent, session, signer = False, None, None
        if _origin(url) == _origin(self.base_url):  # other hosts, ports and schemes never see the credential
            sent = self._add_auth(query, headers, cookies)
            if self.auth and self.auth["type"] == "browser" and self.cookie_provider:
                try:
                    jar = await self.cookie_provider(host)
                except (OSError, httpx.HTTPError, RuntimeError):
                    raise ApiError(
                        "The mcpsmith Chrome is not running. Start it with `mcpsmith browser`, log in, then retry."
                    ) from None
                if self.session_cookie and self.session_cookie not in jar:
                    raise ApiError(self._expired_message(host), expired=True)
                cookies.update(jar)
                sent = True
            if self.auth and self.auth["type"] == "signin":
                signer = signin.provider(self.auth["provider"])
                session = self._signin_session(signer)
                cookies.update(signer.cookies(session))
                headers.update(signer.HEADERS)
                sent = True
        if any(re.search(r"[;\r\n]", str(v)) for v in cookies.values()) or any(
            re.search(r"[\r\n]", str(v)) for v in headers.values()
        ):
            raise ApiError("Refusing a header or cookie value containing ';' or a line break.")
        if cookies:
            headers["Cookie"] = "; ".join(f"{k}={v}" for k, v in cookies.items())
        payload = None if method == "GET" else _nest(body) if isinstance(body, dict) else body
        form, sign_key = signer.encrypt(url, session, payload) if session else (None, None)
        if form:
            send: dict[str, Any] = {"data": form}
        elif encoding == "form":
            send = {"data": _fields(payload)}
        elif encoding == "multipart":
            # shortcut: text fields only, no file upload; add (filename, bytes) parts when a plan needs one.
            send = {"files": [(k, (None, v)) for k, vs in _fields(payload).items() for v in vs]}
        else:
            send = {"json": payload}

        shown = {k: hashlib.sha256(v.encode()).hexdigest() if k == "Cookie" else v for k, v in headers.items()}
        key = (url, json.dumps([query, shown], sort_keys=True, default=str))
        cached = method == "GET" and self.cache_ttl > 0
        if cached and (hit := self._cache.get(key)) and time.monotonic() - hit[0] < self.cache_ttl:
            return copy.deepcopy(hit[1])
        # A non-idempotent request that failed mid-way may have been applied: retry only an outright 429.
        safe = method in IDEMPOTENT
        for attempt in range(self.RETRIES + 1):
            last = attempt == self.RETRIES
            wait = self.RETRY_DELAY * 2**attempt * random.uniform(1, 1.5)
            try:
                r, content = await self._request(method, url, query, headers, send, host)
            except ApiError as e:
                if last or e.status is not None or not safe:
                    raise
                await asyncio.sleep(min(wait, self.MAX_RETRY_WAIT))
                continue
            if (r.status_code == 429 or r.status_code >= 500 and safe) and not last:
                await asyncio.sleep(min(_retry_after(r) or wait, self.MAX_RETRY_WAIT))
                continue
            break

        if self.auth and self.auth["type"] == "browser" and sent and self._expired(r):
            raise ApiError(self._expired_message(host), expired=True)
        if session and r.status_code == 401:
            raise ApiError(f"The {host} sign-in has expired. {signin.LOGIN_HINT}", 401, expired=True)
        if r.status_code >= 300:
            raise ApiError(self._status_message(r, content, host, sent), r.status_code)
        if method != "GET":
            self._cache.clear()  # a write may change what any cached GET would return
        if not content:
            return {"status": r.status_code}
        if sign_key:
            content = signer.decrypt(sign_key, content)
        try:
            data = json.loads(content)
        except ValueError:
            text = content.decode("utf-8", "replace")
            return {"text": text[: self.TEXT_CLIP] + ("...[cut]" if len(text) > self.TEXT_CLIP else "")}
        if session and (refused := signer.error(data)):  # an HTTP 200 carrying the service's own error
            raise ApiError(f"{host} refused the request ({refused})")
        if cached:
            if len(self._cache) >= self.CACHE_MAX:
                self._cache.pop(next(iter(self._cache)))
            self._cache[key] = (time.monotonic(), copy.deepcopy(data))
        return data

    @staticmethod
    def _expired_message(host: str) -> str:
        return f"{host} says the session has expired. Log in again in the mcpsmith Chrome window, then retry."

    def _expired(self, r: httpx.Response) -> bool:
        """A 401, or a redirect to the site's login page."""
        loc = httpx.URL(r.headers.get("location", "")).path
        return r.status_code == 401 or (
            300 <= r.status_code < 400 and bool(self.login_path) and loc.startswith(self.login_path)
        )

    def _url(self, url: str, path: dict[str, Any]) -> str:
        def fill(m: re.Match) -> str:
            if m.group(1) not in path or str(path[m.group(1)]) == "":  # "" would turn /items/{id} into /items/
                raise ApiError(f"Missing value for path parameter '{m.group(1)}'.")
            v = quote(_text(path[m.group(1)]), safe="")
            return v.replace(".", "%2E") if v in (".", "..") else v  # httpx would resolve "." and ".." segments

        url = re.sub(r"\{([^{}]+)\}", fill, url)
        return url if url.startswith(("http://", "https://")) else f"{self.base_url}/{url.lstrip('/')}"

    def _signin_session(self, signer) -> dict[str, str]:
        try:
            return signer.session_of(self._credential(self.auth["env"]))
        except ValueError:  # empty, not JSON, or missing fields
            raise ApiError(f"Not signed in to {signer.TITLE}. {signin.LOGIN_HINT}", expired=True) from None

    def _credential(self, env: str) -> str:
        """The environment variable first, then the keychain-backed `secret` callback."""
        return (os.environ.get(env) or (self.secret and self.secret(env)) or "").strip()

    def _add_auth(self, query: dict, headers: dict, cookies: dict) -> bool:
        """Add the credential; True when one was sent. Read once: a `once` secret is gone after this."""
        if not self.auth or self.auth["type"] in ("browser", "signin"):
            return False
        env, kind, param = self.auth["env"], self.auth["type"], self.auth.get("param", "")
        secret = self._credential(env)
        if not secret:
            return False  # public endpoints still work; a 401 then names the variable to set

        if kind == "bearer":
            headers["Authorization"] = f"Bearer {secret}"
        elif kind == "basic":  # env value is "user:password"
            headers["Authorization"] = "Basic " + base64.b64encode(secret.encode()).decode()
        else:
            {"header": headers, "query": query, "cookie": cookies}[kind][param] = secret
        return True

    def _proxies(self) -> list[str]:
        """An explicit `proxy`, else <P>_PROXY: one proxy URL, or a comma-separated list used one at a time."""
        if self.proxy:
            return [self.proxy]
        return [p.strip() for p in os.environ.get(f"{self.env_prefix}_PROXY", "").split(",") if p.strip()]

    def _next_proxy(self, client: httpx.AsyncClient) -> None:
        """Sticky pool: move to the next proxy after a block or connect error; the next call rebuilds the client.

        Only the client that failed may rotate: a slower request that failed on an old client must not skip a good
        proxy. The old client is closed late because other requests may still be using it.
        """
        if len(self._proxies()) < 2 or client is not self._client:
            return
        self._client, self._limit, self._proxy_index = None, None, self._proxy_index + 1
        t = asyncio.create_task(_close_later(client))
        self._retired.add(t)
        t.add_done_callback(self._retired.discard)

    def _get_client(self) -> tuple[httpx.AsyncClient, asyncio.Semaphore]:
        if self._client is None:
            # System proxy settings are ignored unless the user opts in with <P>_PROXY. The proxy goes to the
            # transport (a client-level proxy would be ignored once a transport is given).
            proxies = self._proxies()
            proxy = proxies[self._proxy_index % len(proxies)] if proxies else None
            self._client = httpx.AsyncClient(
                transport=self._transport or self._make_transport(proxy),
                headers=self.headers,
                timeout=httpx.Timeout(20, connect=5),
                limits=httpx.Limits(max_connections=2 * self.concurrency),
                follow_redirects=False,
                trust_env=False,
            )
            self._limit = asyncio.Semaphore(self.concurrency)
        assert self._limit is not None
        return self._client, self._limit

    def _make_transport(self, proxy: str | None) -> httpx.AsyncBaseTransport:
        if self.impersonate and (curl := _curl()):
            transport, curl_opt = curl
            # curl_cffi presents Chrome's TLS/HTTP2 fingerprint and default headers (User-Agent, sec-ch-ua).
            return transport(
                impersonate="chrome",
                default_headers=True,
                curl_options={curl_opt.NOPROXY: "*"},  # httpx-curl-cffi drops its own NOPROXY unless non-empty
                proxy=proxy,
                max_connections=2 * self.concurrency,
            )
        return httpx.AsyncHTTPTransport(
            verify=tls_context(), proxy=proxy, limits=httpx.Limits(max_connections=2 * self.concurrency)
        )

    async def _request(self, method, url, query, headers, body, host) -> tuple[httpx.Response, bytes]:
        client, limit = self._get_client()
        try:
            async with limit:
                if self.impersonate:
                    await asyncio.sleep(random.uniform(*self.JITTER))
                async with client.stream(method, url, params=query, headers=headers, **body) as r:
                    content = await self._read_capped(r, host)
        except httpx.TimeoutException as e:
            raise ApiError(f"{host} did not answer in time. Try again in a moment.") from e
        except httpx.RequestError as e:
            if isinstance(e, httpx.ConnectError | httpx.ProxyError):
                self._next_proxy(client)
            raise ApiError(f"Could not reach {host} ({type(e).__name__}). Check the internet connection.") from e
        if r.status_code in (403, 429):
            self._next_proxy(client)  # blocked: try the next proxy on the following call
        return r, content

    async def _read_capped(self, r: httpx.Response, host: str) -> bytes:
        """Read the body, refusing anything over MAX_BODY so one huge answer cannot flood memory or context."""
        buf = bytearray()
        async for chunk in r.aiter_bytes():
            buf += chunk
            if len(buf) > self.MAX_BODY:
                raise ApiError(f"{host} sent a response over {self.MAX_BODY // 1_000_000} MB; refused.", r.status_code)
        return bytes(buf)

    def _status_message(self, r: httpx.Response, content: bytes, host: str, sent: bool) -> str:
        code, env = r.status_code, self.auth.get("env") if self.auth else None
        if code == 401:
            if env and not sent:
                return f"{host} requires authentication (HTTP 401). Set {env} to your API credential and restart."
            if env:
                return f"{host} rejected the credentials (HTTP 401). Check that {env} holds a valid, unexpired value."
            return f"{host} requires authentication (HTTP 401); this server has no credentials for it."
        if code == 403:
            hint = f"Check that {env} has access to this, or the" if env else "The"
            return (
                f"{host} blocked the request (HTTP 403). {hint} site blocks this IP: turn off VPN/proxy, "
                f"or set {self.env_prefix}_PROXY to another proxy."
            )
        if code == 404:
            return f"Not found on {host} (HTTP 404). Check the id / path arguments."
        if code == 429:
            return f"{host} is rate limiting requests (HTTP 429). Wait a minute before retrying."
        if code >= 500:
            return f"{host} had a server error (HTTP {code}). Try again later."
        if code < 400:
            loc = httpx.URL(r.headers.get("location", "?"))  # query and userinfo may carry codes or tokens
            to = f"{loc.scheme}://{loc.host}{loc.path}" if loc.host else loc.path
            return f"{host} redirected the request (HTTP {code}) to {to}; not followed."
        try:
            detail = _error_text(json.loads(content))
        except ValueError:
            detail = content[:200].decode("utf-8", "replace")
        return f"{host} rejected the request (HTTP {code}): {detail}"


async def _close_later(client: httpx.AsyncClient) -> None:
    await asyncio.sleep(30)  # longer than the 20 s request timeout
    await client.aclose()


def _present(d: dict[str, Any] | None) -> dict[str, Any]:
    """Copy without None values (httpx would send None as an empty value); 5.0 is sent as 5."""
    return {k: int(v) if isinstance(v, float) and v.is_integer() else v for k, v in (d or {}).items() if v is not None}


def _text(v: Any) -> str:
    """A scalar as sent on the wire: booleans lowercase, like JSON and httpx query params."""
    return str(v).lower() if isinstance(v, bool) else str(v)


def _flat(kind: str, d: dict[str, Any], lists: bool = False) -> dict[str, Any]:
    """Refuse values that have no text form here (an object; a list too, except repeated query keys)."""
    for k, v in d.items():
        items = v if lists and isinstance(v, list) else [v]
        if any(isinstance(x, dict | list) for x in items):
            raise ApiError(f"The {kind} value '{k}' must be text or a number, not {type(v).__name__}.")
    return d


def _fields(payload: Any) -> dict[str, list[str]]:
    """Form or multipart fields from a body object: text values, a list repeats the key."""
    if payload is None:
        return {}
    if not isinstance(payload, dict):
        raise ApiError(f"A form body must be an object of fields, not {type(payload).__name__}.")
    return {
        k: [_text(x) for x in v] if isinstance(v, list) else [_text(v)] for k, v in _flat("form", payload, True).items()
    }


def _origin(url: str) -> tuple[str, str, int | None]:
    u = httpx.URL(url)
    return u.scheme, u.host, u.port or {"http": 80, "https": 443}.get(u.scheme)


def _nest(body: dict[str, Any]) -> dict[str, Any]:
    """{"a.b": 1, "c": 2} -> {"a": {"b": 1}, "c": 2}; {"p.0": 1, "p.1": 2} -> {"p": [1, 2]}; None values dropped."""
    out: dict[str, Any] = {}
    made: set[int] = set()  # nodes built from dotted keys; only these may become lists
    by: dict[str, str] = {}  # dotted path -> the body key that set it, to name both sides of a clash
    for key, value in body.items():
        if value is None:
            continue
        *parents, leaf = key.split(".")
        node = out
        for i, p in enumerate(parents):
            at = ".".join(parents[: i + 1])
            if p not in node:
                node[p] = {}
                made.add(id(node[p]))
                by.setdefault(at, key)
            elif not isinstance(node[p], dict):
                raise ApiError(
                    f"Body fields '{by.get(at, at)}' and '{key}' clash: '{at}' cannot be a value and an object."
                )
            node = node[p]
        if leaf in node and id(node[leaf]) in made:
            raise ApiError(f"Body fields '{by[key]}' and '{key}' clash: '{key}' cannot be a value and an object.")
        node[leaf] = copy.deepcopy(value)
        by.setdefault(key, key)
    return _listify(out, made)


def _listify(node: dict[str, Any], made: set[int]) -> Any:
    """Turn built nodes whose keys are exactly 0..n-1 into lists."""
    out = {k: _listify(v, made) if id(v) in made else v for k, v in node.items()}
    if id(node) in made and out and set(out) == {str(i) for i in range(len(out))}:
        return [out[str(i)] for i in range(len(out))]
    return out


def _retry_after(r: httpx.Response) -> float | None:
    """Seconds from a numeric Retry-After on a 429/503, else None (HTTP-date form is ignored)."""
    v = r.headers.get("retry-after", "")
    return float(v) if r.status_code in (429, 503) and v.replace(".", "", 1).isdigit() else None


def _error_text(body: Any) -> str:
    if isinstance(body, dict):
        err = body.get("detail") or body.get("error") or body.get("message") or body.get("messages") or body
        if isinstance(err, list) and err and isinstance(err[0], dict):
            err = err[0].get("msg") or err[0].get("message") or err[0]
        elif isinstance(err, dict):
            err = err.get("message") or err.get("msg") or err
        return str(err)[:300]
    return str(body)[:300]
