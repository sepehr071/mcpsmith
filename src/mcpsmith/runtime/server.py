"""Tool registry and MCPServer construction for generated servers."""

from __future__ import annotations

import argparse
import functools
import hmac
import inspect
import json
import logging
import os
import sys
from collections.abc import Callable
from typing import Any

from mcp.server import MCPServer
from mcp.types import ToolAnnotations

from .shape import clean

READ_ONLY = ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=True)
WRITE = ToolAnnotations(read_only_hint=False, destructive_hint=True, open_world_hint=True)


class Registry:
    """Collects tool functions: `tool = Registry()`, then `@tool("Title")` on each async function."""

    def __init__(self) -> None:
        self.tools: list[tuple[Callable[..., Any], str, bool]] = []

    def __call__(self, title: str, read_only: bool = True) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        def register(fn: Callable[..., Any]) -> Callable[..., Any]:
            self.tools.append((fn, title, read_only))
            return fn

        return register


def compact(fn: Callable[..., Any]) -> Callable[..., Any]:
    """Return the tool result as one compact JSON text block (the SDK default sends it twice, indented)."""

    @functools.wraps(fn)
    async def call(*args, **kwargs):
        return json.dumps(clean(await fn(*args, **kwargs), top=True), ensure_ascii=False, separators=(",", ":"))

    call.__doc__ = inspect.cleandoc(fn.__doc__ or "")
    return call


def _strip_titles(schema: Any) -> None:
    """Remove pydantic's auto-generated 'title' strings from an input schema."""
    if isinstance(schema, dict):
        if isinstance(schema.get("title"), str):
            del schema["title"]
        for x in schema.values():
            _strip_titles(x)
    elif isinstance(schema, list):
        for x in schema:
            _strip_titles(x)


def build_server(
    name: str,
    *,
    registry: Registry,
    title: str | None = None,
    instructions: str | None = None,
    version: str = "",
    website_url: str | None = None,
) -> MCPServer:
    mcp = MCPServer(name, title=title, instructions=instructions, version=version, website_url=website_url)
    # MCPServer turns on INFO logging; httpx would then log every URL, a query credential included (tests too).
    logging.getLogger("httpx").setLevel(logging.WARNING)
    for fn, tool_title, read_only in registry.tools:
        annotations = READ_ONLY if read_only else WRITE
        mcp.add_tool(compact(fn), title=tool_title, annotations=annotations, structured_output=False)
    # shortcut: reaches into the SDK's tool registry; mcp is pinned <3, recheck on upgrade.
    for t in mcp._tool_manager.list_tools():
        _strip_titles(t.parameters)
    return mcp


LOOPBACK = {"127.0.0.1", "localhost", "::1"}


def bearer_guard(app: Any, token: str) -> Any:
    """ASGI wrapper: HTTP requests need `Authorization: Bearer <token>`, else 401 (lifespan events pass through)."""
    want = f"Bearer {token}".encode()

    async def guarded(scope, receive, send):
        if scope["type"] == "http" and not hmac.compare_digest(dict(scope["headers"]).get(b"authorization", b""), want):
            await send({"type": "http.response.start", "status": 401, "headers": [(b"www-authenticate", b"Bearer")]})
            await send({"type": "http.response.body", "body": b"missing or wrong bearer token"})
            return
        await app(scope, receive, send)

    return guarded


def run(mcp: MCPServer, argv: list[str] | None = None) -> None:
    """Serve on stdio (default) or Streamable HTTP at http://HOST:PORT/mcp.

    Options: `--transport stdio|http`, `--host` (default 127.0.0.1), `--port` (default 8000); env `MCP_TRANSPORT`,
    `MCP_HOST`, `MCP_PORT` set the defaults. Unknown arguments are left alone. With env `MCP_AUTH_TOKEN` set, HTTP
    clients must send `Authorization: Bearer <token>`.
    """
    p = argparse.ArgumentParser(prog=mcp.name, add_help=False, allow_abbrev=False)
    p.add_argument("--transport", default=os.environ.get("MCP_TRANSPORT") or "stdio")
    p.add_argument("--host", default=os.environ.get("MCP_HOST") or "127.0.0.1")
    p.add_argument("--port", type=int, default=os.environ.get("MCP_PORT") or 8000)  # argparse converts str defaults
    args, _ = p.parse_known_args(sys.argv[1:] if argv is None else argv)
    if args.transport not in ("stdio", "http"):
        p.error(f"transport {args.transport!r}: use stdio or http")
    if args.transport == "stdio":
        mcp.run()
        return
    token = os.environ.get("MCP_AUTH_TOKEN", "")
    if token:
        import uvicorn  # a dependency of the mcp SDK's HTTP transport

        app = bearer_guard(mcp.streamable_http_app(host=args.host), token)
        uvicorn.run(app, host=args.host, port=args.port, log_level="warning")
        return
    if args.host not in LOOPBACK:
        print(
            f"{mcp.name}: warning: serving on {args.host}:{args.port} with no authentication; anyone who can reach "
            "it can call every tool (set MCP_AUTH_TOKEN to require a bearer token)",
            file=sys.stderr,
        )
    mcp.run("streamable-http", host=args.host, port=args.port)
