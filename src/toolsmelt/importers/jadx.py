"""Decompiled classes of an APK, from the jadx CLI or REA's bundled JADX MCP server (a jar speaking MCP over stdio).

Order: an explicit jar, else `jadx` on PATH (decompiled once per APK into a temp cache, then read as files), else
REA_JADX_MCP_JAR (`npx -y rea-agents setup` installs it). The decompiler and a Java runtime belong to the machine:
toolsmelt never ships one, downloads an app, or runs anything from the APK's directory.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path

import anyio
from mcp import ClientSession, StdioServerParameters, stdio_client

from toolsmelt.importers import android

Call = Callable[[str, dict], Awaitable[str]]

CLASS_NAME = re.compile(r"[A-Za-z_$][\w$]*(?:\.[\w$]+)*")
API_STRINGS = ('"api/', '"/api/', '"v1/', '"/v1/')  # code that holds one of these declares or builds API paths
BUILDER_MARK = "HTTP method annotation is required"  # Retrofit's request builder, the class that names the annotations
PAGE = 200
MAX_SCAN = 1_000_000  # classes scanned per search; the jar's own default stops at 5000
MISSING = (
    "run `npx -y rea-agents setup` (it installs the JADX MCP jar and sets REA_JADX_MCP_JAR) or pass --jadx-jar PATH"
)
INSTALL = (
    "install jadx so `jadx` is on PATH (free: https://github.com/skylot/jadx/releases, or `scoop install extras/jadx`), "
    f"or {MISSING}"
)
CACHE = Path(tempfile.gettempdir()) / "toolsmelt-jadx"  # the jadx CLI's output, one directory per APK sha256


def _jadx_cli() -> str | None:
    names = ("jadx", "jadx.bat", "jadx.cmd") if sys.platform == "win32" else ("jadx",)
    return next((path for name in names if (path := shutil.which(name))), None)


def _tree(apk: Path, jar: str | None) -> Path | None:
    """The jadx CLI's decompiled sources of `apk` (decompiled once per APK content), or None to use the jar."""
    exe = None if jar else _jadx_cli()
    if not exe:
        return None
    if not apk.is_file():
        raise ValueError(f"{apk}: not a file")
    sha = hashlib.sha256()
    with apk.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            sha.update(chunk)
    out = CACHE / sha.hexdigest()
    if (out / "sources").is_dir():
        return out / "sources"
    CACHE.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp(dir=CACHE))  # new and empty; becomes `out` only once jadx has finished
    # jadx.bat runs through cmd.exe, where an `&` in a path is a command separator: pass only names we made,
    # relative to the cache directory (an APK copy named by its hash, the temp output directory).
    copy = CACHE / f"{sha.hexdigest()}.apk"
    try:
        shutil.copyfile(apk, copy)
        args = [exe, "-d", tmp.name, "--show-bad-code", "--no-res", copy.name]
        proc = subprocess.run(args, cwd=CACHE, capture_output=True, text=True, errors="replace")
    finally:
        with contextlib.suppress(OSError):
            copy.unlink()
    # jadx exits non-zero when some classes fail to decompile; the rest are still usable
    if not (tmp / "sources").is_dir() or not any((tmp / "sources").rglob("*.java")):
        shutil.rmtree(tmp, ignore_errors=True)
        raise RuntimeError(f"jadx produced no classes (exit {proc.returncode}): {(proc.stderr or proc.stdout)[-300:]}")
    try:
        tmp.rename(out)
    except OSError:  # a concurrent run finished first
        if not (out / "sources").is_dir():
            raise
        shutil.rmtree(tmp, ignore_errors=True)
    return out / "sources"


def _classes(tree: Path) -> dict[str, Path]:
    return {".".join(p.relative_to(tree).with_suffix("").parts): p for p in tree.rglob("*.java")}


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


def _jar(jar: str | None) -> str:
    path = jar or os.environ.get("REA_JADX_MCP_JAR") or ""
    if not path or not Path(path).is_file():
        raise RuntimeError(f"no decompiler: JADX MCP jar not found ({path or 'REA_JADX_MCP_JAR unset'}): {INSTALL}")
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
    """Classes whose code holds a path-like string such as "api/..." or "/v1/..." (candidates for the HTTP interface).

    None found is an error that says what to do instead, naming a known request-signing SDK when the app has one.
    """
    return api_classes_and_signer(apk, jar)[0]


def api_classes_and_signer(apk: Path, jar: str | None = None) -> tuple[list[str], tuple[str, str] | None]:
    """find_api_classes, plus (app, provider) of a known request-signing SDK in the app, else None.

    A seconds-long triage of the APK runs first: an app built with Flutter or React Native, or one without Retrofit,
    gets its advice without the minutes-long decompile.
    """
    t = android.triage(apk)
    if t.framework:
        raise RuntimeError(android.framework_advice(t.framework, t.signer))
    if t.retrofit is False:
        raise RuntimeError(android.dead_end(t.signer))
    signer = t.signer
    if (tree := _tree(apk, jar)) is not None:
        classes = _classes(tree)
        found = [n for n, p in classes.items() if any(term in _read(p) for term in API_STRINGS)]
        signer = signer or android.known_signer(classes)
    else:

        async def work(call: Call) -> list[str]:
            return [n for term in API_STRINGS for n in await _search(call, term)]

        found = _run(apk, jar, work)
    if not found:
        raise RuntimeError(android.dead_end(signer))
    return sorted(dict.fromkeys(found)), signer


def class_source(apk: Path, class_name: str, jar: str | None = None) -> str:
    """The decompiled Java of one class."""
    if not CLASS_NAME.fullmatch(class_name):
        raise ValueError(f"{class_name!r} is not a class name like com.example.api.Service")
    if (tree := _tree(apk, jar)) is not None:
        *package, name = class_name.split("$")[0].split(".")  # an inner class lives in its outer class's file
        path = tree.joinpath(*package, name + ".java")
        if not path.is_file():
            raise RuntimeError(f"jadx: no class {class_name} in {apk.name}")
        return _read(path)

    async def work(call: Call) -> str:
        return await call("get_class_source", {"class_name": class_name})

    return _run(apk, jar, work)


def builder_annotations(apk: Path, jar: str | None = None) -> dict[str, str]:
    """Minified annotation name -> Retrofit meaning, from the app's Retrofit request builder ({} if it has none)."""
    if (tree := _tree(apk, jar)) is not None:
        mapping: dict[str, str] = {}
        for path in _classes(tree).values():
            if BUILDER_MARK in (java := _read(path)):
                mapping |= android.resolve_annotations(java)
        return mapping

    async def work(call: Call) -> dict[str, str]:
        mapping: dict[str, str] = {}
        for cls in await _search(call, BUILDER_MARK):
            mapping |= android.resolve_annotations(await call("get_class_source", {"class_name": cls}))
        return mapping

    return _run(apk, jar, work)
