import json
import time
from pathlib import Path

import httpx
import pytest

from mcpsmith import importers
from mcpsmith.importers import har, openapi

FIXTURES = Path(__file__).parent / "fixtures"
PETSTORE3_URL = "https://petstore3.swagger.io/api/v3/openapi.json"


def by_id(source):
    return {e.id: e for e in source.endpoints}


# ---------- OpenAPI ----------


def test_swagger2_yaml():
    src = importers.load(str(FIXTURES / "openapi" / "petstore2.yaml"))
    eps = by_id(src)
    assert src.kind == "openapi" and src.title == "Swagger Petstore"
    assert src.base_url == "https://petstore.example.com/v2"
    assert {a.name: (a.type, a.param) for a in src.auth} == {
        "api_key": ("header", "api_key"),
        "basicAuth": ("basic", ""),
    }
    assert set(eps) == {
        "addPet",
        "findPetsByStatus",
        "getPetById",
        "updatePetWithForm",
        "DELETE /pet/{petId}",
    }  # no HEAD

    add = eps["addPet"]
    assert add.body_schema["properties"]["category"]["properties"]["name"] == {"type": "string"}  # $ref resolved
    assert add.body_example["name"] == "doggie" and add.body_example["status"] == "available"
    assert add.auth == ["api_key"]

    status = eps["findPetsByStatus"].params[0]
    assert (status.location, status.type, status.required) == ("query", "array", True)
    assert status.enum == ["available", "pending", "sold"] and status.default == "available"
    assert eps["findPetsByStatus"].response_example[0]["category"] == {"id": 0, "name": "string"}

    get = eps["getPetById"]
    assert [(p.name, p.location, p.type, p.required) for p in get.params] == [("petId", "path", "integer", True)]
    assert get.response_example == {"id": 7, "name": "Rex", "status": "sold"}
    assert get.auth == ["basicAuth"]

    form = eps["updatePetWithForm"]
    assert form.body_schema["required"] == ["name"] and set(form.body_schema["properties"]) == {"name", "status"}
    assert [p.name for p in eps["DELETE /pet/{petId}"].params] == ["petId", "api_key"]


def test_openapi31():
    src = importers.load(str(FIXTURES / "openapi" / "tasks31.json"))
    eps = by_id(src)
    assert src.base_url == "https://eu.tasks.example.com/v1"  # server variables -> defaults
    assert {a.name: (a.type, a.param) for a in src.auth} == {
        "bearerAuth": ("bearer", ""),
        "keyQuery": ("query", "key"),
        "session": ("cookie", "sid"),
        "oauth": ("bearer", ""),
    }
    assert set(eps) == {"listTasks", "createTask", "GET /tasks/{taskId}", "updateTask"}  # no OPTIONS

    listing = eps["listTasks"]
    limit = listing.params[0]
    assert (limit.name, limit.type, limit.default, limit.example) == ("limit", "integer", 20, 5)
    assert listing.params[1].enum == ["open", "done"]
    assert listing.auth == ["bearerAuth"]
    task = listing.response_example[0]
    assert task["id"] == "00000000-0000-0000-0000-000000000000" and task["due"] == "2024-01-01"
    assert task["priority"] == "high" and len(listing.response_example) == 1
    assert task["subtasks"] == []  # recursive schema: cycle stops
    assert listing.response_schema["items"]["properties"]["subtasks"]["items"] == {"$ref": "#/components/schemas/Task"}

    create = eps["createTask"]
    assert create.body_example == {"title": "Buy milk"}
    assert create.body_schema["required"] == ["title"]
    assert create.response_example == {"id": "t1"}
    assert create.auth == ["keyQuery", "session"]

    one = eps["GET /tasks/{taskId}"]
    assert [(p.name, p.location, p.required) for p in one.params] == [
        ("taskId", "path", True),
        ("X-Trace", "header", False),
    ]
    assert one.auth == []  # security: [] overrides the top level
    assert one.response_example["title"] == "string"  # from the default response

    update = eps["updateTask"]
    assert update.base_url == "https://write.tasks.example.com/v1"
    assert update.body_example == {"title": "string", "done": True}
    assert update.response_example is None


def test_ref_cycle_is_safe():
    doc = {
        "openapi": "3.0.0",
        "info": {"title": "t"},
        "paths": {
            "/n": {"get": {"responses": {"200": {"content": {"application/json": {"schema": {"$ref": "#/c/N"}}}}}}}
        },
        "c": {"N": {"type": "object", "properties": {"next": {"$ref": "#/c/N"}, "v": {"type": "integer"}}}},
    }
    ep = openapi.parse(doc).endpoints[0]
    assert ep.response_example == {"next": None, "v": 0}
    assert ep.response_schema["properties"]["next"] == {"$ref": "#/c/N"}


def test_example_depth_cap():
    schema = {"type": "string"}
    for _ in range(20):
        schema = {"type": "object", "properties": {"a": schema}}
    ex, depth = openapi.example(schema), 0
    while isinstance(ex, dict):
        ex, depth = ex["a"], depth + 1
    assert depth <= openapi.MAX_DEPTH + 1


def test_petstore3_with_origin():
    doc = json.loads((FIXTURES / "openapi" / "petstore3.json").read_text(encoding="utf-8"))
    src = openapi.parse(doc, origin=PETSTORE3_URL)
    assert len(src.endpoints) >= 10
    assert src.base_url == "https://petstore3.swagger.io/api/v3"
    assert all(e.base_url == "" for e in src.endpoints)
    find = src.endpoint("findPetsByStatus")
    assert find is not None and find.response_example


def test_load_url_passes_origin(monkeypatch):
    text = (FIXTURES / "openapi" / "petstore3.json").read_text(encoding="utf-8")

    def fake_get(url, **kwargs):
        assert kwargs.get("follow_redirects") is True
        return httpx.Response(200, text=text, request=httpx.Request("GET", url))

    monkeypatch.setattr(importers.httpx, "get", fake_get)
    assert importers.load(PETSTORE3_URL).base_url == "https://petstore3.swagger.io/api/v3"


def test_load_rejects_unknown(tmp_path):
    f = tmp_path / "x.yaml"
    f.write_text("hello: world\n", encoding="utf-8")
    with pytest.raises(ValueError, match="neither"):
        importers.load(str(f))


# ---------- HAR ----------


@pytest.fixture
def shop():
    return importers.load(str(FIXTURES / "har" / "shop.har"))


def test_har_filters_and_groups(shop):
    eps = by_id(shop)
    assert shop.kind == "har" and shop.title == "Example"
    assert shop.base_url == "https://api.shop.example.com"
    # analytics, image, html document and the CORS preflight are gone; failed /products/789 joins its group
    assert set(eps) == {
        "GET /v1/search",
        "GET /v1/products/{product_id}",
        "GET /v1/products/{product_id}/reviews",
        "GET /v1/categories/{category_id}",
        "GET /v1/me/orders",
        "POST /v1/cart",
        "GET /api/stock",
    }
    assert eps["GET /api/stock"].base_url == "https://stock.example.net"
    assert all(e.base_url == "" for i, e in eps.items() if i != "GET /api/stock")


def test_har_params(shop):
    eps = by_id(shop)
    q = {p.name: p for p in eps["GET /v1/search"].params}
    assert (q["q"].type, q["q"].required, q["q"].example) == ("string", True, "shoes")
    assert (q["page"].type, q["page"].required, q["page"].example) == ("integer", True, 1)
    assert (q["sort"].required, q["sort"].example) == (False, "price")
    assert eps["GET /v1/search"].count == 2

    pid = eps["GET /v1/products/{product_id}"].params[0]
    assert (pid.name, pid.location, pid.type, pid.required, pid.example) == ("product_id", "path", "integer", True, 123)
    cat = eps["GET /v1/categories/{category_id}"].params[0]
    assert (cat.type, cat.example) == ("string", "mens-shoes")

    stock = {p.name: p for p in eps["GET /api/stock"].params}
    assert (stock["fresh"].type, stock["fresh"].example) == ("boolean", True)


def test_har_bodies(shop):
    eps = by_id(shop)
    search = eps["GET /v1/search"].response_example
    assert search["total"] == 5 and len(search["items"]) == 3  # largest body, list cut to 3
    product = eps["GET /v1/products/{product_id}"]
    assert product.count == 2  # the 500 is dropped when successes exist
    assert product.response_example["id"] == 123 and len(product.response_example["description"]) == 300
    assert eps["GET /v1/products/{product_id}/reviews"].response_example["reviews"][1]["text"] == "ok"  # base64
    assert eps["POST /v1/cart"].body_example == {"product_id": 123, "qty": 1}


def test_har_headers_and_auth(shop):
    eps = by_id(shop)
    assert shop.headers == {"accept": "application/json", "x-client": "web"}
    assert {a.name: (a.type, a.param) for a in shop.auth} == {
        "bearer": ("bearer", ""),
        "x-api-key": ("header", "x-api-key"),
    }
    assert eps["GET /v1/me/orders"].auth == ["bearer"]
    assert eps["GET /api/stock"].auth == ["x-api-key"]
    assert eps["GET /v1/search"].auth == []
    dump = shop.model_dump_json()
    assert "secret-token-xyz" not in dump and "secret-key-123" not in dump


def test_har_hosts_filter():
    src = importers.load(str(FIXTURES / "har" / "shop.har"), hosts=["example.net"])
    assert src.base_url == "https://stock.example.net"
    assert [e.id for e in src.endpoints] == ["GET /api/stock"]
    assert all(
        e.path != "/api/stock"
        for e in importers.load(str(FIXTURES / "har" / "shop.har"), hosts=["shop.example.com"]).endpoints
    )


def test_har_only_failures_kept_and_query_token():
    entry = {
        "request": {"method": "GET", "url": "https://x.example.com/items?api_key=s3cret&page=2", "headers": []},
        "response": {"status": 401, "content": {"mimeType": "application/json", "text": '{"error": "auth"}'}},
    }
    src = har.parse({"log": {"entries": [entry]}})
    ep = src.endpoints[0]
    assert ep.response_example == {"error": "auth"}
    assert [p.name for p in ep.params] == ["page"] and ep.auth == ["api_key"]
    assert src.auth[0].type == "query" and "s3cret" not in src.model_dump_json()


def test_har_templates_ids():
    def e(path):
        return {
            "request": {"method": "GET", "url": "https://x.example.com" + path, "headers": []},
            "response": {"status": 200, "content": {"mimeType": "application/json", "text": "{}"}},
        }

    paths = [
        "/api/users/3fa85f64-5717-4562-b3fc-2c963f66afa6/orders/5f2b8c9e1a3d4e6f7a8b9c0d",
        "/api/search",
        "/api/categories",
    ]
    ids = [ep.path for ep in har.parse({"log": {"entries": [e(p) for p in paths]}}).endpoints]
    assert ids == ["/api/users/{user_id}/orders/{order_id}", "/api/search", "/api/categories"]  # plain words not merged


def _leaky_har():
    headers = [
        {"name": "accept", "value": "application/json"},
        {"name": "x-session-token", "value": "SESSTOKEN-Zq81"},
        {"name": "x-user-id", "value": "884412"},
        {"name": "x-amz-security-token", "value": "AMZTOKEN-Kp42"},
        {"name": "referer", "value": "https://shop.example.com/app?ticket=TICKET-Lm93#frag"},
    ]
    body = {
        "id": 7,
        "title": "Red shoes",
        "price": 19.5,
        "sku": "3fa85f64-5717-4562-b3fc-2c963f66afa6",
        "email": "jane@corp-leak.com",
        "contact": "call 09121234567 or mail bob@leak.org",
        "phone": "+989121234567",
        "access_token": "ACCESS-Rt55",
        "note": "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJqYW5lIn0.sigLEAK",
        "user": {"first_name": "Jane", "address": {"city": "Leakville"}},
    }
    url = "https://api.shop.example.com/v1/items"
    post = {"title": "Hat", "price": 5, "password": "hunter2-LEAK", "phone": "09129998888", "address": "12 Leak St"}
    entries = [
        {
            "request": {
                "method": "GET",
                "url": url + "?page=1&session=SESSQ-Aa11&sid=SID-Bb22&refresh_token=REFRESH-Cc33&key=KEYQ-Dd44",
                "headers": headers,
            },
            "response": {"status": 200, "content": {"mimeType": "application/json", "text": json.dumps(body)}},
        },
        {
            "request": {
                "method": "POST",
                "url": url,
                "headers": headers,
                "postData": {"mimeType": "application/json", "text": json.dumps(post)},
            },
            "response": {"status": 200, "content": {"mimeType": "application/json", "text": '{"id": 8}'}},
        },
    ]
    return {"log": {"entries": entries}}


LEAKS = [
    "SESSTOKEN-Zq81",
    "884412",
    "AMZTOKEN-Kp42",
    "TICKET-Lm93",
    "SESSQ-Aa11",
    "SID-Bb22",
    "REFRESH-Cc33",
    "KEYQ-Dd44",
    "hunter2-LEAK",
    "09129998888",
    "12 Leak St",
    "jane@corp-leak.com",
    "bob@leak.org",
    "09121234567",
    "989121234567",
    "ACCESS-Rt55",
    "sigLEAK",
    "Leakville",
    "Jane",
]


def test_har_leaks_no_secrets_or_personal_data():
    from mcpsmith.design import digest

    src = har.parse(_leaky_har())
    texts = [src.model_dump_json(), digest(src)]
    for text in texts:
        for leak in LEAKS:
            assert leak not in text, leak
        for keep in ("Red shoes", "19.5", "3fa85f64-5717-4562-b3fc-2c963f66afa6", "Hat", "user@example.com"):
            assert keep in text, keep
    assert src.headers == {"accept": "application/json", "referer": "https://shop.example.com/app"}
    eps = by_id(src)
    assert [p.name for p in eps["GET /v1/items"].params] == ["page"]
    assert eps["POST /v1/items"].body_example["price"] == 5
    assert {a.name for a in src.auth} == {"x-session-token", "x-amz-security-token", "session", "sid", "key"}


def test_scrub_placeholders():
    out = har._scrub({"userEmail": "a@b.co", "mobile": "09121112222", "ssn": "123", "age_id": 5, "user_id": 5, "n": 3})
    assert out == {
        "userEmail": "user@example.com",
        "mobile": "09000000000",
        "ssn": "<redacted>",
        "age_id": 5,
        "user_id": 0,
        "n": 3,
    }
    assert har._scrub({"apiKey": "x", "SessionID": 1, "csrf-token": "y"}) == dict.fromkeys(
        ("apiKey", "SessionID", "csrf-token"), "<redacted>"
    )


def test_scrub_phone_forms():
    persian = "".join(chr(0x6F0 + int(c)) for c in "09121234567")
    for text in [persian, "+98 912 123-4567", "0912 123 4567", "call 0912-123-4567 now"]:
        assert "09000000000" in har._scrub(text), text
    for text in ["price 1250000", "2026-10-20", "id 12345678901234567"]:
        assert har._scrub(text) == text


def test_deref_expansion_is_bounded():
    schemas = {
        f"S{i}": {"type": "object", "properties": {"a": {"$ref": f"#/c/S{i + 1}"}, "b": {"$ref": f"#/c/S{i + 1}"}}}
        for i in range(25)
    }
    schemas["S25"] = {"type": "string"}
    doc = {
        "openapi": "3.0.0",
        "info": {"title": "t"},
        "paths": {
            "/n": {"get": {"responses": {"200": {"content": {"application/json": {"schema": {"$ref": "#/c/S0"}}}}}}}
        },
        "c": schemas,
    }
    start = time.perf_counter()
    ep = openapi.parse(doc).endpoints[0]
    assert time.perf_counter() - start < 1 and ep.response_schema["type"] == "object"
