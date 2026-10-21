"""Where each coding agent keeps its MCP servers and skills, and how to add/remove ours without touching the rest."""

import json
import shutil
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import tomlkit

from mcpsmith import __version__
from mcpsmith.browser import clean_proxy

SERVERS = {
    "mcpsmith": ["uvx", f"mcpsmith@{__version__}", "mcp"],
    "chrome-devtools": ["npx", "-y", "chrome-devtools-mcp@1.11.0", "--browser-url=http://127.0.0.1:9222"],
}


def proxy_env(environ) -> dict[str, str]:
    """Proxy variables worth passing to the mcpsmith server: userinfo stripped, nothing else copied."""
    env = {}
    for key in ("HTTPS_PROXY", "HTTP_PROXY"):
        value = clean_proxy(environ.get(key) or environ.get(key.lower()))
        if value:
            env[key] = value
    if no_proxy := environ.get("NO_PROXY") or environ.get("no_proxy"):
        env["NO_PROXY"] = no_proxy
    return env


def server_entry(name: str, windows: bool, env: dict[str, str]) -> dict:
    command, *args = SERVERS[name]
    if windows and command == "npx":
        command, args = "cmd", ["/d", "/c", "npx", *args]
    return {"command": command, "args": args, "env": dict(env) if name == "mcpsmith" else {}}


@dataclass(frozen=True)
class Agent:
    id: str
    rea_client: str
    toml: bool
    config: Callable[[Path, dict], Path]
    skills: Callable[[Path], Path]
    config_dir: Callable[[Path, dict], Path]
    clis: tuple[str, ...]

    def config_path(self, home: Path, environ) -> Path:
        return self.config(home, environ)

    def skills_root(self, home: Path) -> Path:
        return self.skills(home)

    def detected(self, home: Path, environ, which=shutil.which) -> bool:
        return self.config_dir(home, environ).is_dir() or any(which(cli) for cli in self.clis)

    def load(self, path: Path):
        text = path.read_text(encoding="utf-8") if path.exists() else ""
        try:
            if self.toml:
                return tomlkit.parse(text)
            doc = json.loads(text) if text.strip() else {}
        except Exception as e:  # tomlkit and json raise different parse errors
            raise ValueError(f"{path}: not valid {'TOML' if self.toml else 'JSON'} ({e})") from e
        if not isinstance(doc, dict):
            raise ValueError(f"{path}: expected an object at the top level")
        return doc

    def dump(self, doc) -> str:
        if self.toml:
            return tomlkit.dumps(doc)
        return json.dumps(doc, indent=2, ensure_ascii=False) + "\n"

    def _key(self) -> str:
        return "mcp_servers" if self.toml else "mcpServers"

    def has(self, doc, name: str) -> bool:
        return name in doc.get(self._key(), {})

    def add(self, doc, name: str, entry: dict):
        if self.toml:
            table = tomlkit.table()
            table["command"] = entry["command"]
            table["args"] = entry["args"]
            table["startup_timeout_sec"] = 30
            if entry["env"]:
                table["env"] = tomlkit.item(entry["env"])
            if "mcp_servers" not in doc:
                doc["mcp_servers"] = tomlkit.table(is_super_table=True)
            doc["mcp_servers"][name] = table
        else:
            entry = {**entry, "timeout": 30000} if self.id == "gemini" else {"type": "stdio", **entry}
            doc.setdefault("mcpServers", {})[name] = entry
        return doc

    def remove(self, doc, name: str):
        if self.has(doc, name):
            del doc[self._key()][name]
        return doc


def _claude_dir(home, environ):
    return Path(environ.get("CLAUDE_CONFIG_DIR") or home / ".claude")


def _codex_home(home, environ):
    return Path(environ.get("CODEX_HOME") or home / ".codex")


def _gemini_dir(home, environ):
    return Path(environ.get("GEMINI_CLI_HOME") or home) / ".gemini"


def _agent_skills(home):
    return home / ".agents" / "skills"


AGENTS = {
    "claude": Agent(
        "claude",
        "claude_code",
        False,
        lambda home, env: (
            Path(env["CLAUDE_CONFIG_DIR"]) / ".claude.json" if env.get("CLAUDE_CONFIG_DIR") else home / ".claude.json"
        ),
        lambda home: home / ".claude" / "skills",
        _claude_dir,
        ("claude",),
    ),
    "codex": Agent(
        "codex",
        "codex",
        True,
        lambda home, env: _codex_home(home, env) / "config.toml",
        _agent_skills,
        _codex_home,
        ("codex",),
    ),
    "cursor": Agent(
        "cursor",
        "cursor",
        False,
        lambda home, env: home / ".cursor" / "mcp.json",
        _agent_skills,
        lambda home, env: home / ".cursor",
        ("cursor-agent", "cursor"),
    ),
    "gemini": Agent(
        "gemini",
        "gemini_cli",
        False,
        lambda home, env: _gemini_dir(home, env) / "settings.json",
        _agent_skills,
        _gemini_dir,
        ("gemini",),
    ),
}
