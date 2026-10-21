"""Decompiled classes of an APK from REA's bundled JADX MCP server (a jar speaking MCP over stdio).

The jar and a Java runtime belong to REA and the machine: `npx -y rea-agents setup` installs the jar and sets
REA_JADX_MCP_JAR. mcpsmith never ships a decompiler, downloads an app, or runs anything from the APK's directory.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import shutil
import sys
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path

import anyio
from mcp import ClientSession, StdioServerParameters, stdio_client

from mcpsmith.importers import android

Call = Callable[[str, dict], Awaitable[str]]

CLASS_NAME = re.compile(r"[A-Za-z_$][\w$]*(?:\.[\w$]+)*")
API_STRINGS = ('"api/', '"/api/', '"v1/', '"/v1/')  # code that holds one of these declares or builds API paths
BUILDER_MARK = "HTTP method annotation is required"  # Retrofit's request builder, the class that names the annotations
PAGE = 200
MAX_SCAN = 1_000_000  # classes scanned per search; the jar's own default stops at 5000
MISSING = (
    "run `npx -y rea-agents setup` (it installs the JADX MCP jar and sets REA_JADX_MCP_JAR) or pass --jadx-jar PATH"
)


def _jar(jar: str | None) -> str:
    path = jar or os.environ.get("REA_JADX_MCP_JAR") or ""
    if not path or not Path(path).is_file():
        raise RuntimeError(f"JADX MCP jar not found ({path or 'REA_JADX_MCP_JAR unset'}): {MISSING}")
    return path


def _java() -> str:
    home = os.environ.get("JAVA_HOME")
    exe = "java.exe" if sys.platform == "win32" else "java"
    found = str(Path(home) / "bin" / exe) if home and (Path(home) / "bin" / exe).is_file() else shutil.which("java")
    if not found:
        raise RuntimeError("Java not found: install a JDK (21 or newer) or set JAVA_HOME")
    return found


@contextlib.asynccontextmanager
async def _session(apk: Path, jar: str | None) -> AsyncIterator[Call]:
    if not apk.is_file():
        raise ValueError(f"{apk}: not a file")
    params = StdioServerParameters(command=_java(), args=["-jar", _jar(jar), "--apk", str(apk.resolve())])
    with open(os.devnull, "w") as quiet:  # the jar logs a lot to stderr
        async with stdio_client(params, errlog=quiet) as (read, write), ClientSession(read, write) as session:
            await session.initialize()

            async def call(tool: str, args: dict) -> str:
                result = await session.call_tool(tool, args)
                text = "".join(getattr(c, "text", "") for c in result.content)
                if result.is_error:
                    raise RuntimeError(f"JADX {tool}: {text[:300]}")
                return text

            yield call


def _run(apk: Path, jar: str | None, work: Callable[[Call], Awaitable]):
    async def main():
        async with _session(apk, jar) as call:
            return await work(call)

    try:
        return anyio.run(main)
    except (RuntimeError, ValueError):
        raise
    except Exception as e:  # anyio wraps failures of the child process in exception groups
        raise RuntimeError(f"JADX failed ({type(e).__name__}: {e}). Is the jar current? {MISSING}") from e


async def _search(call: Call, term: str, scope: str = "code") -> list[str]:
    """Names of all classes whose code contains `term`: one scan of every class, then pages of results."""
    names: list[str] = []
    while True:
        out = json.loads(
            await call(
                "search_classes_by_keyword",
                {"search_term": term, "search_in": scope, "max_scan": MAX_SCAN, "count": PAGE, "offset": len(names)},
            )
        )
        items = out.get("items") or []
        names += [i["fqn"] for i in items]
        if out.get("max_scan_reached"):
            raise RuntimeError(f"JADX stopped after {out.get('scanned')} classes; the app is larger than {MAX_SCAN}")
        if len(items) < PAGE:
            return names


def find_api_classes(apk: Path, jar: str | None = None) -> list[str]:
    """Classes whose code holds a path-like string such as "api/..." or "/v1/..." (candidates for the HTTP interface)."""

    async def work(call: Call) -> list[str]:
        found = [n for term in API_STRINGS for n in await _search(call, term)]
        return sorted(dict.fromkeys(found))

    return _run(apk, jar, work)


def class_source(apk: Path, class_name: str, jar: str | None = None) -> str:
    """The decompiled Java of one class."""
    if not CLASS_NAME.fullmatch(class_name):
        raise ValueError(f"{class_name!r} is not a class name like com.example.api.Service")

    async def work(call: Call) -> str:
        return await call("get_class_source", {"class_name": class_name})

    return _run(apk, jar, work)


def builder_annotations(apk: Path, jar: str | None = None) -> dict[str, str]:
    """Minified annotation name -> Retrofit meaning, from the app's Retrofit request builder ({} if it has none)."""

    async def work(call: Call) -> dict[str, str]:
        mapping: dict[str, str] = {}
        for cls in await _search(call, BUILDER_MARK):
            mapping |= android.resolve_annotations(await call("get_class_source", {"class_name": cls}))
        return mapping

    return _run(apk, jar, work)
