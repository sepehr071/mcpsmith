"""Tool registry and MCPServer construction for generated servers."""

from __future__ import annotations

import functools
import inspect
import json
import logging
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
    for fn, tool_title, read_only in registry.tools:
        annotations = READ_ONLY if read_only else WRITE
        mcp.add_tool(compact(fn), title=tool_title, annotations=annotations, structured_output=False)
    # shortcut: reaches into the SDK's tool registry; mcp is pinned <3, recheck on upgrade.
    for t in mcp._tool_manager.list_tools():
        _strip_titles(t.parameters)
    return mcp


def run(mcp: MCPServer) -> None:
    logging.getLogger("httpx").setLevel(logging.WARNING)  # one INFO line per request floods client logs (and URLs)
    mcp.run()
