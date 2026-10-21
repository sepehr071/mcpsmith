"""Sign-in auth for apps that sign their own requests: the user signs in on the provider's own page, the session is
kept in the OS keychain, and the provider signs (and may encrypt) every call.

A provider is a module with: TITLE, ACCOUNT (who should sign in), USER_AGENT, HEADERS, TEST_SESSION, session_of(raw),
cookies(session), encrypt(url, session, payload) -> (form, key), decrypt(key, content), error(data),
fake_reply(request, body), start_login(client), finish_login(client, poll) and whoami(client, session).
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable
from types import ModuleType
from typing import TYPE_CHECKING, Any

import httpx
from mcp.server.mcpserver.exceptions import ToolError

from ... import secrets
from . import xiaomi

if TYPE_CHECKING:
    from ..http import Api

log = logging.getLogger("mcpsmith")
PROVIDERS: dict[str, ModuleType] = {"xiaomi": xiaomi}
LOGIN_HINT = "Call this server's *_login tool (smith_login in the mcpsmith hub) and give the user its link."
_logins: dict[str, asyncio.Task] = {}  # sign-ins waiting for the user, by keychain name


def provider(name: str) -> ModuleType:
    if name not in PROVIDERS:
        raise ValueError(f"unknown sign-in provider {name!r}; use one of {sorted(PROVIDERS)}")
    return PROVIDERS[name]


def keychain(name: str) -> Callable[[str], str | None]:
    """The `Api(secret=...)` lookup for sessions saved by `login` under `name`."""
    return lambda env: secrets.get(name, env)


def test_session(name: str) -> str:
    """For tests: a session JSON the provider accepts."""
    return json.dumps(provider(name).TEST_SESSION)


def fake_reply(name: str, request: httpx.Request, body: Any) -> httpx.Response:
    """For tests: the provider's answer `body`, signed or encrypted for `request` as the real service would."""
    return provider(name).fake_reply(request, body)


def _client(api: Api, p: ModuleType) -> httpx.AsyncClient:
    from ..http import tls_context  # http imports this package

    return httpx.AsyncClient(
        proxy=(api._proxies() or [None])[0],
        verify=tls_context(),
        trust_env=False,
        follow_redirects=True,
        headers={"User-Agent": p.USER_AGENT},
        timeout=30,
    )


async def _status(api: Api, p: ModuleType, env: str) -> dict[str, Any] | None:
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
