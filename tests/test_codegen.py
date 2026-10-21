import ast
import datetime
import json
import os
import shutil
import subprocess
import sys

import httpx
import pytest
import yaml

from mcpsmith import check
from mcpsmith.codegen import generate
from mcpsmith.model import AuthScheme, Endpoint, Output, Plan, Source, Tool, ToolArg, load_plan, save_plan

try:
    import tomllib
except ImportError:  # Python 3.10
    import tomli as tomllib

ITEM = {"id": 7, "name": 'چای "سبز"', "price": {"value": 10}, "note": None}
LIST = {"data": {"items": [{"id": 1, "tags": [{"name": "x"}]}, {"id": 2, "tags": []}, {"id": 3}]}}

SOURCE = Source(
    kind="openapi",
    title="Demo",
    base_url="https://api.demo.test/v1",
    endpoints=[
        Endpoint(id="getItem", method="get", path="/items/{itemId}", response_example=ITEM),
        Endpoint(id="listItems", method="GET", path="/items", response_example=LIST),
        Endpoint(id="createItem", method="POST", path="/items", base_url="https://write.demo.test/v2"),
    ],
)

PLAN = Plan(
    name="demo-mcp",
    title="Demo",
    description="Demo shop: items and search.",
    instructions='Use demo_search first.\nPrices are "Toman"; names are Persian (چای\u200cسبز).',
    prefix="demo",
    base_url="https://api.demo.test/v1",
    env_prefix="DEMO_MCP",
    headers={"Accept": "application/json"},
    auth=AuthScheme(name="token", type="bearer", env="DEMO_MCP_TOKEN"),
    author="Test Author",
    repository="https://github.com/someone/demo-mcp",
    tools=[
        Tool(
            name="demo_item",
            title='One "item"',
            description='One item by id: name and price.\nUse after demo_search; «چای\u200cسبز» is "tea" \\ backslash."',
            endpoint="getItem",
            args=[
                ToolArg(name="item_id", type="integer", description="Item id, e.g. 7.", required=True, to="path.itemId")
            ],
            fixed={"header.X-Client": "mcpsmith"},
            output=Output(fields={"id": "id", "name": "name", "price": "price.value"}),
            example={"item_id": 7},
        ),
        Tool(
            name="demo_search",
            title="Search items",
            description="Items matching a text query, cheapest first.",
            endpoint="listItems",
            args=[
                ToolArg(
                    name="min_price", type="number", description="Lowest price.", minimum=0, to="query.filters.minPrice"
                ),
                ToolArg(name="query", description="Search text, e.g. چای.", required=True, to="query.q"),
                ToolArg(
                    name="sort",
                    description="Order.",
                    map={"new": "date_desc", "cheap": "price_asc"},
                    default="new",
                    to="query.sort",
                ),
                ToolArg(name="tags", type="array", description="Tags.", default=["a"], to="query.tags"),
                ToolArg(
                    name="limit",
                    type="integer",
                    description="How many.",
                    default=10,
                    minimum=1,
                    maximum=50,
                    to="query.size",
                ),
            ],
            output=Output(root="data.items", fields={"id": "id", "tag": "tags[].name"}, max_items=2),
            example={"query": "چای", "min_price": 5},
        ),
        Tool(
            name="demo_create",
            title="Create item",
            description="Create an item and return its id.",
            endpoint="createItem",
            args=[
                ToolArg(name="name", description="Item name.", required=True, to="body.item.name"),
                ToolArg(name="note", description="Free text note.", to="body.note"),
            ],
            fixed={"body.item.kind": "tea"},
            read_only=False,
            example={"name": "x"},
        ),
    ],
)


@pytest.fixture(scope="module")
def project(tmp_path_factory):
    out = tmp_path_factory.mktemp("gen") / "demo-mcp"
    generate(PLAN, out, SOURCE)
    return out


def test_layout_and_refuses_non_empty(project):
    for rel in [
        "pyproject.toml",
        "server.json",
        "README.md",
        "LICENSE",
        ".gitignore",
        "mcpsmith.yaml",
        "src/demo_mcp/__init__.py",
        "src/demo_mcp/server.py",
        "tests/conftest.py",
        "tests/test_tools.py",
        "tests/fixtures/demo_item.json",
        "tests/fixtures/demo_search.json",
        ".github/workflows/ci.yml",
        ".github/workflows/publish.yml",
        ".claude-plugin/plugin.json",
        "skills/demo/SKILL.md",
    ]:
        assert (project / rel).is_file(), rel
    assert not (project / "tests/fixtures/demo_create.json").exists()  # no response example
    assert load_plan(project / "mcpsmith.yaml") == PLAN
    with pytest.raises(FileExistsError):
        generate(PLAN, project, SOURCE)
    assert generate(PLAN, project, SOURCE, force=True)


def test_metadata_files(project):
    server = json.loads((project / "server.json").read_text(encoding="utf-8"))
    assert server["name"] == "io.github.someone/demo-mcp" and server["packages"][0]["identifier"] == "demo-mcp"
    env = {e["name"]: e for e in server["packages"][0]["environmentVariables"]}
    assert env["DEMO_MCP_TOKEN"]["isRequired"] and env["DEMO_MCP_TOKEN"]["isSecret"]
    assert not env["DEMO_MCP_PROXY"]["isRequired"] and "DEMO_MCP_BASE_URL" in env
    plugin = json.loads((project / ".claude-plugin/plugin.json").read_text(encoding="utf-8"))
    assert plugin["mcpServers"] == {"demo": {"command": "uvx", "args": ["demo-mcp@0.1.0"]}}
    skill = (project / "skills/demo/SKILL.md").read_text(encoding="utf-8")
    assert skill.startswith("---\nname: demo\n") and "change data on Demo: `demo_create`." in skill
    assert "pypi/demo-mcp/" in (project / ".github/workflows/publish.yml").read_text(encoding="utf-8")
    assert '| `demo_item` | One "item" | One item by id: name and price. |' in (project / "README.md").read_text(
        encoding="utf-8"
    )


def test_server_code(project):
    code = (project / "src/demo_mcp/server.py").read_text(encoding="utf-8")
    compile(code, "server.py", "exec")
    assert "\u200c" not in code and "\\u200c" in code  # invisible characters are escaped
    tree = ast.parse(code)
    tools = {}
    for node in tree.body:
        if isinstance(node, ast.AsyncFunctionDef) and node.decorator_list:
            deco = node.decorator_list[0]
            assert isinstance(deco, ast.Call) and deco.func.id == "tool" and isinstance(deco.args[0], ast.Constant)
            tools[node.name] = (deco.args[0].value, ast.get_docstring(node))
    assert tools["demo_item"][0] == 'One "item"'
    assert tools["demo_item"][1] == PLAN.tools[0].description
    assert list(tools) == ["demo_item", "demo_search", "demo_create"]
    assert '"filters.minPrice": min_price' in code
    assert '"https://write.demo.test/v2/items"' in code and '"write.demo.test"' in code


@pytest.mark.skipif(not shutil.which("ruff"), reason="ruff not installed")
def test_generated_code_is_ruff_clean(project):
    for args in (["check"], ["format", "--check"]):
        r = subprocess.run(["ruff", *args, str(project)], capture_output=True, text=True)
        assert r.returncode == 0, r.stdout + r.stderr


def test_generated_tests_pass(project):
    env = {k: v for k, v in os.environ.items() if not k.startswith("DEMO_MCP")}
    env["PYTHONPATH"] = str(project / "src")
    r = subprocess.run(
        [sys.executable, "-m", "pytest", str(project / "tests"), "-q", "-p", "no:cacheprovider"],
        cwd=project,
        env=env,
        capture_output=True,
        text=True,
    )
    assert r.returncode == 0, r.stdout + r.stderr
    assert "2 passed, 3 deselected" in r.stdout


def test_transforms_in_generated_code(tmp_path):
    source = Source(
        kind="openapi",
        title="Demo",
        base_url="https://api.demo.test/v1",
        endpoints=[Endpoint(id="days", method="GET", path="/days", response_example={"day": "2026-10-20"})],
    )
    tool = Tool(
        name="demo_day",
        title="Day",
        description="The day.",
        endpoint="days",
        args=[_arg(name="since", description="From, Jalali.", required=True, transform="jalali->unix")],
        output=Output(fields={"day": "day"}, transforms={"day": "date->jalali"}),
        example={"since": "1405/07/28"},
    )
    plan = PLAN.model_copy(update={"tools": [tool], "timezone": "Asia/Tehran"})
    out = tmp_path / "demo-mcp"
    generate(plan, out, source)
    code = (out / "src/demo_mcp/server.py").read_text(encoding="utf-8")
    assert 'TZ = "Asia/Tehran"' in code and 'transform_in("jalali->unix", since, TZ)' in code
    (out / "tests/test_transformed.py").write_text(
        "import json\n\nimport pytest\n\npytestmark = pytest.mark.anyio\n\n\n"
        "async def test_output(client, replay):\n    replay('demo_day')\n"
        "    result = await client.call_tool('demo_day', {'since': '1405/07/28'})\n"
        "    assert json.loads(result.content[0].text) == {'day': '1405/07/28'}\n",
        encoding="utf-8",
    )
    env = {k: v for k, v in os.environ.items() if not k.startswith("DEMO_MCP")} | {"PYTHONPATH": str(out / "src")}
    r = subprocess.run(
        [sys.executable, "-m", "pytest", str(out / "tests"), "-q", "-p", "no:cacheprovider"],
        cwd=out,
        env=env,
        capture_output=True,
        text=True,
    )
    assert r.returncode == 0, r.stdout + r.stderr
    assert "2 passed" in r.stdout


def test_endpoint_without_source(tmp_path):
    plan = PLAN.model_copy(update={"tools": [PLAN.tools[2].model_copy(update={"endpoint": "post /things"})]})
    generate(plan, tmp_path / "a")
    assert '"POST", "/things"' in (tmp_path / "a/src/demo_mcp/server.py").read_text(encoding="utf-8")
    with pytest.raises(ValueError, match="METHOD /path"):
        generate(PLAN, tmp_path / "b")


def test_check_run(project, monkeypatch):
    seen = []

    def handler(request):
        seen.append(request)
        if request.method == "POST":
            return httpx.Response(201, json={"id": 8})
        if request.url.path.endswith("/items"):
            return httpx.Response(200, json={"data": {"items": [{"id": 1}, {"id": 2}]}})
        return httpx.Response(404, json={"message": "gone"})

    monkeypatch.setattr(httpx, "AsyncHTTPTransport", lambda **kw: httpx.MockTransport(handler))
    monkeypatch.setenv("DEMO_MCP_TOKEN", "t")
    modules = set(sys.modules)
    safe = {r.tool: r for r in check.run(project)}
    assert safe["demo_create"].skipped and not any(r.method == "POST" for r in seen)  # writes need opting in
    results = {r.tool: r for r in check.run(project, writes=True)}
    assert not {m for m in sys.modules if m.startswith("demo_mcp")} - modules
    assert str(project / "src") not in sys.path

    assert not results["demo_item"].ok and "HTTP 404" in results["demo_item"].error
    search = results["demo_search"]
    assert search.ok and search.error is None and search.chars > 0 and search.empty_fields == ["tag"]
    assert results["demo_create"].ok and results["demo_create"].empty_fields == []

    post = next(r for r in seen if r.method == "POST")
    assert str(post.url) == "https://write.demo.test/v2/items"
    assert json.loads(post.content) == {"item": {"name": "x", "kind": "tea"}}
    assert "authorization" not in post.headers  # the credential goes only to the base URL host
    get = next(r for r in seen if r.url.path.endswith("/items/7"))
    assert get.headers["x-client"] == "mcpsmith" and get.headers["authorization"] == "Bearer t"
    search_q = next(r for r in seen if r.url.path.endswith("/items")).url.params
    assert search_q["filters.minPrice"] == "5" and search_q["sort"] == "date_desc" and search_q["size"] == "10"


def test_check_skips_write_methods_without_opt_in(project, tmp_path, monkeypatch):
    copy = tmp_path / "p"
    shutil.copytree(project, copy)
    plan = load_plan(copy / "mcpsmith.yaml")
    plan.tools[2].read_only = True  # a hand-edited project: validate() would reject this plan
    plan.tools[2].endpoint = "DELETE /items"
    save_plan(plan, copy / "mcpsmith.yaml")
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, json={})

    monkeypatch.setattr(httpx, "AsyncHTTPTransport", lambda **kw: httpx.MockTransport(handler))
    monkeypatch.setenv("DEMO_MCP_TOKEN", "t")
    results = {r.tool: r for r in check.run(copy)}
    assert results["demo_create"].skipped and not any(r.method == "POST" for r in seen)


# ---------- unsafe plans ----------


def _tool_plan(**kw):
    tool = Tool(**{"name": "demo_x", "title": "X", "description": "Does x.", "endpoint": "GET /x"} | kw)
    return PLAN.model_copy(update={"tools": [tool]})


def _arg(**kw):
    return ToolArg(**{"name": "n", "description": "N.", "to": "query.n"} | kw)


@pytest.mark.parametrize(
    "plan, needle",
    [
        (_tool_plan(endpoint="DELETE /x"), "read_only"),
        (_tool_plan(args=[_arg(default=float("nan"))]), "NaN"),
        (_tool_plan(args=[_arg(), _arg(name="m")]), "more than once"),
        (PLAN.model_copy(update={"auth": AuthScheme(name="t", type="bearer", env="ANTHROPIC_API_KEY")}), "must start"),
        (PLAN.model_copy(update={"name": "demo\nmcp"}), "plan.name"),
        (PLAN.model_copy(update={"name": "../demo"}), "plan.name"),
        (PLAN.model_copy(update={"repository": "https://github.com/a/b-->"}), "plan.repository"),
    ],
)
def test_generate_refuses_invalid_plan(tmp_path, plan, needle):
    with pytest.raises(ValueError, match="not valid") as e:
        generate(plan, tmp_path / "out")
    assert needle in str(e.value)
    assert not (tmp_path / "out").exists()


def test_generate_refuses_browser_auth(tmp_path):
    plan = PLAN.model_copy(update={"auth": AuthScheme(name="b", type="browser", login_path="/Account/Login")})
    with pytest.raises(ValueError, match="browser"):
        generate(plan, tmp_path / "out")


def test_dates_become_strings(tmp_path):
    day = datetime.date(2024, 1, 2)
    plan = _tool_plan(args=[_arg(name="since", default=day, to="query.since")], example={"since": day})
    generate(plan, tmp_path / "a")
    code = (tmp_path / "a/src/demo_mcp/server.py").read_text(encoding="utf-8")
    compile(code, "server.py", "exec")
    assert '"2024-01-02"' in code and "datetime" not in code
    compile((tmp_path / "a/tests/test_tools.py").read_text(encoding="utf-8"), "test_tools.py", "exec")
    # an unquoted date in plan.yaml is a date for YAML, a string for the plan
    data = plan.model_dump()
    data["tools"][0]["args"][0]["default"] = day
    (tmp_path / "plan.yaml").write_text(yaml.safe_dump(data), encoding="utf-8")
    assert load_plan(tmp_path / "plan.yaml").tools[0].args[0].default == "2024-01-02"


def test_pyproject_is_valid_toml_with_special_characters(tmp_path):
    description = 'Tea\xa0shop\xadDEL\x7fچای‌سبز, "quoted" \\ back.'
    plan = PLAN.model_copy(update={"description": description, "author": 'A "B"\x7f\xa0'})
    generate(plan, tmp_path / "a", SOURCE)
    data = tomllib.loads((tmp_path / "a/pyproject.toml").read_text(encoding="utf-8"))
    assert data["project"]["description"] == description and data["project"]["authors"][0]["name"] == plan.author
    assert data["project"]["scripts"] == {"demo-mcp": "demo_mcp.server:main"}
    assert data["project"]["urls"]["Homepage"] == "https://github.com/someone/demo-mcp"
    compile((tmp_path / "a/src/demo_mcp/__init__.py").read_text(encoding="utf-8"), "__init__.py", "exec")


def test_templates(project):
    ci = (project / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    assert "ruff check" in ci and "format --check" not in ci
    ignore = (project / ".gitignore").read_text(encoding="utf-8").split()
    assert "*.har" in ignore and ".mcpsmith/" in ignore
    assert (
        (project / "README.md").read_text(encoding="utf-8").startswith("<!-- mcp-name: io.github.someone/demo-mcp -->")
    )


def test_generated_dependency_floor(tmp_path):
    generate(PLAN, tmp_path / "a", SOURCE)
    assert '"mcpsmith>=0.2"' in (tmp_path / "a/pyproject.toml").read_text(encoding="utf-8")


def test_signin_project_has_login_and_replays_encrypted(tmp_path):
    source = Source(
        kind="app",
        title="Mi",
        base_url="https://us.api.io.mi.com/app",
        endpoints=[
            Endpoint(
                id="rpc", method="POST", path="/home/rpc/{did}", response_example={"code": 0, "result": ["on", "80"]}
            )
        ],
    )
    tool = Tool(
        name="demo_status",
        title="Status",
        description="Power and brightness of one bulb.",
        endpoint="rpc",
        args=[_arg(name="did", description="Bulb id, e.g. '7'.", required=True, to="path.did")],
        fixed={"body.method": "get_prop", "body.params": ["power", "bright"]},
        output=Output(fields={"power": "result.0", "brightness": "result.1"}),
        example={"did": "7"},
    )
    auth = AuthScheme(name="mi", type="signin", provider="xiaomi", env="DEMO_MCP_SESSION")
    out = tmp_path / "demo-mcp"
    generate(PLAN.model_copy(update={"tools": [tool], "auth": auth}), out, source)
    code = (out / "src/demo_mcp/server.py").read_text(encoding="utf-8")
    assert 'secret=signin.keychain("demo-mcp")' in code and '"provider": "xiaomi"' in code
    assert "async def demo_login(" in code and 'signin.login("demo-mcp", "DEMO_MCP_SESSION", api, again)' in code
    assert '"mcpsmith>=0.5"' in (out / "pyproject.toml").read_text(encoding="utf-8")
    assert "DEMO_MCP_SESSION=" not in (out / "README.md").read_text(encoding="utf-8")
    env = {k: v for k, v in os.environ.items() if not k.startswith("DEMO_MCP")} | {"PYTHONPATH": str(out / "src")}
    r = subprocess.run(
        [sys.executable, "-m", "pytest", str(out / "tests"), "-q", "-p", "no:cacheprovider"],
        cwd=out,
        env=env,
        capture_output=True,
        text=True,
    )
    assert r.returncode == 0, r.stdout + r.stderr
    assert "1 passed" in r.stdout
