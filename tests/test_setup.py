import subprocess
import zipfile
from pathlib import Path

from toolsmelt.skills_path import skills_dir

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
        assert f"toolsmelt/skills/{name}/SKILL.md" in names


# --- agent targets ---

import pytest  # noqa: E402
import tomlkit  # noqa: E402

from toolsmelt import __version__  # noqa: E402
from toolsmelt.setup.agents import AGENTS, proxy_env, server_entry  # noqa: E402

ENV = {"HTTPS_PROXY": "http://h:1"}


def test_server_entry_shapes():
    assert server_entry("toolsmelt", False, ENV) == {
        "command": "uvx",
        "args": [f"toolsmelt@{__version__}", "mcp"],
        "env": ENV,
    }
    win = server_entry("toolsmelt-chrome", True, ENV)
    assert win["command"] == "cmd"
    assert win["args"][:5] == ["/d", "/c", "npx", "-y", "chrome-devtools-mcp@1.11.0"]
    assert win["env"] == {}
    assert server_entry("toolsmelt-chrome", False, ENV)["command"] == "npx"
    assert server_entry("toolsmelt", True, ENV)["command"] == "uvx"


def test_proxy_env_strips_userinfo_and_ignores_other_vars():
    env = {"HTTPS_PROXY": "http://u:p@h:1", "NO_PROXY": "localhost", "OPENAI_API_KEY": "sk-x"}
    assert proxy_env(env) == {"HTTPS_PROXY": "http://h:1", "NO_PROXY": "localhost"}
    assert proxy_env({}) == {}


@pytest.mark.parametrize("agent_id", ["claude", "cursor"])
def test_json_agents_add_remove(agent_id):
    agent = AGENTS[agent_id]
    doc = {"theme": "dark", "mcpServers": {"other": {"command": "x"}}}
    for name in ("toolsmelt", "toolsmelt-chrome"):
        agent.add(doc, name, server_entry(name, False, ENV))
    assert doc["mcpServers"]["toolsmelt"] == {"type": "stdio", **server_entry("toolsmelt", False, ENV)}
    assert doc["mcpServers"]["toolsmelt-chrome"]["type"] == "stdio"
    assert list(doc["mcpServers"]) == ["other", "toolsmelt", "toolsmelt-chrome"]
    assert agent.has(doc, "toolsmelt")
    agent.remove(doc, "toolsmelt")
    agent.remove(doc, "toolsmelt-chrome")
    assert doc == {"theme": "dark", "mcpServers": {"other": {"command": "x"}}}
    assert agent.dump(doc).startswith('{\n  "theme"')


def test_gemini_entry_has_timeout_and_no_type():
    doc = AGENTS["gemini"].add({}, "toolsmelt", server_entry("toolsmelt", False, ENV))
    assert doc["mcpServers"]["toolsmelt"] == {**server_entry("toolsmelt", False, ENV), "timeout": 30000}


def test_codex_toml_keeps_comments_and_other_tables():
    agent = AGENTS["codex"]
    path_text = '# my config\nmodel = "o3"\n\n[mcp_servers.other]\ncommand = "x"\n'
    import tomlkit

    doc = tomlkit.parse(path_text)
    agent.add(doc, "toolsmelt", server_entry("toolsmelt", False, ENV))
    agent.add(doc, "toolsmelt-chrome", server_entry("toolsmelt-chrome", False, ENV))
    out = tomlkit.parse(agent.dump(doc))
    assert out["mcp_servers"]["toolsmelt"] == {
        "command": "uvx",
        "args": [f"toolsmelt@{__version__}", "mcp"],
        "startup_timeout_sec": 30,
        "env": ENV,
    }
    assert "env" not in out["mcp_servers"]["toolsmelt-chrome"]
    assert "[mcp_servers.toolsmelt.env]" in agent.dump(doc) or "env = " in agent.dump(doc)
    agent.remove(doc, "toolsmelt")
    agent.remove(doc, "toolsmelt-chrome")
    assert agent.dump(doc).startswith(path_text)
    assert agent.has(doc, "other") and not agent.has(doc, "toolsmelt")


def test_codex_add_to_empty_doc():
    agent = AGENTS["codex"]
    text = agent.dump(
        agent.add(agent.load(Path("/nonexistent/x.toml")), "toolsmelt", server_entry("toolsmelt", False, {}))
    )
    assert text.startswith("[mcp_servers.toolsmelt]")


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
    assert a["claude"].skills_root(home, {}) == home / ".claude" / "skills"
    assert a["claude"].skills_root(home, {"CLAUDE_CONFIG_DIR": str(home / "c")}) == home / "c" / "skills"
    for k in ("codex", "cursor", "gemini"):
        assert a[k].skills_root(home, {}) == home / ".agents" / "skills"
    assert [a[k].rea_client for k in a] == ["claude_code", "codex", "cursor", "gemini_cli"]


def test_detected_by_dir_or_cli(tmp_path):
    none = lambda cli: None  # noqa: E731
    cursor = AGENTS["cursor"]
    assert not cursor.detected(tmp_path, {}, none)
    assert cursor.detected(tmp_path, {}, lambda cli: "/bin/cursor-agent" if cli == "cursor-agent" else None)
    (tmp_path / ".cursor").mkdir()
    assert cursor.detected(tmp_path, {}, none)
    assert not AGENTS["codex"].detected(tmp_path, {"CODEX_HOME": str(tmp_path / "cx")}, none)


# --- toolsmelt setup ---

import io  # noqa: E402
import json  # noqa: E402
from types import SimpleNamespace  # noqa: E402

from toolsmelt.cli import build_parser, main  # noqa: E402
from toolsmelt.setup import run as setup_run  # noqa: E402

TOML = '# my config\nmodel = "o3"\n\n[mcp_servers.other]\ncommand = "x"\n'


class Tty(io.StringIO):
    def isatty(self):
        return True


def make_home(tmp_path):
    (tmp_path / ".claude.json").write_text(json.dumps({"theme": "dark", "mcpServers": {"o": {"command": "x"}}}))
    (tmp_path / ".claude").mkdir()
    (tmp_path / ".codex").mkdir()
    (tmp_path / ".codex" / "config.toml").write_text(TOML, encoding="utf-8")
    return tmp_path


def go(tmp_path, *argv, which=lambda c: "/bin/" + c, stdin=None, env=None):
    calls = []

    def runner(cmd, **kw):
        calls.append(cmd)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    args = build_parser().parse_args(["setup", *argv])
    rc = setup_run.cmd(args, tmp_path, env or {}, which, runner, stdin or Tty())
    return rc, calls


def test_dry_run_writes_nothing(tmp_path, capsys):
    home = make_home(tmp_path)
    before = (home / ".claude.json").read_bytes()
    rc, calls = go(home, "--dry-run", "--rea", "--agents", "claude,codex")
    out = capsys.readouterr().out
    assert rc == 0 and calls == []
    assert (home / ".claude.json").read_bytes() == before
    assert not list(home.rglob("*.toolsmelt.bak")) and not (home / ".agents").exists()
    assert out.count("add") >= 6 and "toolsmelt-chrome" in out and "rea" in out and ".agents" in out


def test_setup_remove_roundtrip(tmp_path):
    home = make_home(tmp_path)
    orig_json = json.loads((home / ".claude.json").read_text())
    rc, calls = go(home, "--yes", "--rea", "--agents", "claude,codex")
    assert rc == 0
    assert [c[-3:] for c in calls] == [["--client", "claude_code", "--yes"], ["--client", "codex", "--yes"]]
    assert "setup" in calls[0] and setup_run.REA in calls[0]
    cfg = json.loads((home / ".claude.json").read_text())
    assert {"toolsmelt", "toolsmelt-chrome", "o"} <= set(cfg["mcpServers"])
    assert "mcp_servers.toolsmelt" in (home / ".codex" / "config.toml").read_text()
    for n in SKILLS:
        assert (home / ".claude" / "skills" / n / "SKILL.md").is_file()
        assert (home / ".agents" / "skills" / n / "SKILL.md").is_file()
    bak = home / ".claude.json.toolsmelt.bak"
    assert json.loads(bak.read_text()) == orig_json
    assert (home / ".codex" / "config.toml.toolsmelt.bak").read_text(encoding="utf-8") == TOML

    # second run: nothing to add, backups untouched
    rc, calls = go(home, "--yes", "--agents", "claude,codex", "--no-rea")
    assert json.loads(bak.read_text()) == orig_json
    plan = setup_run.plan(home, {}, ["claude", "codex"], False, True, False, lambda c: "/x")
    assert not [c for c in plan if c.action == "add"] and {c.action for c in plan} == {"skip"}

    rc, calls = go(home, "--yes", "--rea", "--remove", "--agents", "claude,codex")
    assert calls == []  # REA is left installed on remove
    assert json.loads((home / ".claude.json").read_text()) == orig_json
    assert (home / ".codex" / "config.toml").read_text(encoding="utf-8").startswith(TOML)
    assert not any((home / ".claude" / "skills").glob("*")) and not any((home / ".agents" / "skills").glob("*"))


def test_undetected_agent_untouched(tmp_path):
    home = make_home(tmp_path)
    rc, _ = go(home, "--yes", "--no-rea", which=lambda c: None)
    assert rc == 0
    assert not (home / ".cursor").exists() and not (home / ".gemini").exists()


def test_malformed_config_skips_that_agent(tmp_path, capsys):
    home = make_home(tmp_path)
    (home / ".codex" / "config.toml").write_text("[[[ bad", encoding="utf-8")
    (home / ".claude.json").write_text(json.dumps({"mcpServers": "oops"}))
    (home / ".cursor").mkdir()
    rc, calls = go(home, "--yes", "--rea", "--agents", "claude,codex,cursor")
    assert rc == 0 and [c[-2] for c in calls] == ["cursor"]
    assert (home / ".codex" / "config.toml").read_text(encoding="utf-8") == "[[[ bad"
    assert "toolsmelt" in json.loads((home / ".cursor" / "mcp.json").read_text())["mcpServers"]
    assert capsys.readouterr().out.count("skipped:") >= 2


def test_no_node_skips_chrome_and_rea(tmp_path, capsys):
    home = make_home(tmp_path)
    rc, calls = go(home, "--yes", "--rea", "--agents", "claude", which=lambda c: None)
    out = capsys.readouterr().out
    assert rc == 0 and calls == []
    assert out.count("npx not found") >= 2
    assert list(json.loads((home / ".claude.json").read_text())["mcpServers"]) == ["o", "toolsmelt"]


def test_other_entries_untouched_by_name(tmp_path):
    home = make_home(tmp_path)
    mine = {"command": "cmd", "args": ["/c", "npx", "-y", "chrome-devtools-mcp@1.10.1", "--isolated"]}
    (home / ".claude.json").write_text(json.dumps({"mcpServers": {"chrome-devtools": mine}}))
    own = '[mcp_servers.chrome-devtools]\ncommand = "npx"\nargs = ["chrome-devtools-mcp@1.10.1"]\n'
    toml = TOML + "\n" + own
    (home / ".codex" / "config.toml").write_text(toml, encoding="utf-8")
    flags = ("--yes", "--agents", "claude,codex", "--no-rea", "--no-skills")
    go(home, *flags)
    servers = json.loads((home / ".claude.json").read_text())["mcpServers"]
    assert servers["chrome-devtools"] == mine and {"toolsmelt", "toolsmelt-chrome"} <= set(servers)
    assert own in (home / ".codex" / "config.toml").read_text("utf-8")
    go(home, *flags, "--remove")
    assert json.loads((home / ".claude.json").read_text()) == {"mcpServers": {"chrome-devtools": mine}}
    assert (home / ".codex" / "config.toml").read_text(encoding="utf-8") == toml


def test_verify_failure_restores(tmp_path, monkeypatch):
    home = make_home(tmp_path)
    before = (home / ".claude.json").read_bytes()
    monkeypatch.setattr(AGENTS["claude"].__class__, "has", lambda self, doc, name: False)
    rc, _ = go(home, "--yes", "--agents", "claude", "--no-rea", "--no-skills")
    assert rc == 1 and (home / ".claude.json").read_bytes() == before


def test_remove_keeps_rea_and_prints_command(tmp_path, capsys):
    home = make_home(tmp_path)
    rc, calls = go(home, "--yes", "--rea", "--remove", "--agents", "claude")
    assert rc == 0 and calls == []
    assert "REA left installed; remove it with: npx -y rea-agents@6.7.0 uninstall --client claude_code --yes" in (
        capsys.readouterr().out
    )


def test_write_is_atomic_and_keeps_symlink(tmp_path, monkeypatch):
    home = make_home(tmp_path)
    real = home / "dotfiles" / "claude.json"
    real.parent.mkdir()
    real.write_bytes((home / ".claude.json").read_bytes())
    (home / ".claude.json").unlink()
    try:
        (home / ".claude.json").symlink_to(real)
    except OSError:
        pytest.skip("symlinks not permitted")
    before, seen = real.read_bytes(), []
    replace = setup_run.os.replace

    def spy(src, dst):
        seen.append((Path(dst), real.read_bytes() == before, json.loads(Path(src).read_text())))
        replace(src, dst)

    monkeypatch.setattr(setup_run.os, "replace", spy)
    go(home, "--yes", "--agents", "claude", "--no-rea", "--no-skills")
    assert seen and seen[0][0] == real.resolve() and seen[0][1] and "toolsmelt" in seen[0][2]["mcpServers"]
    assert (home / ".claude.json").is_symlink() and "toolsmelt" in json.loads(real.read_text())["mcpServers"]
    assert not list(real.parent.glob("*.tmp"))


def test_user_skill_survives_install_and_remove(tmp_path, capsys):
    home = make_home(tmp_path)
    mine = home / ".claude" / "skills" / "extract-api"
    mine.mkdir(parents=True)
    (mine / "SKILL.md").write_text("mine", encoding="utf-8")
    go(home, "--yes", "--agents", "claude", "--no-rea")
    assert "exists and is not toolsmelt's" in capsys.readouterr().out
    assert (mine / "SKILL.md").read_text(encoding="utf-8") == "mine" and not (mine / ".toolsmelt").exists()
    assert (home / ".claude" / "skills" / "build-mcp-server" / ".toolsmelt").is_file()
    go(home, "--yes", "--remove", "--agents", "claude", "--no-rea")
    assert (mine / "SKILL.md").read_text(encoding="utf-8") == "mine"
    assert not (home / ".claude" / "skills" / "build-mcp-server").exists()


def test_claude_config_dir_skills_root(tmp_path):
    home = make_home(tmp_path)
    cdir = home / "cc"
    cdir.mkdir()
    go(home, "--yes", "--agents", "claude", "--no-rea", env={"CLAUDE_CONFIG_DIR": str(cdir)})
    assert (cdir / "skills" / "build-mcp-server" / "SKILL.md").is_file()
    assert not (home / ".claude" / "skills").exists()


def test_rea_timeout_is_failed_line(tmp_path, capsys):
    home = make_home(tmp_path)
    seen = {}

    def runner(cmd, **kw):
        seen.update(kw)
        raise subprocess.TimeoutExpired(cmd, kw["timeout"])

    args = build_parser().parse_args(["setup", "--yes", "--rea", "--agents", "claude", "--no-skills"])
    rc = setup_run.cmd(args, home, {}, lambda c: "/bin/" + c, runner, Tty())
    assert rc == 1 and seen["timeout"] == 300 and "FAILED claude rea: timed out" in capsys.readouterr().out


def test_skill_copy_error_continues_with_other_agents(tmp_path, capsys, monkeypatch):
    home = make_home(tmp_path)
    real_copy = setup_run.shutil.copytree

    def copy(src, dst, **kw):
        if ".claude" in Path(dst).parts:
            raise OSError("locked")
        return real_copy(src, dst, **kw)

    monkeypatch.setattr(setup_run.shutil, "copytree", copy)
    rc, _ = go(home, "--yes", "--agents", "claude,codex", "--no-rea")
    out = capsys.readouterr().out
    assert rc == 1 and "FAILED skill" in out and "locked" in out
    assert (home / ".agents" / "skills" / "build-mcp-server" / "SKILL.md").is_file()
    assert "mcp_servers.toolsmelt" in (home / ".codex" / "config.toml").read_text(encoding="utf-8")


def test_non_tty_without_yes_exits_2(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(setup_run.Path, "home", lambda: tmp_path)
    monkeypatch.setattr("sys.stdin", io.StringIO(""))
    assert main(["setup", "--agents", "claude", "--no-rea"]) == 2
    assert "--yes" in capsys.readouterr().err
    assert not (tmp_path / ".claude.json").exists()


def test_confirmation_prompt(tmp_path, monkeypatch):
    home = make_home(tmp_path)
    monkeypatch.setattr("builtins.input", lambda _: "n")
    go(home, "--agents", "claude", "--no-rea")
    assert not (home / ".claude.json.toolsmelt.bak").exists()
    monkeypatch.setattr("builtins.input", lambda _: "y")
    go(home, "--agents", "claude", "--no-rea")
    assert (home / ".claude.json.toolsmelt.bak").exists()


def test_rea_is_opt_in(tmp_path):
    home = make_home(tmp_path)
    flags = ("--yes", "--agents", "claude", "--no-skills")
    assert go(home, *flags)[1] == []  # default: no REA
    assert go(home, *flags, "--no-rea")[1] == []  # old flag still accepted
    assert [c[-3:] for c in go(home, *flags, "--rea")[1]] == [["--client", "claude_code", "--yes"]]


# --- toolsmelt install ---

from toolsmelt.model import Plan, save_plan  # noqa: E402


def make_project(tmp_path, name="demo-mcp"):
    project = tmp_path / "proj"
    project.mkdir(parents=True)
    plan = Plan(
        name=name,
        title="Demo",
        description="Demo.",
        instructions="Demo.",
        prefix="demo",
        env_prefix="DEMO_MCP",
        base_url="https://x.test",
        tools=[],
    )
    save_plan(plan, project / "toolsmelt.yaml")
    return project


def install(home, *argv, env=None):
    args = build_parser().parse_args(["install", *argv])
    return setup_run.install_cmd(args, home, env or {}, lambda c: "/bin/" + c, Tty())


def test_install_adds_entry_and_removes_it(tmp_path):
    home, project = make_home(tmp_path), make_project(tmp_path)
    orig_json = json.loads((home / ".claude.json").read_text())
    assert install(home, str(project), "--yes", "--agents", "claude,codex") == 0
    local = {"command": "uv", "args": ["run", "--directory", str(project.resolve()), "demo-mcp"], "env": {}}
    servers = json.loads((home / ".claude.json").read_text())["mcpServers"]
    assert servers["demo-mcp"] == {"type": "stdio", **local} and set(servers) == {"o", "demo-mcp"}
    codex = tomlkit.parse((home / ".codex" / "config.toml").read_text(encoding="utf-8"))
    assert codex["mcp_servers"]["demo-mcp"]["args"] == local["args"] and "env" not in codex["mcp_servers"]["demo-mcp"]
    assert json.loads((home / ".claude.json.toolsmelt.bak").read_text()) == orig_json
    changes = setup_run.install_changes(project, home, {}, ["claude", "codex"])
    assert {c.action for c in changes} == {"skip"}  # second run: up to date
    assert install(home, str(project), "--yes", "--remove", "--agents", "claude,codex") == 0
    assert json.loads((home / ".claude.json").read_text()) == orig_json
    assert (home / ".codex" / "config.toml").read_text(encoding="utf-8").startswith(TOML)


def test_install_pypi_entry(tmp_path):
    home, project = make_home(tmp_path), make_project(tmp_path)
    assert install(home, str(project), "--yes", "--pypi", "--agents", "claude") == 0
    entry = json.loads((home / ".claude.json").read_text())["mcpServers"]["demo-mcp"]
    assert (entry["command"], entry["args"], entry["env"]) == ("uvx", ["demo-mcp@0.1.0"], {})


def test_install_refuses_conflicting_entry_unless_force(tmp_path, capsys):
    home, project = make_home(tmp_path), make_project(tmp_path)
    theirs = {"command": "node", "args": ["server.js"]}
    (home / ".claude.json").write_text(json.dumps({"mcpServers": {"demo-mcp": theirs, "toolsmelt": {"command": "x"}}}))
    assert install(home, str(project), "--yes", "--agents", "claude") == 1
    assert "CONFLICT" in capsys.readouterr().out
    assert json.loads((home / ".claude.json").read_text())["mcpServers"]["demo-mcp"] == theirs
    assert install(home, str(project), "--yes", "--remove", "--agents", "claude") == 1
    assert json.loads((home / ".claude.json").read_text())["mcpServers"]["demo-mcp"] == theirs
    assert install(home, str(project), "--yes", "--force", "--agents", "claude") == 0
    servers = json.loads((home / ".claude.json").read_text())["mcpServers"]
    assert servers["demo-mcp"]["command"] == "uv" and servers["toolsmelt"] == {"command": "x"}


def test_install_dry_run_and_bad_project(tmp_path, capsys):
    home, project = make_home(tmp_path), make_project(tmp_path)
    before = (home / ".claude.json").read_bytes()
    assert install(home, str(project), "--dry-run", "--agents", "claude") == 0
    assert (home / ".claude.json").read_bytes() == before and "dry run" in capsys.readouterr().out
    with pytest.raises(OSError):
        install(home, str(tmp_path / "missing"), "--yes", "--agents", "claude")
    with pytest.raises(ValueError, match="reserved"):
        setup_run.install_changes(make_project(tmp_path / "x", "toolsmelt-chrome"), home, {}, ["claude"])
