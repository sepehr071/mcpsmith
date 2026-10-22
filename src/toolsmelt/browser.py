"""One shared Chrome (remote debugging + a persistent profile) that the agent and toolsmelt both drive over CDP."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import subprocess
import sys
from collections.abc import AsyncIterator
from pathlib import Path

import httpx
from websockets.asyncio.client import connect

from . import getenv

_CANDIDATES = [
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/usr/bin/google-chrome",
    "/usr/bin/google-chrome-stable",
    "/usr/bin/chromium",
    "/usr/bin/chromium-browser",
]


def home() -> Path:
    if env := getenv("HOME"):
        return Path(env)
    new, old = Path.home() / ".toolsmelt", Path.home() / ".mcpsmith"
    return old if old.is_dir() and not new.exists() else new  # keep using connections saved before the rename


def find_chrome() -> Path:
    env = getenv("CHROME")
    for c in ([env] if env else []) + _CANDIDATES:
        if Path(c).is_file():
            return Path(c)
    raise RuntimeError(
        "Chrome not found; set TOOLSMELT_CHROME. Searched: " + ", ".join(([env] if env else []) + _CANDIDATES)
    )


def _version(port: int) -> dict | None:
    # trust_env=False: loopback must never go through the shell's proxy
    try:
        r = httpx.get(f"http://127.0.0.1:{port}/json/version", timeout=2, trust_env=False)
        return r.json() if r.status_code == 200 else None
    except (httpx.HTTPError, ValueError):
        return None


def status() -> dict | None:
    try:
        info = json.loads((home() / "browser.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return info if _version(info["port"]) else None


def clean_proxy(proxy: str | None) -> str | None:
    """scheme://host:port only: a bare host:port gets http://, user:pass is dropped (Chrome ignores it anyway)."""
    if not proxy:
        return None
    url = httpx.URL(proxy if "://" in proxy else "http://" + proxy)
    return str(url.copy_with(username=None, password=None))


def launch(port: int = 9222, proxy: str | None = None, no_proxy: bool = False) -> dict:
    """Start Chrome detached (or reuse one already answering on the port); remember it in browser.json."""
    if proxy is None and not no_proxy:
        proxy = os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy")
    if proxy and not no_proxy and "@" in proxy.split("://")[-1].split("/")[0]:
        print("toolsmelt: warning: authenticated proxies are not supported for the shared Chrome; credentials dropped")
    info = {"port": port, "proxy": None if no_proxy else clean_proxy(proxy)}
    if not _version(port):
        cmd = [
            str(find_chrome()),
            f"--remote-debugging-port={port}",
            f"--user-data-dir={home() / 'chrome-profile'}",
            "--no-first-run",
        ]
        if info["proxy"]:
            cmd.append(f"--proxy-server={info['proxy']}")
        detach = (
            {"creationflags": subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP}
            if sys.platform == "win32"
            else {"start_new_session": True}
        )
        subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **detach)
    home().mkdir(parents=True, exist_ok=True)
    (home() / "browser.json").write_text(json.dumps(info), encoding="utf-8", newline="\n")
    return info


class Cdp:
    """Browser-level CDP connection. `call` sends a command; `events` yields every message without an id."""

    def __init__(self, ws):
        self._ws = ws
        self._next = 0
        self._pending: dict[int, asyncio.Future] = {}
        self._events: asyncio.Queue = asyncio.Queue()
        self._reader = asyncio.ensure_future(self._read())

    @classmethod
    async def connect(cls, port: int) -> Cdp:
        async with httpx.AsyncClient(trust_env=False, timeout=5) as c:
            url = (await c.get(f"http://127.0.0.1:{port}/json/version")).json()["webSocketDebuggerUrl"]
        return cls(await connect(url, proxy=None, max_size=None))

    async def _read(self) -> None:
        try:
            async for raw in self._ws:
                msg = json.loads(raw)
                fut = self._pending.pop(msg["id"], None) if "id" in msg else None
                if fut is None:
                    if "id" not in msg:
                        self._events.put_nowait(msg)
                elif "error" in msg:
                    fut.set_exception(RuntimeError(f"CDP {msg['error']}"))
                else:
                    fut.set_result(msg.get("result", {}))
        finally:
            for fut in self._pending.values():
                if not fut.done():
                    fut.set_exception(RuntimeError("CDP connection closed"))

    async def call(self, method: str, params: dict | None = None, session: str | None = None) -> dict:
        self._next += 1
        msg = {"id": self._next, "method": method, "params": params or {}}
        if session:
            msg["sessionId"] = session
        fut = self._pending[self._next] = asyncio.get_running_loop().create_future()
        await self._ws.send(json.dumps(msg))
        return await fut

    async def events(self) -> AsyncIterator[dict]:
        while True:
            yield await self._events.get()

    async def close(self) -> None:
        self._reader.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self._reader
        await self._ws.close()

    async def __aenter__(self) -> Cdp:
        return self

    async def __aexit__(self, *exc) -> None:
        await self.close()


async def cookies_for(host: str, port: int) -> dict[str, str]:
    """Cookies of the shared browser that apply to host, as {name: value}. Held in memory only."""

    async def read() -> list[dict]:
        async with await Cdp.connect(port) as cdp:
            return (await cdp.call("Storage.getCookies"))["cookies"]

    cookies = await asyncio.wait_for(read(), timeout=10)
    return {c["name"]: c["value"] for c in cookies if host == (d := c["domain"].lstrip(".")) or host.endswith("." + d)}
