import json

import pytest
from mcp import Client

from mcpsmith import server
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
        "smith_inspect",
        "smith_draft",
        "smith_validate",
        "smith_generate",
        "smith_check",
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
