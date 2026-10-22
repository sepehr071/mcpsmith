"""Drift check: call every tool of a generated project live with its example arguments and report what came back."""

from __future__ import annotations

import importlib
import json
import logging
import sys
from dataclasses import dataclass, field
from pathlib import Path
from types import ModuleType
from typing import Any

import anyio
from mcp import Client

from .model import WRITE_METHODS, Plan, endpoint_method, load_plan


@dataclass
class CheckResult:
    tool: str
    ok: bool
    error: str | None
    chars: int
    empty_fields: list[str] = field(default_factory=list)
    skipped: bool = False
    reason: str = ""  # why it was skipped


def run(project: Path, writes: bool = False) -> list[CheckResult]:
    """Load `<project>/mcpsmith.yaml`, import `<package>.server` from `<project>/src` and call each tool once.

    Tools with read_only false (or a DELETE/PUT/PATCH endpoint written as 'METHOD /path') are skipped unless `writes`:
    they would change data on the live API. Tools whose required args have no example are skipped too.
    """
    project = Path(project)
    for name in ("httpx", "mcp"):  # the report already says what failed; per-request log lines are noise
        logging.getLogger(name).setLevel(logging.WARNING)
    plan = load_plan(project / "mcpsmith.yaml")
    server = _import_server(project / "src", plan.package)
    return anyio.run(_check, plan, server, writes)


def _import_server(src: Path, package: str) -> ModuleType:
    """Import a fresh copy of the project's server module, leaving sys.path and sys.modules as they were."""

    def drop() -> None:
        for name in [n for n in sys.modules if n == package or n.startswith(package + ".")]:
            del sys.modules[name]

    drop()
    sys.path.insert(0, str(src))
    try:
        return importlib.import_module(f"{package}.server")
    finally:
        sys.path.remove(str(src))
        drop()


async def _check(plan: Plan, server: ModuleType, writes: bool) -> list[CheckResult]:
    results = []
    async with Client(server.mcp) as client:
        for t in plan.tools:
            if (not t.read_only or endpoint_method(t.endpoint) in WRITE_METHODS) and not writes:
                reason = "changes data on the live API; add --writes to call it"
                results.append(CheckResult(t.name, True, None, 0, skipped=True, reason=reason))
                continue
            if missing := [a.name for a in t.args if a.required and a.name not in t.example]:
                reason = f"add example values for {missing} to mcpsmith.yaml and regenerate"  # as the generated tests
                results.append(CheckResult(t.name, True, None, 0, skipped=True, reason=reason))
                continue
            try:
                result = await client.call_tool(t.name, t.example)
            except Exception as e:  # one broken tool must not hide the report for the others
                results.append(CheckResult(t.name, False, f"{type(e).__name__}: {e}", 0))
                continue
            text = "".join(getattr(c, "text", "") for c in result.content)
            if result.is_error:
                results.append(CheckResult(t.name, False, text, len(text)))
                continue
            try:
                data = json.loads(text)
            except ValueError:
                data = None
            results.append(CheckResult(t.name, True, None, len(text), _empty_fields(data, list(t.output.fields))))
    return results


def _empty_fields(data: Any, fields: list[str]) -> list[str]:
    """Output fields that are missing or empty in every returned item (output is already stripped of empties)."""
    items = data["items"] if isinstance(data, dict) and isinstance(data.get("items"), list) else [data]
    return [f for f in fields if all(not isinstance(i, dict) or i.get(f) in (None, "", [], {}) for i in items)]
