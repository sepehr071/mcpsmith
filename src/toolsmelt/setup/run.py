"""`toolsmelt setup`: register toolsmelt, Chrome DevTools MCP (and REA on request) with coding agents, and install the
skills. `toolsmelt install`: register a generated server project the same way."""

import os
import shutil
import subprocess
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from toolsmelt.model import load_plan, project_plan
from toolsmelt.skills_path import skills_dir

from .agents import AGENTS, proxy_env, server_entry

REA = "rea-agents@6.7.0"
NAMES = ("toolsmelt", "toolsmelt-chrome")  # entries we own; every other entry is never touched
LEGACY = (
    "mcpsmith",
    "mcpsmith-chrome",
)  # the same entries under the old name: setup replaces them, --remove removes them
MARKER = ".toolsmelt"  # file in each skill folder we install; folders without it are the user's
LEGACY_MARKER = ".mcpsmith"  # skill folders installed before the rename
REA_TIMEOUT = 300
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
                out.append(make("remove", "registered by toolsmelt"))
        elif name == "toolsmelt-chrome" and not has_npx:
            out.append(make("skip", NO_NODE))
        elif existing is None:
            out.append(make("add", ""))
        else:
            scratch = agent.add(agent.load(Path("/nonexistent/x")), name, entry)
            same = _servers(agent, scratch)[name] == existing
            out.append(make("skip", "up to date") if same else make("update", "entry differs"))
    for name in LEGACY:
        if name in servers:
            out.append(Change(agent.id, "server", str(path), "remove", "old name: mcpsmith is now toolsmelt", name))
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
        roots.setdefault(agent.skills_root(home, environ), []).append(aid)
        if rea:
            cmd = ["npx", "-y", REA, "uninstall" if remove else "setup", "--client", agent.rea_client, "--yes"]
            if remove:  # the user may have had REA before setup, so never uninstall it for them
                changes.append(
                    Change(aid, "rea", " ".join(cmd), "skip", f"REA left installed; remove it with: {' '.join(cmd)}")
                )
                continue
            if windows:
                cmd = ["cmd", "/d", "/c", *cmd]
            if not npx:
                changes.append(Change(aid, "rea", " ".join(cmd), "skip", NO_NODE, cmd=tuple(cmd)))
            else:
                known = any(c.action in ("skip", "update") and c.name == "toolsmelt" for c in server_changes)
                action = "update" if known else "add"
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


def _atomic_write(path: Path, data: bytes) -> None:
    target = path.resolve()  # follow a symlinked dotfile so the link survives
    tmp = target.with_name(target.name + ".toolsmelt.tmp")
    try:
        tmp.write_bytes(data)
        os.replace(tmp, target)
    finally:
        tmp.unlink(missing_ok=True)


def _write_server(change: Change) -> str:
    agent, path = AGENTS[change.agent], Path(change.target)
    backup = path.with_name(path.name + ".toolsmelt.bak")
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
        _atomic_write(path, text.encode("utf-8"))
        check = agent.load(path)
        if agent.has(check, change.name) != (change.action != "remove"):
            raise ValueError("entry not as expected after writing")
    except (OSError, ValueError, TypeError, AttributeError) as e:
        try:
            if original is None:
                path.unlink(missing_ok=True)
            else:
                _atomic_write(path, original)
        except OSError as e2:
            e = f"{e}; restore failed: {e2}"
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
            done = []
            for n in _skill_names():
                dest = root / n
                if not dest.exists():
                    if c.action == "remove":
                        continue
                elif not any((dest / m).is_file() for m in (MARKER, LEGACY_MARKER)):
                    lines.append(f"skip {n} in {root}: exists and is not toolsmelt's")
                    continue
                try:
                    if c.action == "remove":
                        shutil.rmtree(dest)
                    else:
                        shutil.copytree(skills_dir() / n, dest, dirs_exist_ok=True)
                        (dest / MARKER).write_text("installed by toolsmelt setup\n", encoding="utf-8")
                        (dest / LEGACY_MARKER).unlink(missing_ok=True)
                    done.append(n)
                except OSError as e:
                    lines.append(f"FAILED skill {n} in {root}: {e}")
            if done:
                lines.append(f"{c.action} skills in {root}: {', '.join(done)}")
        else:
            try:
                r = runner(list(c.cmd), capture_output=True, text=True, check=False, timeout=REA_TIMEOUT)
                ok, tail = r.returncode == 0, (r.stderr or r.stdout or "").strip()[-300:]
            except subprocess.TimeoutExpired:
                ok, tail = False, f"timed out after {REA_TIMEOUT}s"
            except OSError as e:
                ok, tail = False, str(e)
            lines.append(f"{c.action} {c.agent} rea" if ok else f"FAILED {c.agent} rea: {tail}")
    return lines


def project_entry(project, pypi: bool = False) -> tuple[str, dict]:
    """The server name and agent-config entry for a project written by `toolsmelt generate`."""
    project = Path(project).resolve()
    p = load_plan(project_plan(project))
    if p.name in NAMES:
        raise ValueError(f"{p.name!r} is reserved for toolsmelt's own entries; rename the plan")
    if pypi:
        return p.name, {"command": "uvx", "args": [f"{p.name}@{p.version}"], "env": {}}
    return p.name, {"command": "uv", "args": ["run", "--directory", str(project), p.name], "env": {}}


def install_changes(project, home, environ, agents, remove=False, force=False, pypi=False) -> list[Change]:
    """Changes that add (or remove) one generated project's entry; an entry of that name pointing elsewhere is a
    conflict (skipped, reason starts with CONFLICT) unless force."""
    name, entry = project_entry(project, pypi)
    out = []
    for aid in agents:
        agent = AGENTS[aid]
        path = agent.config_path(home, environ)

        def make(action, reason, aid=aid, path=path):
            return Change(aid, "server", str(path), action, reason, name, entry)

        try:
            existing = _servers(agent, agent.load(path)).get(name)
        except (ValueError, TypeError, AttributeError) as e:
            out.append(Change(aid, "server", str(path), "skip", f"skipped: {e}"))
            continue
        ours = isinstance(existing, Mapping) and (existing.get("command"), list(existing.get("args") or [])) == (
            entry["command"],
            entry["args"],
        )
        if existing is None:
            out.append(make("skip", "not registered") if remove else make("add", ""))
        elif not ours and not force:
            out.append(make("skip", f"CONFLICT: an entry named {name} runs something else; pass --force to replace"))
        elif remove:
            out.append(make("remove", ""))
        elif ours:
            scratch = agent.add(agent.load(Path("/nonexistent/x")), name, entry)
            same = _servers(agent, scratch)[name] == existing
            out.append(make("skip", "up to date") if same else make("update", "entry differs"))
        else:
            out.append(make("update", "replaced (--force)"))
    return out


def _agent_ids(args, home, environ, which) -> list[str]:
    if args.agents:
        ids = [a.strip() for a in args.agents.split(",") if a.strip()]
        if bad := [a for a in ids if a not in AGENTS]:
            raise ValueError(f"unknown agent {', '.join(bad)} (choose from {', '.join(AGENTS)})")
        return ids
    return [a for a, ag in AGENTS.items() if ag.detected(home, environ, which)]


def _show_and_confirm(args, changes, stdin) -> bool:
    for c in changes:
        print(f"{c.action:<7} {c.agent:<9} {c.kind:<7} {c.name + ' ' if c.name else ''}{c.target}  {c.reason}".rstrip())
    if args.dry_run:
        print("dry run: nothing written")
        return False
    if not args.yes:
        if not stdin.isatty():
            raise ValueError("stdin is not interactive; pass --yes to apply without asking")
        if input("Apply these changes? [y/N] ").strip().lower() not in ("y", "yes"):
            print("cancelled")
            return False
    return True


def cmd(args, home=None, environ=None, which=shutil.which, runner=subprocess.run, stdin=None) -> int:
    home, environ, stdin = home or Path.home(), os.environ if environ is None else environ, stdin or sys.stdin
    ids = _agent_ids(args, home, environ, which)
    if not ids:
        print("no coding agents found; pass --agents claude,codex,cursor,gemini to choose")
        return 0
    changes = plan(home, environ, ids, args.rea, not args.no_skills, args.remove, which)
    if not _show_and_confirm(args, changes, stdin):
        return 0
    lines = apply(changes, runner)
    print("\n".join(lines))
    print("Restart your agents. Run `toolsmelt browser` before using Chrome DevTools.")
    return 1 if any(line.startswith("FAILED") for line in lines) else 0


def install_cmd(args, home=None, environ=None, which=shutil.which, stdin=None) -> int:
    home, environ, stdin = home or Path.home(), os.environ if environ is None else environ, stdin or sys.stdin
    ids = _agent_ids(args, home, environ, which)
    if not ids:
        print("no coding agents found; pass --agents claude,codex,cursor,gemini to choose")
        return 0
    changes = install_changes(args.project, home, environ, ids, args.remove, args.force, args.pypi)
    conflict = any(c.reason.startswith("CONFLICT") for c in changes)
    if not _show_and_confirm(args, changes, stdin):
        return 1 if conflict else 0
    lines = apply(changes)
    print("\n".join(lines))
    print("Restart your agents to load the server.")
    return 1 if conflict or any(line.startswith("FAILED") for line in lines) else 0
