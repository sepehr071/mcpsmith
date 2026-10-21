import subprocess
import zipfile
from pathlib import Path

from mcpsmith.skills_path import skills_dir

SKILLS = ["build-mcp-server", "design-mcp-tools", "extract-api"]


def test_skills_dir_has_all_skills():
    for name in SKILLS:
        text = (skills_dir() / name / "SKILL.md").read_text(encoding="utf-8")
        assert text.split("---")[1].count(f"name: {name}\n") == 1


def test_wheel_contains_skills(tmp_path):
    subprocess.run(["uv", "build", "--wheel", "-o", str(tmp_path)], check=True, capture_output=True)
    wheel = next(tmp_path.glob("*.whl"))
    names = zipfile.ZipFile(wheel).namelist()
    for name in SKILLS:
        assert f"mcpsmith/skills/{name}/SKILL.md" in names


# --- agent targets ---

import pytest  # noqa: E402

from mcpsmith import __version__  # noqa: E402
from mcpsmith.setup.agents import AGENTS, proxy_env, server_entry  # noqa: E402

ENV = {"HTTPS_PROXY": "http://h:1"}


def test_server_entry_shapes():
    assert server_entry("mcpsmith", False, ENV) == {
        "command": "uvx",
        "args": [f"mcpsmith@{__version__}", "mcp"],
        "env": ENV,
    }
    win = server_entry("chrome-devtools", True, ENV)
    assert win["command"] == "cmd"
    assert win["args"][:5] == ["/d", "/c", "npx", "-y", "chrome-devtools-mcp@1.11.0"]
    assert win["env"] == {}
    assert server_entry("chrome-devtools", False, ENV)["command"] == "npx"
    assert server_entry("mcpsmith", True, ENV)["command"] == "uvx"


def test_proxy_env_strips_userinfo_and_ignores_other_vars():
    env = {"HTTPS_PROXY": "http://u:p@h:1", "NO_PROXY": "localhost", "OPENAI_API_KEY": "sk-x"}
    assert proxy_env(env) == {"HTTPS_PROXY": "http://h:1", "NO_PROXY": "localhost"}
    assert proxy_env({}) == {}


@pytest.mark.parametrize("agent_id", ["claude", "cursor"])
def test_json_agents_add_remove(agent_id):
    agent = AGENTS[agent_id]
    doc = {"theme": "dark", "mcpServers": {"other": {"command": "x"}}}
    for name in ("mcpsmith", "chrome-devtools"):
        agent.add(doc, name, server_entry(name, False, ENV))
    assert doc["mcpServers"]["mcpsmith"] == {"type": "stdio", **server_entry("mcpsmith", False, ENV)}
    assert doc["mcpServers"]["chrome-devtools"]["type"] == "stdio"
    assert list(doc["mcpServers"]) == ["other", "mcpsmith", "chrome-devtools"]
    assert agent.has(doc, "mcpsmith")
    agent.remove(doc, "mcpsmith")
    agent.remove(doc, "chrome-devtools")
    assert doc == {"theme": "dark", "mcpServers": {"other": {"command": "x"}}}
    assert agent.dump(doc).startswith('{\n  "theme"')


def test_gemini_entry_has_timeout_and_no_type():
    doc = AGENTS["gemini"].add({}, "mcpsmith", server_entry("mcpsmith", False, ENV))
    assert doc["mcpServers"]["mcpsmith"] == {**server_entry("mcpsmith", False, ENV), "timeout": 30000}


def test_codex_toml_keeps_comments_and_other_tables():
    agent = AGENTS["codex"]
    path_text = '# my config\nmodel = "o3"\n\n[mcp_servers.other]\ncommand = "x"\n'
    import tomlkit

    doc = tomlkit.parse(path_text)
    agent.add(doc, "mcpsmith", server_entry("mcpsmith", False, ENV))
    agent.add(doc, "chrome-devtools", server_entry("chrome-devtools", False, ENV))
    out = tomlkit.parse(agent.dump(doc))
    assert out["mcp_servers"]["mcpsmith"] == {
        "command": "uvx",
        "args": [f"mcpsmith@{__version__}", "mcp"],
        "startup_timeout_sec": 30,
        "env": ENV,
    }
    assert "env" not in out["mcp_servers"]["chrome-devtools"]
    assert "[mcp_servers.mcpsmith.env]" in agent.dump(doc) or "env = " in agent.dump(doc)
    agent.remove(doc, "mcpsmith")
    agent.remove(doc, "chrome-devtools")
    assert agent.dump(doc).startswith(path_text)
    assert agent.has(doc, "other") and not agent.has(doc, "mcpsmith")


def test_codex_add_to_empty_doc():
    agent = AGENTS["codex"]
    text = agent.dump(
        agent.add(agent.load(Path("/nonexistent/x.toml")), "mcpsmith", server_entry("mcpsmith", False, {}))
    )
    assert text.startswith("[mcp_servers.mcpsmith]")


@pytest.mark.parametrize("agent_id", list(AGENTS))
def test_load_malformed_raises_value_error(agent_id, tmp_path):
    bad = tmp_path / "cfg"
    bad.write_text("{ not valid [", encoding="utf-8")
    with pytest.raises(ValueError):
        AGENTS[agent_id].load(bad)


def test_paths_and_overrides(tmp_path):
    home = tmp_path
    a = AGENTS
    assert a["claude"].config_path(home, {}) == home / ".claude.json"
    assert a["claude"].config_path(home, {"CLAUDE_CONFIG_DIR": str(home / "c")}) == home / "c" / ".claude.json"
    assert a["codex"].config_path(home, {}) == home / ".codex" / "config.toml"
    assert a["codex"].config_path(home, {"CODEX_HOME": str(home / "cx")}) == home / "cx" / "config.toml"
    assert a["cursor"].config_path(home, {}) == home / ".cursor" / "mcp.json"
    assert a["gemini"].config_path(home, {}) == home / ".gemini" / "settings.json"
    assert (
        a["gemini"].config_path(home, {"GEMINI_CLI_HOME": str(home / "g")}) == home / "g" / ".gemini" / "settings.json"
    )
    assert a["claude"].skills_root(home) == home / ".claude" / "skills"
    for k in ("codex", "cursor", "gemini"):
        assert a[k].skills_root(home) == home / ".agents" / "skills"
    assert [a[k].rea_client for k in a] == ["claude_code", "codex", "cursor", "gemini_cli"]


def test_detected_by_dir_or_cli(tmp_path):
    none = lambda cli: None  # noqa: E731
    cursor = AGENTS["cursor"]
    assert not cursor.detected(tmp_path, {}, none)
    assert cursor.detected(tmp_path, {}, lambda cli: "/bin/cursor-agent" if cli == "cursor-agent" else None)
    (tmp_path / ".cursor").mkdir()
    assert cursor.detected(tmp_path, {}, none)
    assert not AGENTS["codex"].detected(tmp_path, {"CODEX_HOME": str(tmp_path / "cx")}, none)
