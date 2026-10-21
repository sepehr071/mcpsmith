import json

import httpx
import pytest
from pydantic import ValidationError

from mcpsmith.design import digest, draft, guide, llm, validate
from mcpsmith.model import AuthScheme, Endpoint, Output, Param, Plan, Source, Tool, ToolArg

PRODUCTS = [
    {
        "id": 100 + i,
        "name": f"Wild honey {i}",
        "slug": f"wild-honey-{i}",
        "price": {"value": 250000 + i, "currency": "IRT"},
        "images": [{"url": f"https://cdn.example.com/{i}.jpg"}],
        "html": "<div>" + "x" * 300 + "</div>",
    }
    for i in range(3)
]


def shop() -> Source:
    return Source(
        kind="har",
        title="Example Shop API",
        base_url="https://shop.example.com",
        headers={"Accept": "application/json"},
        auth=[AuthScheme(name="bearerAuth", type="bearer")],
        endpoints=[
            Endpoint(
                id="GET /api/v1/products/search",
                method="GET",
                path="/api/v1/products/search",
                summary="Search products",
                params=[
                    Param(name="q", location="query", required=True, example="honey"),
                    Param(name="sort", location="query", enum=["rank", "price"]),
                    Param(name="page", location="query", type="integer", default=1),
                    Param(name="filters.minPrice", location="query", type="integer"),
                ],
                response_example={"data": {"products": PRODUCTS, "total": 3}},
                count=12,
            ),
            Endpoint(
                id="getProduct",
                method="GET",
                path="/api/v1/products/{productId}",
                summary="Get one product",
                params=[Param(name="productId", location="path", type="integer", example=100)],
                response_example=PRODUCTS[0],
                count=4,
            ),
            Endpoint(id="POST /track/event", method="POST", path="/track/event", body_example={"e": "view"}, count=40),
            Endpoint(
                id="POST /api/v1/cart",
                method="POST",
                path="/api/v1/cart",
                summary="Add a product to the cart",
                body_example={"product_id": 100, "quantity": 1},
                response_example={"ok": True},
                auth=["bearerAuth"],
            ),
        ],
    )


def valid_plan() -> Plan:
    return Plan(
        name="shop-mcp",
        title="Shop",
        description="Search the shop.",
        instructions="Use shop_search.",
        prefix="shop",
        base_url="https://shop.example.com",
        env_prefix="SHOP_MCP",
        tools=[
            Tool(
                name="shop_get_product",
                title="Get product",
                description="Return one product.",
                endpoint="getProduct",
                args=[
                    ToolArg(
                        name="product_id",
                        type="integer",
                        description="Id, e.g. 100.",
                        required=True,
                        to="path.productId",
                    )
                ],
                output=Output(fields={"name": "name", "price": "price.value"}),
                example={"product_id": 100},
            )
        ],
    )


# ---------- guide / digest ----------


def test_guide_covers_reference():
    g = guide()
    assert 80 <= len(g.splitlines()) <= 150
    for s in ("body.<a.b.c>", "tags[].name", "max_items", "read_only", "<prefix>_<verb>_<noun>"):
        assert s in g


def test_digest_lists_skeleton_paths():
    d = digest(shop())
    assert len(d) <= 60_000
    assert "`data.products` [3]" in d and "`data.products[].images` [1]" in d
    assert "Wild honey 0" in d and "Wild honey 1" not in d  # lists cut to one item
    assert d.index("/products/search") < d.index("/products/{productId}") < d.index("/api/v1/cart")
    assert "omitted" not in d


def test_digest_tiny_budget_notes_truncation():
    d = digest(shop(), budget=400)
    assert len(d) <= 400
    assert "omitted" in d


def test_digest_shrinks_before_dropping():
    full = digest(shop())
    d = digest(shop(), budget=len(full) - 200)
    assert len(d) <= len(full) - 200 and "omitted" not in d


# ---------- draft ----------


def test_draft_is_valid_and_shaped():
    src = shop()
    plan = draft(src)
    assert [i for i in validate(plan, src) if i.level == "error"] == []
    assert plan.name == "example-shop-mcp" and plan.prefix == "example" and plan.env_prefix == "EXAMPLE_SHOP_MCP"
    assert plan.impersonate and plan.headers == {"Accept": "application/json"}
    assert plan.auth and plan.auth.env == "EXAMPLE_SHOP_MCP_TOKEN"
    tools = {t.endpoint: t for t in plan.tools}
    assert "POST /track/event" not in tools  # noise
    search = tools["GET /api/v1/products/search"]
    assert search.name == "example_search_products"
    assert search.output.root == "data.products" and search.output.max_items == 20
    assert search.output.fields["price_value"] == "price.value" and "id" in search.output.fields
    assert search.example == {"q": "honey", "sort": "rank", "page": 1}
    assert [a.name for a in search.args] == ["q", "sort", "page", "filters_min_price"]
    assert search.args[3].to == "query.filters.minPrice"  # dots stay in the wire name
    assert "html" not in search.output.fields
    product = tools["getProduct"]
    assert product.name == "example_get_product" and product.args[0].required and product.args[0].to == "path.productId"
    assert product.example == {"product_id": 100}
    cart = tools["POST /api/v1/cart"]
    assert not cart.read_only and {a.to for a in cart.args} == {"body.product_id", "body.quantity"}
    assert all(t.description.splitlines()[0].endswith(".") for t in plan.tools)
    assert plan.tools[-1] is cart  # GET first


# ---------- validate ----------


def test_valid_plan_has_no_issues():
    assert validate(valid_plan(), shop()) == []


def _errors(plan, source=None):
    return [i for i in validate(plan, source or shop()) if i.level == "error"]


@pytest.mark.parametrize(
    "mutate, needle",
    [
        (lambda p: setattr(p.tools[0], "name", "Shop-Get"), "snake_case"),
        (lambda p: setattr(p.tools[0], "name", "other_get"), "prefix"),
        (lambda p: p.tools.append(p.tools[0].model_copy()), "duplicate tool"),
        (lambda p: p.tools[0].args.append(p.tools[0].args[0].model_copy()), "duplicate arg"),
        (lambda p: setattr(p.tools[0].args[0], "name", "class"), "identifier"),
        (lambda p: setattr(p.tools[0].args[0], "to", "form.productId"), "bad target"),
        (lambda p: p.tools[0].fixed.update({"query": 1}), "bad target"),
        (lambda p: setattr(p.tools[0], "endpoint", "nope"), "unknown endpoint"),
        (lambda p: setattr(p.tools[0].args[0], "to", "query.productId"), "{productId}"),
        (lambda p: setattr(p.tools[0].args[0], "default", 5), "has a default"),
        (lambda p: setattr(p, "description", "x" * 101), "at most 100"),
        (lambda p: setattr(p, "env_prefix", "shop-mcp"), "A-Z"),
        (lambda p: setattr(p, "base_url", "/api/v3"), "absolute URL"),
        (lambda p: setattr(p, "env_prefix", "ANTHROPIC"), "end with '_MCP'"),
        (lambda p: setattr(p.tools[0].args[0], "name", "api"), "reserved"),
        (lambda p: p.tools[0].args[0].__dict__.update(required=False, map={"a": 1}, default="b"), "not a key of map"),
        (lambda p: p.tools[0].args[0].__dict__.update(map={"a": 1}, enum=["a", "z"]), "needs a key in map"),
        # assignment and model_copy skip the model's validators: validate() repeats them
        (lambda p: setattr(p, "name", "shop\nmcp"), "name"),
        (lambda p: setattr(p, "repository", "https://github.com/a/b-->"), "repository"),
        (lambda p: setattr(p.tools[0], "name", "shop_get\n"), "snake_case"),
        (lambda p: setattr(p.tools[0], "name", "pass"), "keyword"),
        (lambda p: setattr(p.tools[0].args[0], "name", "_id"), "identifier"),
        (lambda p: setattr(p, "env_prefix", "SHOP\n"), "A-Z"),
        (lambda p: setattr(p.tools[0].args[0], "to", "path.productId\n"), "bad target"),
        # new rules
        (lambda p: setattr(p, "auth", AuthScheme(name="t", type="bearer", env="ANTHROPIC_API_KEY")), "must start with"),
        (lambda p: p.tools[0].fixed.update({"path.productId": 1}), "more than once"),
        (lambda p: setattr(p.tools[0], "endpoint", "DELETE /api/v1/products/{productId}"), "read_only"),
        (lambda p: setattr(p.tools[0], "example", {"product_id": float("nan")}), "NaN"),
        (lambda p: setattr(p.tools[0].args[0], "transform", "nope"), "unknown transform"),
        (lambda p: setattr(p.tools[0].args[0], "transform", "unix->date"), "unknown transform"),  # output-only name
        (lambda p: setattr(p.tools[0].output, "transforms", {"x": "unix->date"}), "not in fields"),
        (lambda p: p.tools[0].output.__dict__.update(fields={"x": "a"}, transforms={"x": "nope"}), "unknown transform"),
        (lambda p: setattr(p, "timezone", "Mars/Base"), "timezone"),
        (lambda p: p.tools[0].args[0].__dict__.update(maximum=float("inf")), "NaN"),
        (lambda p: p.tools[0].fixed.update({"query.x": [1, {"y": float("-inf")}]}), "NaN"),
        (lambda p: p.tools[0].args[0].__dict__.update(required=False, map={"a": float("nan")}), "NaN"),
    ],
)
def test_validate_errors(mutate, needle):
    plan = valid_plan()
    mutate(plan)
    errors = _errors(plan)
    assert errors and any(needle in e.message for e in errors), errors


def test_browser_auth_warns():
    plan = valid_plan()
    plan.auth = AuthScheme(name="b", type="browser", login_path="/Account/Login")
    issues = validate(plan)
    assert not [i for i in issues if i.level == "error"]
    assert any(i.level == "warning" and "token" in i.message for i in issues)


def test_session_cookie_validation():
    plan = valid_plan()
    plan.auth = AuthScheme(name="b", type="browser", login_path="/Login")
    assert any(i.level == "warning" and "session_cookie" in i.message for i in validate(plan))
    plan.auth.session_cookie = "sid"
    assert not [i for i in validate(plan) if "session_cookie" in i.message]
    plan.auth = AuthScheme(name="t", type="bearer", env="EX_MCP_TOKEN", session_cookie="sid")
    assert any("only applies to browser" in e.message for e in _errors(plan))


def test_plan_schema_and_new_fields():
    assert "transform" in Plan.model_json_schema()["$defs"]["ToolArg"]["properties"]
    plan = valid_plan()
    plan.timezone = "Asia/Tehran"
    assert not _errors(plan)


def test_validate_source_method_must_match_read_only():
    src = shop()
    src.endpoint("getProduct").method = "PATCH"
    assert any("read_only" in e.message for e in _errors(valid_plan(), src))
    src.endpoint("getProduct").method = "POST"  # searches are often POST: only a warning
    plan = valid_plan()
    assert not _errors(plan, src) and any("read_only" in i.message for i in validate(plan, src))


@pytest.mark.parametrize(
    "mutate",
    [
        lambda d: d.update(name="shop\nmcp"),
        lambda d: d.update(name="shop-mcp\n"),
        lambda d: d.update(name="../shop-mcp"),
        lambda d: d.update(name='shop"mcp'),
        lambda d: d.update(name="Shop-mcp"),
        lambda d: d.update(name="class"),
        lambda d: d.update(name="1shop"),
        lambda d: d.update(name="json"),
        lambda d: d.update(name="mcpsmith"),
        lambda d: d.update(name="a" * 64),
        lambda d: d.update(prefix="shop\n"),
        lambda d: d.update(prefix="_shop"),
        lambda d: d.update(env_prefix="SHOP_MCP\n"),
        lambda d: d.update(env_prefix="shop"),
        lambda d: d.update(repository="https://github.com/a/b-->"),
        lambda d: d.update(repository="https://github.com/a/b\nx"),
        lambda d: d.update(repository="https://evil.example/a/b"),
        lambda d: d.update(concurrency=0),
        lambda d: d.update(cache_ttl=-1),
        lambda d: d.update(auth={"name": "t", "type": "bearer", "env": "lower"}),
        lambda d: d.update(auth={"name": "t", "type": "bearer", "env": "SHOP_TOKEN\n"}),
        lambda d: d["tools"][0].update(name="pass"),
        lambda d: d["tools"][0].update(name="api"),
        lambda d: d["tools"][0].update(name="main"),
        lambda d: d["tools"][0].update(name="shop_get\n"),
        lambda d: d["tools"][0]["args"][0].update(name="_id"),
        lambda d: d["tools"][0]["args"][0].update(name="shape"),
        lambda d: d["tools"][0]["args"][0].update(name="class"),
        lambda d: d["tools"][0]["args"][0].update(name="id\n"),
        lambda d: d["tools"][0]["args"][0].update(to="query.x\n"),
        lambda d: d["tools"][0]["args"][0].update(to="form.x"),
        lambda d: d["tools"][0].update(fixed={"query.x\n": 1}),
    ],
)
def test_model_rejects_unsafe_values(mutate):
    data = valid_plan().model_dump()
    mutate(data)
    with pytest.raises(ValidationError):
        Plan.model_validate(data)


def test_model_normalizes_repository_and_keeps_schema():
    data = valid_plan().model_dump() | {"repository": "https://github.com/some-one/shop.mcp.git/"}
    assert Plan.model_validate(data).repository == "https://github.com/some-one/shop.mcp"
    assert json.dumps(Plan.model_json_schema())


def test_draft_avoids_reserved_names():
    src = shop()
    src.endpoints[0].params.append(Param(name="api", location="query"))
    plan = draft(src)
    assert "api_" in [a.name for t in plan.tools for a in t.args]
    assert [i for i in validate(plan, src) if i.level == "error"] == []


def test_validate_warnings():
    plan = valid_plan()
    t = plan.tools[0]
    t.description = "Returns one product"
    t.example = {}
    t.output.fields["bogus"] = "nope.x"
    t.endpoint = "POST /api/v1/cart"
    t.args = []
    plan.tools += [t.model_copy(update={"name": f"shop_t{i}"}) for i in range(20)]
    issues = validate(plan, shop())
    assert not [i for i in issues if i.level == "error"]
    text = " ".join(i.message for i in issues)
    for needle in ("ending with '.'", "20 or fewer", "read_only", "resolves to nothing"):
        assert needle in text
    t2 = valid_plan().tools[0]
    t2.example = {}
    assert any("example" in i.where for i in validate(valid_plan().model_copy(update={"tools": [t2]})))


def test_validate_big_response_without_fields():
    src = shop()
    src.endpoint("getProduct").response_example = {"blob": "y" * 5000}
    plan = valid_plan()
    plan.tools[0].output = Output()
    assert any("4 KB" in i.message for i in validate(plan, src))


# ---------- llm ----------


@pytest.fixture
def calls(monkeypatch):
    sent = []
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    monkeypatch.setenv("OPENROUTER_API_KEY", "k")
    monkeypatch.delenv("MCPSMITH_BASE_URL", raising=False)
    monkeypatch.delenv("MCPSMITH_MODEL", raising=False)

    def use(replies, shape):
        def handler(request: httpx.Request) -> httpx.Response:
            sent.append((request, json.loads(request.content)))
            return httpx.Response(200, json=shape(replies[len(sent) - 1]))

        monkeypatch.setattr(llm, "_transport", httpx.MockTransport(handler))
        return sent

    return use


BAD = '```json\n{"name": "shop-mcp"}\n```'
GOOD = "Here it is:\n" + valid_plan().model_dump_json()


def test_design_anthropic_retries_with_errors(calls):
    sent = calls(
        [BAD, GOOD],
        lambda text: {
            "content": [{"type": "thinking", "thinking": ""}, {"type": "text", "text": text}],
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 10, "output_tokens": 5, "cache_read_input_tokens": 900},
        },
    )
    plan = llm.design(shop(), goal="shopping workflow")
    assert plan.tools[0].name == "shop_get_product"
    assert len(sent) == 2
    req, body = sent[0]
    assert str(req.url) == "https://api.anthropic.com/v1/messages"
    assert req.headers["x-api-key"] == "k" and req.headers["anthropic-version"] == "2023-06-01"
    assert body["model"] == "claude-sonnet-5-5"
    assert body["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert "Plan JSON schema" in body["system"][0]["text"]
    assert "API digest" in body["messages"][0]["content"] and "shopping workflow" in body["messages"][0]["content"]
    retry = sent[1][1]["messages"]
    assert [m["role"] for m in retry] == ["user", "assistant", "user"]
    assert retry[1]["content"][0]["type"] == "thinking"  # full content passed back
    assert "title" in retry[2]["content"] and "Field required" in retry[2]["content"]
    assert sent[0][1]["system"] == sent[1][1]["system"]  # cached prefix stays identical


def test_design_openai_compatible(calls):
    sent = calls(
        [BAD, GOOD],
        lambda text: {"choices": [{"message": {"content": text}}], "usage": {"prompt_tokens": 3}},
    )
    plan = llm.design(shop(), provider="openrouter")
    assert plan.name == "shop-mcp"
    req, body = sent[0]
    assert str(req.url) == "https://openrouter.ai/api/v1/chat/completions"
    assert req.headers["authorization"] == "Bearer k"
    assert body["model"] == "anthropic/claude-sonnet-5.5"
    assert body["messages"][0]["content"][0]["cache_control"] == {"type": "ephemeral"}
    assert [m["role"] for m in sent[1][1]["messages"]] == ["system", "user", "assistant", "user"]


def test_design_ollama_gives_up(calls, monkeypatch):
    monkeypatch.setenv("MCPSMITH_BASE_URL", "http://localhost:9999/v1/")
    sent = calls([BAD, BAD], lambda text: {"choices": [{"message": {"content": text}}]})
    with pytest.raises(ValueError, match="still invalid after 2 rounds"):
        llm.design(shop(), provider="ollama", max_rounds=2)
    req, body = sent[0]
    assert str(req.url) == "http://localhost:9999/v1/chat/completions" and "authorization" not in req.headers
    assert body["model"] == "qwen3" and isinstance(body["messages"][0]["content"], str)


def test_design_missing_key(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    with pytest.raises(ValueError, match="OPENAI_API_KEY"):
        llm.design(shop(), provider="openai")
