"""`mcpsmith setup`: register mcpsmith, Chrome DevTools MCP and REA with coding agents, and install the skills."""

import os
import shutil
import subprocess
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from mcpsmith.skills_path import skills_dir

from .agents import AGENTS, proxy_env, server_entry

REA = "rea-agents@6.7.0"
NAMES = ("mcpsmith", "mcpsmith-chrome")  # entries we own; every other entry is never touched
NO_NODE = "npx not found (install Node.js)"


@dataclass(frozen=True)
class Change:
    agent: str
    kind: str  # server | skill | rea
    target: str  # config path, skills root or command line
    action: str  # add | update | remove | skip
    reason: str = ""
    name: str = ""
    entry: dict | None = None
    cmd: tuple[str, ...] = ()


def _skill_names() -> list[str]:
    return sorted(p.name for p in skills_dir().iterdir() if (p / "SKILL.md").is_file())


def _same(src: Path, dst: Path) -> bool:
    return all(
        (dst / f.relative_to(src)).is_file() and (dst / f.relative_to(src)).read_bytes() == f.read_bytes()
        for f in src.rglob("*")
        if f.is_file()
    )


def _servers(agent, doc) -> Mapping:
    servers = doc.get("mcp_servers" if agent.toml else "mcpServers", {})
    if not isinstance(servers, Mapping):
        raise ValueError("MCP servers entry is not a table/object")
    return servers


def _server_changes(agent, home, environ, remove, has_npx, windows) -> list[Change]:
    path = agent.config_path(home, environ)
    servers = _servers(agent, agent.load(path))
    out = []
    for name in NAMES:
        entry = server_entry(name, windows, proxy_env(environ))
        existing = servers.get(name)

        def make(action, reason, name=name, entry=entry):
            return Change(agent.id, "server", str(path), action, reason, name, entry)

        if remove:
            if existing is None:
                out.append(make("skip", "not registered"))
            else:
                out.append(make("remove", "registered by mcpsmith"))
        elif name == "mcpsmith-chrome" and not has_npx:
            out.append(make("skip", NO_NODE))
        elif existing is None:
            out.append(make("add", ""))
        else:
            scratch = agent.add(agent.load(Path("/nonexistent/x")), name, entry)
            same = _servers(agent, scratch)[name] == existing
            out.append(make("skip", "up to date") if same else make("update", "entry differs"))
    return out


def plan(home, environ, agents, rea, skills, remove, which=shutil.which, windows=None) -> list[Change]:
    windows = os.name == "nt" if windows is None else windows
    npx = which("npx")
    changes, roots = [], {}
    for aid in agents:
        agent = AGENTS[aid]
        try:
            server_changes = _server_changes(agent, home, environ, remove, bool(npx), windows)
        except (ValueError, TypeError, AttributeError) as e:
            changes.append(Change(aid, "server", str(agent.config_path(home, environ)), "skip", f"skipped: {e}"))
            continue
        changes += server_changes
        roots.setdefault(agent.skills_root(home), []).append(aid)
        if rea:
            cmd = ["npx", "-y", REA, "uninstall" if remove else "setup", "--client", agent.rea_client, "--yes"]
            if windows:
                cmd = ["cmd", "/d", "/c", *cmd]
            if not npx:
                changes.append(Change(aid, "rea", " ".join(cmd), "skip", NO_NODE, cmd=tuple(cmd)))
            else:
                known = any(c.action in ("skip", "update") and c.name == "mcpsmith" for c in server_changes)
                action = "remove" if remove else "update" if known else "add"
                changes.append(Change(aid, "rea", " ".join(cmd), action, "delegated to rea-agents", cmd=tuple(cmd)))
    if skills:
        for root, ids in roots.items():
            present = [n for n in _skill_names() if (root / n).exists()]
            if remove:
                action = "remove" if present else "skip"
            elif not present:
                action = "add"
            else:
                done = len(present) == len(_skill_names()) and all(_same(skills_dir() / n, root / n) for n in present)
                action = "skip" if done else "update"
            reason = "up to date" if action == "skip" and not remove else "not installed" if action == "skip" else ""
            changes.append(Change(",".join(ids), "skill", str(root), action, reason))
    return changes


def _write_server(change: Change) -> str:
    agent, path = AGENTS[change.agent], Path(change.target)
    backup = path.with_name(path.name + ".mcpsmith.bak")
    original = path.read_bytes() if path.exists() else None
    if original is not None and not backup.exists():
        shutil.copy2(path, backup)
    try:
        doc = agent.load(path)
        if change.action == "remove":
            agent.remove(doc, change.name)
        else:
            agent.add(doc, change.name, change.entry)
        text = agent.dump(doc)
        if agent.toml and change.action == "remove":
            text = text.rstrip("\n") + "\n"  # drop the blank line tomlkit put before our table
        if original and b"\r\n" in original:
            text = text.replace("\n", "\r\n")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(text.encode("utf-8"))
        check = agent.load(path)
        if agent.has(check, change.name) != (change.action != "remove"):
            raise ValueError("entry not as expected after writing")
    except (OSError, ValueError, TypeError, AttributeError) as e:
        if original is None:
            path.unlink(missing_ok=True)
        else:
            path.write_bytes(original)
        return f"FAILED {change.agent} {change.name}: {e}; restored {path}"
    return f"{change.action} {change.agent} {change.name} in {path}"


def apply(changes, runner=subprocess.run) -> list[str]:
    lines = []
    for c in changes:
        if c.action == "skip":
            lines.append(f"skip {c.agent} {c.kind} {c.name}: {c.reason}".replace("  ", " "))
        elif c.kind == "server":
            lines.append(_write_server(c))
        elif c.kind == "skill":
            root = Path(c.target)
            for n in _skill_names():
                if c.action == "remove":
                    shutil.rmtree(root / n, ignore_errors=True)
                else:
                    shutil.copytree(skills_dir() / n, root / n, dirs_exist_ok=True)
            lines.append(f"{c.action} skills in {root}")
        else:
            try:
                r = runner(list(c.cmd), capture_output=True, text=True, check=False)
                ok, tail = r.returncode == 0, (r.stderr or r.stdout or "").strip()[-300:]
            except OSError as e:
                ok, tail = False, str(e)
            lines.append(f"{c.action} {c.agent} rea" if ok else f"FAILED {c.agent} rea: {tail}")
    return lines


def cmd(args, home=None, environ=None, which=shutil.which, runner=subprocess.run, stdin=None) -> int:
    home, environ, stdin = home or Path.home(), os.environ if environ is None else environ, stdin or sys.stdin
    if args.agents:
        ids = [a.strip() for a in args.agents.split(",") if a.strip()]
        if bad := [a for a in ids if a not in AGENTS]:
            raise ValueError(f"unknown agent {', '.join(bad)} (choose from {', '.join(AGENTS)})")
    else:
        ids = [a for a, ag in AGENTS.items() if ag.detected(home, environ, which)]
    if not ids:
        print("no coding agents found; pass --agents claude,codex,cursor,gemini to choose")
        return 0
    changes = plan(home, environ, ids, not args.no_rea, not args.no_skills, args.remove, which)
    for c in changes:
        print(f"{c.action:<7} {c.agent:<9} {c.kind:<7} {c.name + ' ' if c.name else ''}{c.target}  {c.reason}".rstrip())
    if args.dry_run:
        print("dry run: nothing written")
        return 0
    if not args.yes:
        if not stdin.isatty():
            raise ValueError("stdin is not interactive; pass --yes to apply without asking")
        if input("Apply these changes? [y/N] ").strip().lower() not in ("y", "yes"):
            print("cancelled")
            return 0
    lines = apply(changes, runner)
    print("\n".join(lines))
    print("Restart your agents. Run `mcpsmith browser` before using Chrome DevTools.")
    return 1 if any(line.startswith("FAILED") for line in lines) else 0
