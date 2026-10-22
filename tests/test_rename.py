"""mcpsmith was renamed toolsmelt: what users set up before the rename keeps working."""

import io
import json
from pathlib import Path
from types import SimpleNamespace

from toolsmelt import browser, getenv
from toolsmelt.cli import build_parser
from toolsmelt.model import project_plan
from toolsmelt.setup import run as setup_run


def test_home_falls_back_to_the_old_folder(tmp_path, monkeypatch):
    monkeypatch.delenv("TOOLSMELT_HOME", raising=False)
    monkeypatch.delenv("MCPSMITH_HOME", raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    assert browser.home() == tmp_path / ".toolsmelt"  # fresh machine
    (tmp_path / ".mcpsmith" / "connections").mkdir(parents=True)
    assert browser.home() == tmp_path / ".mcpsmith"  # saved connections stay where they are
    (tmp_path / ".toolsmelt").mkdir()
    assert browser.home() == tmp_path / ".toolsmelt"
    monkeypatch.setenv("MCPSMITH_HOME", str(tmp_path / "old-env"))
    assert browser.home() == tmp_path / "old-env"
    monkeypatch.setenv("TOOLSMELT_HOME", str(tmp_path / "new-env"))
    assert browser.home() == tmp_path / "new-env"


def test_old_env_names_are_read(monkeypatch):
    monkeypatch.delenv("TOOLSMELT_MODEL", raising=False)
    monkeypatch.setenv("MCPSMITH_MODEL", "old")
    assert getenv("MODEL") == "old"
    monkeypatch.setenv("TOOLSMELT_MODEL", "new")
    assert getenv("MODEL") == "new"


def test_projects_generated_before_the_rename_are_found(tmp_path):
    (tmp_path / "mcpsmith.yaml").write_text("x", encoding="utf-8")
    assert project_plan(tmp_path) == tmp_path / "mcpsmith.yaml"
    (tmp_path / "toolsmelt.yaml").write_text("x", encoding="utf-8")
    assert project_plan(tmp_path) == tmp_path / "toolsmelt.yaml"


SHIM = Path(__file__).parents[1] / "compat" / "mcpsmith" / "src"
OLD_SERVER = '''
from mcpsmith.runtime import Api, Registry, build_server, run
from mcpsmith.runtime.interpret import run_tool
import toolsmelt.runtime
assert Api is toolsmelt.runtime.Api and run_tool is toolsmelt.runtime.interpret.run_tool  # same objects
api = Api(base_url="https://api.example.com", env_prefix="OLD_MCP", hosts=["api.example.com"])
tool = Registry()

@tool("Ping")
async def ping() -> dict:
    """Ping."""
    return {}

mcp = build_server("old-mcp", registry=tool)
import mcpsmith
print(mcpsmith.__version__, type(mcp).__name__)
'''


def test_shim_runs_a_server_generated_by_mcpsmith(tmp_path):
    import subprocess
    import sys

    (tmp_path / "old_server.py").write_text(OLD_SERVER, encoding="utf-8")
    env = {**__import__("os").environ, "PYTHONPATH": str(SHIM)}
    r = subprocess.run([sys.executable, str(tmp_path / "old_server.py")], env=env, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    assert r.stdout.split() == [__import__("toolsmelt").__version__, "MCPServer"]


class Tty(io.StringIO):
    def isatty(self):
        return True


def test_setup_replaces_the_old_entries_and_skill_folders(tmp_path):
    old = {"command": "uvx", "args": ["mcpsmith@0.6.0", "mcp"]}
    servers = {"mcpsmith": old, "mcpsmith-chrome": {"command": "npx"}, "mine": {"command": "x"}}
    (tmp_path / ".claude.json").write_text(json.dumps({"mcpServers": servers}), encoding="utf-8")
    skill = tmp_path / ".claude" / "skills" / "extract-api"
    skill.mkdir(parents=True)
    (skill / ".mcpsmith").write_text("installed by mcpsmith setup\n", encoding="utf-8")
    (skill / "SKILL.md").write_text("old", encoding="utf-8")

    args = build_parser().parse_args(["setup", "--yes", "--agents", "claude"])
    ok = SimpleNamespace(returncode=0, stdout="", stderr="")
    assert setup_run.cmd(args, tmp_path, {}, lambda c: "/bin/" + c, lambda cmd, **kw: ok, Tty()) == 0

    cfg = json.loads((tmp_path / ".claude.json").read_text(encoding="utf-8"))
    assert set(cfg["mcpServers"]) == {"toolsmelt", "toolsmelt-chrome", "mine"}  # the user's own entry stays
    assert (skill / ".toolsmelt").is_file() and not (skill / ".mcpsmith").exists()
    assert (skill / "SKILL.md").read_text(encoding="utf-8") != "old"
