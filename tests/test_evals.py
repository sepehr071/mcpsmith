import json

import httpx
import pytest
import yaml

from toolsmelt import evals
from toolsmelt.cli import main
from toolsmelt.design import llm
from toolsmelt.model import Plan, save_plan

PLAN = {
    "schema_version": 1,
    "name": "shop-mcp",
    "title": "Shop",
    "description": "Search the shop.",
    "instructions": "Search products, then get one by id.",
    "prefix": "shop",
    "base_url": "https://shop.example.com",
    "env_prefix": "SHOP_MCP",
    "tools": [
        {
            "name": "shop_search",
            "title": "Search products",
            "description": "Return products matching a query.",
            "endpoint": "GET /search",
            "args": [
                {"name": "query", "description": "Words, e.g. honey.", "required": True, "to": "query.q"},
                {"name": "max_price", "type": "integer", "description": "Toman.", "to": "query.max"},
            ],
            "example": {"query": "honey"},
            "read_only": True,
        },
        {
            "name": "shop_get_product",
            "title": "Get product",
            "description": "Return one product by id.",
            "endpoint": "GET /products/{id}",
            "args": [
                {"name": "product_id", "type": "integer", "description": "Id.", "required": True, "to": "path.id"}
            ],
            "example": {"product_id": 100},
            "read_only": True,
        },
    ],
}

CASES = [
    {"question": "find honey", "tool": "shop_search", "args": {"query": "Honey"}},
    {"question": "show product 100", "tool": "shop_get_product", "args": {"product_id": 100}},
    {"question": "honey under 5000", "tool": "shop_search", "args": {"query": "honey", "max_price": 5000}},
    {"question": "what is the weather", "tool": None, "args": {}},
]
# what the fake model answers per question: right, wrong tool, wrong args, right negative
ANSWERS = {
    "find honey": {"tool": "shop_search", "args": {"query": " honey ", "max_price": None}},
    "show product 100": {"tool": "shop_search", "args": {"query": "100"}},
    "honey under 5000": {"tool": "shop_search", "args": {"query": "honey", "max_price": 9000}},
    "what is the weather": {"tool": None, "args": {}},
}


@pytest.fixture
def workdir(tmp_path):
    save_plan(Plan.model_validate(PLAN), tmp_path / "plan.yaml")
    return tmp_path


@pytest.fixture
def fake(monkeypatch):
    """A fake Anthropic API: case-writing requests get `written`, eval requests the answer for their question."""
    sent = []
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    monkeypatch.delenv("TOOLSMELT_BASE_URL", raising=False)
    monkeypatch.delenv("TOOLSMELT_MODEL", raising=False)
    written = {"cases": []}

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        sent.append(body)
        if "Write evaluation cases" in body["system"][0]["text"]:
            text = json.dumps(written)
        else:
            text = "```json\n" + json.dumps(ANSWERS[body["messages"][0]["content"]]) + "\n```"
        usage = {"input_tokens": 10, "output_tokens": 5, "cache_read_input_tokens": 900 if len(sent) > 1 else 0}
        return httpx.Response(200, json={"content": [{"type": "text", "text": text}], "usage": usage})

    monkeypatch.setattr(llm, "_transport", httpx.MockTransport(handler))
    return sent, written


def test_tool_list_is_what_the_server_exposes(workdir):
    _, tools, path = evals.target(None, workdir)
    assert path == workdir / "evals.yaml"
    search = tools[0]
    assert search["name"] == "shop_search" and search["description"] == "Return products matching a query."
    schema = search["input_schema"]
    assert schema["required"] == ["query"] and "title" not in schema
    assert set(schema["properties"]) == {"query", "max_price"}


def test_generated_project_exposes_the_same_tool_list(workdir, tmp_path):
    from toolsmelt.codegen import generate

    project = tmp_path / "out"
    generate(Plan.model_validate(PLAN), project, None)
    _, tools, path = evals.target(project, workdir)
    assert path == project / "evals.yaml"
    assert tools == evals.target(None, workdir)[1]


def test_scoring_right_wrong_tool_wrong_args_negative(workdir, fake, capsys):
    sent, _ = fake
    (workdir / "evals.yaml").write_text(yaml.safe_dump(CASES), encoding="utf-8")
    assert main(["eval", "-w", str(workdir), "--json"]) == 0
    rep = json.loads(capsys.readouterr().out)
    assert rep["cases"] == 4 and rep["passed"] == 2 and rep["score"] == 0.5
    assert rep["tool_ok"] == 3 and rep["args_checked"] == 2 and rep["args_ok"] == 1
    assert rep["tools"]["shop_get_product"]["picked_instead"] == {"shop_search": 1}
    assert rep["tools"]["(none)"] == {"cases": 1, "tool_ok": 1, "args_ok": 1, "picked_instead": {}}
    assert rep["usage"]["cache_read"] == 2700
    # the tool list is the identical, cached prefix; the question is the only user message
    assert all(b["system"] == sent[0]["system"] for b in sent)
    assert sent[0]["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert '"input_schema"' in sent[0]["system"][0]["text"] and "Search products, then" in sent[0]["system"][0]["text"]
    assert [b["messages"] for b in sent[:1]] == [[{"role": "user", "content": "find honey"}]]


def test_table_and_min_exit_code(workdir, fake, capsys):
    (workdir / "evals.yaml").write_text(yaml.safe_dump(CASES), encoding="utf-8")
    assert main(["eval", "-w", str(workdir), "--min", "0.6", "--concurrency", "3"]) == 1
    out = capsys.readouterr().out
    assert "FAIL  show product 100" in out and "got shop_search" in out
    assert "score 0.50 (2/4 cases right)" in out and "cache read 2700" in out
    assert "find honey" not in out  # passed cases are not listed


def test_write_cases_takes_args_from_examples(workdir, fake, capsys):
    sent, written = fake
    written["cases"] = [
        {"question": "any honey?", "tool": "shop_search", "args": {"query": "wrong"}},
        {"question": "product 100 please", "tool": "shop_get_product", "args": {}},
        {"question": "delete my account", "tool": None, "args": {}},
        {"question": "book a flight", "tool": "shop_flights", "args": {}},
        {"question": ""},
    ]
    assert main(["eval", "-w", str(workdir), "--write"]) == 0
    out = capsys.readouterr().out
    assert "wrote" in out and "(3 cases)" in out and "shop_flights" in out and "malformed" in out
    cases = yaml.safe_load((workdir / "evals.yaml").read_text(encoding="utf-8"))
    assert cases[0] == {"question": "any honey?", "tool": "shop_search", "args": {"query": "honey"}}
    assert cases[1]["args"] == {"product_id": 100} and cases[2]["tool"] is None
    assert '"example": {\n   "query": "honey"' in sent[0]["system"][0]["text"]


def test_ollama_writes_cases_in_json_mode_and_a_bare_list_is_accepted(workdir, monkeypatch, capsys):
    sent = []
    monkeypatch.delenv("TOOLSMELT_BASE_URL", raising=False)
    reply = json.dumps([{"question": "any honey?", "tool": "shop_search", "args": {}}])  # small models drop "cases"

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content))
        return httpx.Response(200, json={"choices": [{"message": {"content": reply}}]})

    monkeypatch.setattr(llm, "_transport", httpx.MockTransport(handler))
    assert main(["eval", "-w", str(workdir), "--write", "--provider", "ollama", "--model", "m"]) == 0
    assert sent[0]["response_format"] == {"type": "json_object"}
    cases = yaml.safe_load((workdir / "evals.yaml").read_text(encoding="utf-8"))
    assert cases == [{"question": "any honey?", "tool": "shop_search", "args": {"query": "honey"}}]


def test_existing_evals_file_is_not_overwritten(workdir, fake, capsys):
    sent, _ = fake
    (workdir / "evals.yaml").write_text("keep: me\n", encoding="utf-8")
    assert main(["eval", "-w", str(workdir), "--write"]) == 2
    assert "already exists" in capsys.readouterr().err
    assert (workdir / "evals.yaml").read_text(encoding="utf-8") == "keep: me\n" and not sent


@pytest.mark.parametrize(
    "cases, message",
    [
        ([{"question": "q", "tool": "shop_nope", "args": {}}], "unknown tool 'shop_nope'"),
        ([{"question": "q", "tool": "shop_search", "args": {"color": "red"}}], "no argument(s) ['color']"),
        ([{"question": "q", "args": {}}], "Field required"),
        ([{"question": "q", "tool": None, "extra": 1}], "Extra inputs"),
        ({"question": "q"}, "non-empty list"),
    ],
)
def test_bad_cases_file_is_an_error(workdir, fake, capsys, cases, message):
    sent, _ = fake
    (workdir / "evals.yaml").write_text(yaml.safe_dump(cases), encoding="utf-8")
    assert main(["eval", "-w", str(workdir)]) == 2
    assert message in capsys.readouterr().err and not sent


def test_missing_cases_file_says_how_to_write_it(workdir, capsys):
    assert main(["eval", "-w", str(workdir)]) == 2
    assert "--write` first" in capsys.readouterr().err


def test_bad_reply_counts_as_wrong():
    case = evals.Case(question="q", tool="shop_search", args={"query": "x"})
    r = evals.score(case, "I would search.")
    assert not r.tool_ok and r.error
    rep = evals.report([r], {})
    assert rep["tools"]["shop_search"]["picked_instead"] == {"(bad reply)": 1}


@pytest.mark.parametrize(
    "a, b, ok",
    [
        ("100", 100, True),
        (1.0, 1, True),
        ("Tehran ", "tehran", True),
        ([1, "A"], ["1", "a"], True),
        ({"x": 1}, {"x": "1"}, True),
        ("honey", "honeys", False),
        (None, "x", False),
        ([1], 1, False),
    ],
)
def test_loose_comparison(a, b, ok):
    assert evals.same(a, b) is ok


def test_openrouter_anthropic_model_caches_the_tool_list(workdir, monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "k")
    monkeypatch.delenv("TOOLSMELT_MODEL", raising=False)
    monkeypatch.delenv("TOOLSMELT_BASE_URL", raising=False)
    sent = []

    def handler(request):
        sent.append(json.loads(request.content))
        reply = '{"tool": null, "args": {}}'
        usage = {"prompt_tokens": 50, "prompt_tokens_details": {"cached_tokens": 40}}
        return httpx.Response(200, json={"choices": [{"message": {"content": reply}}], "usage": usage})

    monkeypatch.setattr(llm, "_transport", httpx.MockTransport(handler))
    plan, tools, _ = evals.target(None, workdir)
    rep = evals.run(plan, tools, [evals.Case(question="hi", tool=None)], "openrouter")
    assert rep["score"] == 1 and rep["usage"]["cache_read"] == 40
    system = sent[0]["messages"][0]
    assert system["role"] == "system" and system["content"][0]["cache_control"] == {"type": "ephemeral"}
