import asyncio
import json
import os

import httpx
import pytest
from mcp import Client

from mcpsmith import __version__, server
from mcpsmith.cli import main
from mcpsmith.model import load_plan, save_plan


@pytest.fixture
def anyio_backend():
    return "asyncio"


def ok(schema_example):
    return {"200": {"description": "ok", "content": {"application/json": {"example": schema_example}}}}


@pytest.fixture
def spec(tmp_path):
    doc = {
        "openapi": "3.0.3",
        "info": {"title": "Pets", "version": "1"},
        "servers": [{"url": "https://pets.example.com/api"}],
        "paths": {
            "/pets": {
                "get": {
                    "operationId": "listPets",
                    "summary": "List pets",
                    "parameters": [{"name": "status", "in": "query", "schema": {"type": "string"}, "example": "sold"}],
                    "responses": ok([{"id": 1, "name": "Rex", "status": "sold"}]),
                }
            },
            "/pets/{petId}": {
                "get": {
                    "operationId": "getPet",
                    "summary": "Get a pet",
                    "parameters": [
                        {"name": "petId", "in": "path", "required": True, "schema": {"type": "integer"}, "example": 1}
                    ],
                    "responses": ok({"id": 1, "name": "Rex", "status": "sold"}),
                }
            },
            "/owners": {
                "get": {
                    "operationId": "listOwners",
                    "summary": "List owners",
                    "responses": ok([{"id": 7, "name": "Ann"}]),
                }
            },
        },
    }
    path = tmp_path / "pets.json"
    path.write_text(json.dumps(doc), encoding="utf-8")
    return path


def test_pipeline(spec, tmp_path, capsys):
    wd = str(tmp_path / "wd")
    assert main(["inspect", str(spec), "-w", wd]) == 0
    assert "3 endpoints" in capsys.readouterr().out
    assert (tmp_path / "wd" / "source.json").exists() and (tmp_path / "wd" / "digest.md").exists()

    assert main(["draft", "-w", wd]) == 0
    assert main(["draft", "-w", wd]) == 2  # refuses to overwrite
    assert "already exists" in capsys.readouterr().err
    assert main(["draft", "-w", wd, "--force", "--name", "pets-mcp"]) == 0
    assert load_plan(tmp_path / "wd" / "plan.yaml").name == "pets-mcp"

    assert main(["validate", "-w", wd]) == 0
    out = tmp_path / "out"
    assert main(["generate", "-w", wd, "-o", str(out)]) == 0
    assert "mcpsmith check" in capsys.readouterr().out
    for rel in ("pyproject.toml", "server.json", "mcpsmith.yaml", "src/pets_mcp/server.py", "tests/test_tools.py"):
        assert (out / rel).exists(), rel


def test_validate_fails_on_broken_plan(spec, tmp_path, capsys):
    wd = str(tmp_path / "wd")
    main(["inspect", str(spec), "-w", wd])
    main(["draft", "-w", wd])
    plan_path = tmp_path / "wd" / "plan.yaml"
    plan = load_plan(plan_path)
    plan.tools[0].name = "other_name"  # syntax errors fail at load; this one only validate() sees (wrong prefix)
    save_plan(plan, plan_path)
    capsys.readouterr()

    assert main(["validate", "-w", wd]) == 1
    assert "ERROR" in capsys.readouterr().out
    assert main(["generate", "-w", wd, "-o", str(tmp_path / "out")]) == 1
    assert not (tmp_path / "out").exists()


def test_errors_are_one_line(tmp_path, capsys):
    assert main(["inspect", str(tmp_path / "missing.json"), "-w", str(tmp_path / "wd")]) == 2
    assert capsys.readouterr().err.startswith("mcpsmith: error:")


def test_guide(capsys):
    assert main(["guide"]) == 0
    assert capsys.readouterr().out.strip()


def test_schema_and_version(capsys):
    assert main(["schema"]) == 0
    assert "schema_version" in json.loads(capsys.readouterr().out)["properties"]
    with pytest.raises(SystemExit) as e:
        main(["--version"])
    assert e.value.code == 0 and capsys.readouterr().out.strip() == f"mcpsmith {__version__}"


def test_missing_file_names_the_command_to_run(tmp_path, capsys):
    wd = str(tmp_path / "wd")
    assert main(["draft", "-w", wd]) == 2
    assert "run `mcpsmith inspect` first" in capsys.readouterr().err
    assert main(["validate", "-w", wd]) == 2
    assert "run `mcpsmith draft` first" in capsys.readouterr().err


def test_check_skips_tools_without_examples(spec, tmp_path, monkeypatch, capsys):
    wd, out = str(tmp_path / "wd"), tmp_path / "out"
    main(["inspect", str(spec), "-w", wd])
    main(["draft", "-w", wd, "--name", "pets-mcp"])
    plan = load_plan(tmp_path / "wd" / "plan.yaml")
    get = next(t for t in plan.tools if t.endpoint == "getPet")
    get.example = {}
    save_plan(plan, tmp_path / "wd" / "plan.yaml")
    assert main(["generate", "-w", wd, "-o", str(out)]) == 0
    seen = []
    handler = lambda r: seen.append(r) or httpx.Response(200, json=[{"id": 1}])  # noqa: E731
    monkeypatch.setattr(httpx, "AsyncHTTPTransport", lambda **kw: httpx.MockTransport(handler))
    capsys.readouterr()
    assert main(["check", str(out)]) == 0
    printed = capsys.readouterr().out
    assert f"SKIP {get.name}  add example values for ['pet_id'] to mcpsmith.yaml and regenerate" in printed
    assert seen and not any(r.url.path.startswith("/api/pets/") for r in seen)


def test_app_desktop_tool_needs_force_to_overwrite(tmp_path):
    har = tmp_path / "capture.har"
    entry = {
        "request": {"method": "GET", "url": "https://api.demo.test/items", "headers": [], "queryString": []},
        "response": {"status": 200, "headers": [], "content": {"mimeType": "application/json", "text": "[1]"}},
    }
    har.write_text(json.dumps({"log": {"entries": [entry]}}), encoding="utf-8")
    wd = str(tmp_path / "wd")
    asyncio.run(server.smith_app_desktop(str(har), None, wd))
    with pytest.raises(ValueError, match="already exists"):
        asyncio.run(server.smith_app_desktop(str(har), None, wd))
    assert asyncio.run(server.smith_app_desktop(str(har), None, wd, force=True))["endpoints"] == 1


def test_help(capsys):
    with pytest.raises(SystemExit) as e:
        main(["--help"])
    assert e.value.code == 0
    assert "generate" in capsys.readouterr().out


@pytest.fixture
async def client():
    async with Client(server.mcp, raise_exceptions=True) as c:
        yield c


async def call(client, name, args):
    result = await client.call_tool(name, args)
    assert not result.is_error, result.content
    return json.loads(result.content[0].text)


@pytest.mark.anyio
async def test_server_tools(client, spec, tmp_path):
    tools = {t.name: t for t in (await client.list_tools()).tools}
    assert set(tools) == {
        "smith_guide",
        "smith_probe",
        "smith_app_android",
        "smith_app_js",
        "smith_app_desktop",
        "smith_inspect",
        "smith_draft",
        "smith_validate",
        "smith_generate",
        "smith_check",
        "smith_record_start",
        "smith_record_stop",
        "smith_connect",
        "smith_disconnect",
        "smith_connections",
        "smith_login",
        "smith_install",
        "smith_tools",
        "smith_call",
    }
    assert tools["smith_validate"].annotations.read_only_hint is True

    assert "guide" in await call(client, "smith_guide", {})

    wd = str(tmp_path / "wd")
    r = await call(client, "smith_inspect", {"source": str(spec), "workdir": wd})
    assert r["endpoints"] == 3 and "Pets" in r["digest"]

    r = await call(client, "smith_draft", {"workdir": wd, "name": "pets-mcp"})
    assert r["plan_path"].endswith("plan.yaml") and "pets-mcp" in r["plan"]

    assert (await call(client, "smith_validate", {"workdir": wd}))["errors"] == []

    out = tmp_path / "out"
    r = await call(client, "smith_generate", {"workdir": wd, "out": str(out)})
    assert r["files"] > 5 and (out / "pyproject.toml").exists()
    assert any("smith_install" in step for step in r["next"])


@pytest.mark.anyio
async def test_tool_list_stays_small(client):
    # every agent pays for this list in context on each turn (REA's is 3.35 MB); ≈15 KB at 0.5.0
    tools = [t.model_dump(exclude_none=True) for t in (await client.list_tools()).tools]
    assert len(json.dumps(tools)) < 24_000


@pytest.mark.anyio
async def test_smith_install_registers_generated_project(client, spec, tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(server.Path, "home", lambda: home)
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    wd, out = str(tmp_path / "wd"), tmp_path / "out"
    await call(client, "smith_inspect", {"source": str(spec), "workdir": wd})
    await call(client, "smith_draft", {"workdir": wd, "name": "pets-mcp"})
    await call(client, "smith_generate", {"workdir": wd, "out": str(out)})
    r = await call(client, "smith_install", {"project": str(out), "agents": ["claude"]})  # dry run by default
    assert "dry_run=false" in r["next"] and any("pets-mcp" in line for line in r["plan"])
    assert not (home / ".claude.json").exists()
    r = await call(client, "smith_install", {"project": str(out), "agents": ["claude"], "dry_run": False})
    assert not r["conflicts"]
    entry = json.loads((home / ".claude.json").read_text(encoding="utf-8"))["mcpServers"]["pets-mcp"]
    assert entry["command"] == "uv" and entry["args"] == ["run", "--directory", str(out.resolve()), "pets-mcp"]


@pytest.mark.anyio
async def test_server_errors_reach_the_agent(tmp_path):
    async with Client(server.mcp) as c:
        result = await c.call_tool("smith_draft", {"workdir": str(tmp_path / "missing")})
    assert result.is_error and "FileNotFoundError" in result.content[0].text and "source.json" in result.content[0].text


def test_connections_command(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("MCPSMITH_HOME", str(tmp_path))
    assert main(["connections"]) == 0
    assert "(none)" in capsys.readouterr().out


def test_setup_and_install_parse():
    from mcpsmith.cli import build_parser

    p = build_parser()
    assert not p.parse_args(["setup"]).rea and p.parse_args(["setup", "--rea"]).rea
    assert not p.parse_args(["setup", "--no-rea"]).rea  # deprecated no-op
    a = p.parse_args(["install", "proj", "--pypi", "--force", "--remove", "--agents", "claude", "--dry-run", "--yes"])
    assert (a.project, a.pypi, a.force, a.remove, a.agents, a.dry_run, a.yes) == (
        "proj",
        True,
        True,
        True,
        "claude",
        True,
        True,
    )


def test_mcp_transport_flags_reach_the_runtime(monkeypatch):
    for k in ("MCP_TRANSPORT", "MCP_HOST", "MCP_PORT"):
        monkeypatch.setenv(k, "")  # records the original, so what cmd_mcp sets is undone after the test
        monkeypatch.delenv(k)
    seen = {}
    monkeypatch.setattr(
        server, "main", lambda: seen.update({k: os.environ.get(k) for k in ("MCP_TRANSPORT", "MCP_PORT")})
    )
    assert main(["mcp", "--transport", "http", "--port", "9001"]) == 0
    assert seen == {"MCP_TRANSPORT": "http", "MCP_PORT": "9001"}
    with pytest.raises(SystemExit):
        main(["mcp", "--transport", "sse"])
