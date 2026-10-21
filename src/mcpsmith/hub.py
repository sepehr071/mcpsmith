"""The live hub: validated plans become tools of the running MCP server, saved under <home>/connections/."""

from __future__ import annotations

import dataclasses
import datetime
import json
import re
import shutil
import tempfile
from pathlib import Path
from typing import Any

from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from . import browser, secrets
from .cli import EXPECTED, load_workdir_source, split_issues
from .design import validate
from .model import Plan, Source, load_plan, load_source
from .runtime.http import Api
from .runtime.interpret import endpoint_of, make_api, make_tool
from .runtime.server import READ_ONLY, WRITE, _strip_titles, compact

NAME = re.compile(r"[a-z0-9][a-z0-9_-]*")
MUTATING = re.compile(r"(?i)(save|delete|update|approve|create|submit|cancel|remove)")


@dataclasses.dataclass
class Connection:
    plan: Plan | None = None
    api: Api | None = None
    fns: dict[str, Any] = dataclasses.field(default_factory=dict)
    warning: str | None = None


class Hub:
    def __init__(self, mcp: MCPServer) -> None:
        self.mcp = mcp
        self._conns: dict[str, Connection] = {}

    @staticmethod
    def _dir(name: str) -> Path:
        return browser.home() / "connections" / name

    def _proxy(self) -> str | None:
        try:
            return browser.clean_proxy(
                json.loads((browser.home() / "browser.json").read_text(encoding="utf-8")).get("proxy")
            )
        except (OSError, ValueError):
            return None

    def _register(self, name: str, plan: Plan, source: Source | None, proxy: str | None) -> Connection:
        api = make_api(plan, source, proxy=proxy, secret=lambda env: secrets.get(name, env))
        conn = Connection(plan, api)
        for t in plan.tools:
            fn = make_tool(t, plan, source, api)
            self.mcp.add_tool(
                compact(fn), title=t.title, annotations=READ_ONLY if t.read_only else WRITE, structured_output=False
            )
            _strip_titles(self.mcp._tool_manager.get_tool(t.name).parameters)
            conn.fns[t.name] = fn
        self._conns[name] = conn
        return conn

    def connect(self, workdir: Path, name: str | None = None) -> dict:
        plan = load_plan(workdir / "plan.yaml")
        source = load_workdir_source(workdir)
        name = name or plan.name
        if not NAME.fullmatch(name):
            raise ValueError(f"connection name {name!r} must be lowercase letters, digits, '-' or '_'")
        errors, warnings = split_issues(validate(plan, source))
        if errors:
            raise ValueError("plan has errors:\n" + "\n".join(errors))
        own = set(self._conns[name].fns) if name in self._conns and self._conns[name].plan else set()
        taken = {t.name for t in self.mcp._tool_manager.list_tools()} - own
        if clash := sorted(taken & {t.name for t in plan.tools}):
            raise ValueError(f"tool name(s) already taken: {', '.join(clash)}; rename them in plan.yaml")
        for t in plan.tools:
            method, url = endpoint_of(t, plan, source)
            if method == "POST" and t.read_only and MUTATING.search(url):
                warnings.append(f"WARNING {t.name}: POST {url} looks like it changes data but is read_only")
        # workdir may be the saved dir itself: build the new dir from memory, then swap it in
        dest = self._dir(name)
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = Path(tempfile.mkdtemp(dir=dest.parent.parent))
        for f in ("plan.yaml", "source.json"):
            if (workdir / f).exists():
                (tmp / f).write_bytes((workdir / f).read_bytes())
        proxy = self._proxy()
        meta = {"proxy": proxy, "created": datetime.datetime.now(datetime.timezone.utc).isoformat()}
        (tmp / "meta.json").write_text(json.dumps(meta), encoding="utf-8", newline="\n")
        if name in self._conns:
            self.disconnect(name)
        shutil.rmtree(dest, ignore_errors=True)
        tmp.replace(dest)
        (workdir / "capture.har").unlink(missing_ok=True)
        self._register(name, plan, source, proxy)
        return {"connection": name, "tools": [t.name for t in plan.tools], "warnings": warnings}

    def disconnect(self, name: str) -> None:
        conn = self._conns.pop(name, None)
        if conn is None:
            raise ValueError(f"unknown connection {name!r}")
        for tool in conn.fns:
            self.mcp.remove_tool(tool)
        shutil.rmtree(self._dir(name), ignore_errors=True)

    def load_all(self) -> list[str]:
        """Re-register every saved connection; one that fails to load is reported, not fatal."""
        warnings = []
        for d in sorted(p for p in (browser.home() / "connections").glob("*") if p.is_dir()):
            try:
                source = load_source(d / "source.json") if (d / "source.json").exists() else None
                meta = json.loads((d / "meta.json").read_text(encoding="utf-8"))
                self._register(d.name, load_plan(d / "plan.yaml"), source, meta.get("proxy"))
            except (*EXPECTED, KeyError, TypeError) as e:
                self._conns[d.name] = Connection(warning=f"{type(e).__name__}: {e}")
                warnings.append(f"connection {d.name} not loaded: {e}")
        return warnings

    def connections(self) -> list[dict]:
        return [
            {
                "name": n,
                "tools": list(c.fns),
                "auth": c.plan.auth.type if c.plan and c.plan.auth else None,
                "ok": c.plan is not None,
                "warning": c.warning,
            }
            for n, c in self._conns.items()
        ]

    def _conn(self, name: str) -> Connection:
        if (c := self._conns.get(name)) is None or c.plan is None:
            raise ToolError(f"unknown connection {name!r}; see smith_connections")
        return c

    def tools(self, name: str) -> list[dict]:
        c = self._conn(name)
        return [
            {
                "name": t.name,
                "description": t.description,
                "input_schema": self.mcp._tool_manager.get_tool(t.name).parameters,
                "read_only": t.read_only,
            }
            for t in c.plan.tools
        ]

    async def call(self, name: str, tool: str, args: dict[str, Any]) -> Any:
        c = self._conn(name)
        spec = next((t for t in c.plan.tools if t.name == tool), None)
        if spec is None:
            raise ToolError(f"unknown tool {tool!r} in {name}; see smith_tools")
        if not spec.read_only:
            raise ToolError(f"{tool} changes data: call it as its own tool after the user confirms.")
        try:
            return await c.fns[tool](**args)
        except TypeError as e:
            raise ToolError(f"bad arguments for {tool}: {e}") from e
