"""Sign-in auth for apps that sign their own requests: the user signs in on the provider's own page, the session is
kept in the OS keychain, and the provider signs (and may encrypt) every call.

A provider is a module (or object) implementing `Provider` below; `xiaomi.py` is the reference. Built-ins are listed in
`_BUILTIN`; installed packages add more through the `toolsmelt.signin` entry point group (see docs/extending.md).
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable
from functools import cache
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

import httpx
from mcp.server.mcpserver.exceptions import ToolError

from ... import plugins, secrets
from . import xiaomi

if TYPE_CHECKING:
    from ..http import Api


@runtime_checkable
class Provider(Protocol):
    """What a sign-in provider offers. The session is the dict `session_of` builds from the keychain JSON."""

    TITLE: str  # service name shown to the user, e.g. "Xiaomi"
    ACCOUNT: str  # who should sign in, e.g. "the Xiaomi account their Mi Home app uses"
    USER_AGENT: str
    HEADERS: dict[str, str]  # sent with every signed call
    TEST_SESSION: dict[str, str]  # a session that `fake_reply` accepts, for tests
    LOGIN_MINUTES: int  # how long a sign-in link stays valid

    def session_of(self, raw: str) -> dict[str, str]:
        """Parse the saved session; ValueError when it is empty or incomplete."""

    def cookies(self, session: dict[str, str]) -> dict[str, str]: ...

    def encrypt(self, url: str, session: dict[str, str], payload: Any) -> tuple[dict[str, str], bytes]:
        """Sign (and maybe encrypt) a call: the form to send and the key `decrypt` needs."""

    def decrypt(self, key: bytes, content: bytes) -> bytes: ...

    def error(self, data: Any) -> str | None:
        """The service's error message in a decoded reply, or None."""

    def fake_reply(self, request: httpx.Request, body: Any) -> httpx.Response:
        """For tests: `body` signed or encrypted for `request` as the real service would."""

    async def start_login(self, client: httpx.AsyncClient) -> tuple[str, str]:
        """(link for the user, poll handle for `finish_login`)."""

    async def finish_login(self, client: httpx.AsyncClient, poll: str) -> dict[str, str]:
        """Wait for the user to sign in; return the session to save."""

    async def whoami(self, client: httpx.AsyncClient, session: dict[str, str]) -> dict[str, Any] | None:
        """Who is signed in, with `found`: {region: {"base_url": ...}}; None when the session is no longer valid."""


log = logging.getLogger("toolsmelt")
_BUILTIN: dict[str, Provider] = {"xiaomi": xiaomi}
LOGIN_HINT = "Call this server's *_login tool (smith_login in the toolsmelt hub) and give the user its link."
_logins: dict[str, asyncio.Task] = {}  # sign-ins waiting for the user, by keychain name


@cache
def _providers() -> dict[str, Provider]:
    found: dict[str, Provider] = {}
    for name, obj in plugins.load("toolsmelt.signin").items():
        if name in _BUILTIN:
            plugins.skip("toolsmelt.signin", name, "a built-in provider has that name")
        elif not isinstance(obj, Provider):
            plugins.skip("toolsmelt.signin", name, "it does not implement the Provider interface")
        else:
            found[name] = obj
    return {**_BUILTIN, **found}


def __getattr__(name: str) -> dict[str, Provider]:
    # PROVIDERS is computed on first use: http imports this package, and a plugin may import http while it loads.
    if name == "PROVIDERS":
        return _providers()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def provider(name: str) -> Provider:
    providers = _providers()
    if name not in providers:
        raise ValueError(f"unknown sign-in provider {name!r}; use one of {sorted(providers)}")
    return providers[name]


def keychain(name: str) -> Callable[[str], str | None]:
    """The `Api(secret=...)` lookup for sessions saved by `login` under `name`."""
    return lambda env: secrets.get(name, env)


def test_session(name: str) -> str:
    """For tests: a session JSON the provider accepts."""
    return json.dumps(provider(name).TEST_SESSION)


def fake_reply(name: str, request: httpx.Request, body: Any) -> httpx.Response:
    """For tests: the provider's answer `body`, signed or encrypted for `request` as the real service would."""
    return provider(name).fake_reply(request, body)


def _client(api: Api, p: Provider) -> httpx.AsyncClient:
    from ..http import tls_context  # http imports this package

    return httpx.AsyncClient(
        proxy=(api._proxies() or [None])[0],
        verify=tls_context(),
        trust_env=False,
        follow_redirects=True,
        headers={"User-Agent": p.USER_AGENT},
        timeout=30,
    )


async def _status(api: Api, p: Provider, env: str) -> dict[str, Any] | None:
    """Who is signed in and where their data is, or None when there is no valid session."""
    try:
        session = p.session_of(api._credential(env))
    except ValueError:  # empty, not JSON, or missing fields
        return None
    async with _client(api, p) as client:
        try:
            who = await p.whoami(client, session)
        except (httpx.HTTPError, ValueError, KeyError):
            raise ToolError(f"Could not reach {p.TITLE}; check the connection and retry.") from None
    if who is None:
        return None
    urls = [f["base_url"] for f in who["found"].values()]
    if not urls:
        hint = f"This account has no data. If it is the wrong one, call again with again=true and sign in with {p.ACCOUNT}."
    elif api.base_url not in urls:
        hint = f"The data is at {urls[0]}, not {api.base_url}: set {api.env_prefix}_BASE_URL to it and restart."
    else:
        hint = "Signed in. To use another account, call again with again=true."
    return {"signed_in": True, **who, "next": hint}


async def login(name: str, env: str, api: Api, again: bool = False) -> dict[str, Any]:
    """Report the current sign-in, or (none, or `again`) return a sign-in link; the session is saved once used."""
    p = provider(api.auth["provider"])
    if not again and (status := await _status(api, p, env)):
        return status
    if old := _logins.pop(name, None):
        old.cancel()
    client = _client(api, p)
    try:
        link, poll = await p.start_login(client)
    except (httpx.HTTPError, ValueError, KeyError):
        await client.aclose()
        raise ToolError(f"Could not reach {p.TITLE}'s sign-in service; check the connection and retry.") from None

    async def wait() -> None:
        try:
            secrets.set(name, env, json.dumps(await p.finish_login(client, poll)))
        except (httpx.HTTPError, TimeoutError, RuntimeError, ValueError, KeyError) as e:
            log.warning("%s sign-in for %s failed: %s", p.TITLE, name, e)
        finally:
            await client.aclose()

    _logins[name] = asyncio.create_task(wait())
    return {
        "signed_in": False,
        "open": link,
        "expires_minutes": p.LOGIN_MINUTES,
        "next": f"Give the user this link. They sign in with {p.ACCOUNT} (in a private window if the browser is "
        "signed in to another account). The session is saved by itself; then call this tool again to check "
        "which account signed in.",
    }
