import json
import time
from pathlib import Path

import httpx
import pytest

from toolsmelt import importers
from toolsmelt.importers import har, openapi

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
    from toolsmelt.design import digest

    src = har.parse(_leaky_har())
    texts = [src.model_dump_json(), digest(src)]
    for text in texts:
        for leak in LEAKS:
            assert leak not in text, leak
        for keep in ("Red shoes", "19.5", "3fa85f64-5717-4562-b3fc-2c963f66afa6", "Hat", "user@example.com"):
            assert keep in text, keep
    assert src.headers == {"accept": "application/json", "referer": "https://shop.example.com"}
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


def test_rea_scenario_capture():
    src = importers.load(str(FIXTURES / "rea" / "scenario.json"))
    ep = by_id(src)["POST /1/indexes/Item_dev/query"]
    assert src.kind == "har" and len(src.endpoints) == 1
    assert ep.body_example["query"] == "mcp"
    assert len(ep.response_example["hits"]) == 2
    assert "28f0e1ec" not in json.dumps(src.model_dump())  # key from the capture URL is not kept


# ---------- audit regressions ----------


def _entry(method, url, status=200, text="{}", mime="application/json", headers=None, post=None):
    req = {"method": method, "url": url, "headers": headers or []}
    if post is not None:
        req["postData"] = post
    return {"request": req, "response": {"status": status, "content": {"mimeType": mime, "text": text}}}


def test_referer_keeps_only_the_site_and_ids_in_header_values_are_dropped():
    headers = [
        {"name": "Referer", "value": "https://ex.com/invite/Zq8Lm2Np5Rs7Tv9Wx1Yz/accept?x=1"},
        {"name": "Origin", "value": "https://user:pw@ex.com"},
        {"name": "X-Tenant", "value": "acme-corp"},
        {"name": "X-Context", "value": "org/Ab9xQ2kLm7Pz4Rt8Yw3e/main"},
    ]
    src = har.parse(
        {"log": {"entries": [_entry("GET", f"https://api.ex.com/v1/p{i}", headers=headers) for i in range(3)]}}
    )
    assert src.headers == {"referer": "https://ex.com", "origin": "https://ex.com", "x-tenant": "acme-corp"}


SECRET_NAMES = [
    *"clientsecret userpassword appkey accesskey privatekey authkey passphrase token2 X-App-Key".split(),
    *"access_key_id client_secret cvv pin apiToken".split(),
]
ORDINARY_NAMES = [
    *"keyboard monkey keyword passenger_count compass author description inside design hockey".split(),
    *"shipping postcode title price page".split(),
]


@pytest.mark.parametrize("name", SECRET_NAMES)
def test_secret_compound_names(name):
    assert har._is_secret(name)


@pytest.mark.parametrize("name", ORDINARY_NAMES)
def test_ordinary_names_are_not_secret(name):
    assert not har._is_secret(name)


def test_scrub_name_value_pairs_and_keys():
    out = har._scrub(
        {
            "fields": [
                {"name": "api_token", "value": "SECRETPAIR1"},
                {"key": "client_secret", "value": "SECRETPAIR2"},
                {"name": "email", "value": "pair@leak.com"},
                {"name": "color", "value": "red"},
            ],
            "byUser": {"jane@leak.com": 3, "+989121234567": 1},
        }
    )
    assert out["fields"] == [
        {"name": "api_token", "value": "<redacted>"},
        {"key": "<redacted>", "value": "<redacted>"},  # a field called "key" was already masked
        {"name": "email", "value": "user@example.com"},
        {"name": "color", "value": "red"},
    ]
    assert "jane@leak.com" not in json.dumps(out) and "9121234567" not in json.dumps(out)


def test_har_form_and_multipart_bodies():
    form = {"mimeType": "application/x-www-form-urlencoded", "text": "user=bob&remember=1&tag=a&tag=b&password=pw9"}
    boundary = "----B"
    multipart = (
        f'--{boundary}\r\nContent-Disposition: form-data; name="title"\r\n\r\nHello\r\n'
        f'--{boundary}\r\nContent-Disposition: form-data; name="avatar"; filename="me.png"\r\n'
        f"Content-Type: image/png\r\n\r\nPNGDATA\r\n--{boundary}--\r\n"
    )
    entries = [
        _entry("POST", "https://x.example.com/v1/login", post=form),
        _entry(
            "POST",
            "https://x.example.com/v1/upload",
            post={"mimeType": f"multipart/form-data; boundary={boundary}", "text": multipart},
        ),
        _entry(
            "POST",
            "https://x.example.com/v1/note",
            post={
                "mimeType": "multipart/form-data",
                "params": [{"name": "n", "value": "hi"}, {"name": "f", "fileName": "a.txt"}],
            },
        ),
        _entry("POST", "https://x.example.com/v1/json", post={"mimeType": "application/json", "text": '{"a": 1}'}),
    ]
    eps = by_id(har.parse({"log": {"entries": entries}}))
    login = eps["POST /v1/login"]
    assert login.body_encoding == "form"
    assert login.body_example == {"user": "bob", "remember": "1", "tag": ["a", "b"], "password": "<redacted>"}
    up = eps["POST /v1/upload"]
    assert (up.body_encoding, up.body_example) == ("multipart", {"title": "Hello"})
    assert "avatar" in up.description and "PNGDATA" not in up.model_dump_json()
    note = eps["POST /v1/note"]
    assert (note.body_encoding, note.body_example) == ("multipart", {"n": "hi"}) and "f" in note.description
    assert eps["POST /v1/json"].body_encoding == "json"


def test_har_graphql_operations_are_separate_endpoints():
    def gql(body):
        return _entry(
            "POST", "https://x.example.com/graphql", post={"mimeType": "application/json", "text": json.dumps(body)}
        )

    entries = [
        gql(
            {
                "operationName": "GetUser",
                "query": "query GetUser($id: ID!) { user(id: $id) { name } }",
                "variables": {"id": 1},
            }
        ),
        gql(
            {
                "operationName": "GetUser",
                "query": "query GetUser($id: ID!) { user(id: $id) { name } }",
                "variables": {"id": 2},
            }
        ),
        gql({"query": "mutation AddItem { addItem { id } }"}),
        gql({"query": "{ me { id } }"}),
        _entry("GET", "https://x.example.com/graphql?query=query%20Ping%20%7B%20ping%20%7D"),
        _entry(
            "POST", "https://x.example.com/search", post={"mimeType": "application/json", "text": '{"query": "shoes"}'}
        ),
    ]
    eps = by_id(har.parse({"log": {"entries": entries}}))
    anon = next(
        i for i in eps if i.startswith("POST /graphql#") and i not in ("POST /graphql#GetUser", "POST /graphql#AddItem")
    )
    assert set(eps) == {"POST /graphql#GetUser", "POST /graphql#AddItem", anon, "GET /graphql#Ping", "POST /search"}
    assert len(anon) == len("POST /graphql#") + 8  # a short hash of the query
    assert eps["POST /graphql#GetUser"].count == 2 and eps["POST /graphql#GetUser"].path == "/graphql"
    assert "addItem" in eps["POST /graphql#AddItem"].body_example["query"]


def _ref_doc(schemas, paths):
    return {"openapi": "3.0.0", "info": {"title": "t"}, "paths": paths, "components": {"schemas": schemas}}


def _get(ref):
    return {"get": {"responses": {"200": {"content": {"application/json": {"schema": {"$ref": ref}}}}}}}


def test_deref_dense_graph_is_fast_and_small():
    n, fan = 40, 3
    schemas = {
        f"S{i}": {
            "type": "object",
            "properties": {f"p{j}": {"$ref": f"#/components/schemas/S{(i + j + 1) % n}"} for j in range(fan)},
        }
        for i in range(n)
    }
    doc = _ref_doc(schemas, {f"/r{i}": _get(f"#/components/schemas/S{i % n}") for i in range(200)})
    start = time.perf_counter()
    src = openapi.parse(doc)
    assert time.perf_counter() - start < 2 and len(src.model_dump_json()) < 5_000_000  # was 63 s and 137 MB


def test_deref_deep_chain_and_cycle_can_be_saved(tmp_path):
    from toolsmelt.model import save_source

    chain = {
        f"S{i}": {"type": "object", "properties": {"n": {"$ref": f"#/components/schemas/S{i + 1}"}}} for i in range(200)
    }
    chain["S200"] = {"type": "string"}
    openapi.parse(_ref_doc(chain, {"/a": _get("#/components/schemas/S0")}))  # was RecursionError
    cycle = {
        f"S{i}": {"type": "object", "properties": {"next": {"$ref": f"#/components/schemas/S{(i + 1) % 60}"}}}
        for i in range(60)
    }
    src = openapi.parse(_ref_doc(cycle, {"/a": _get("#/components/schemas/S0")}))
    save_source(src, tmp_path / "source.json")  # was "Circular reference detected"


def test_openapi_body_encodings_and_file_parts():
    doc = {
        "openapi": "3.0.0",
        "info": {"title": "t"},
        "servers": [{"url": "https://x.example.com"}],
        "paths": {
            "/token": {
                "post": {
                    "requestBody": {
                        "content": {
                            "application/x-www-form-urlencoded": {
                                "schema": {"type": "object", "properties": {"grant_type": {"type": "string"}}}
                            }
                        }
                    },
                    "responses": {},
                }
            },
            "/upload": {
                "post": {
                    "requestBody": {
                        "content": {
                            "multipart/form-data": {
                                "schema": {
                                    "type": "object",
                                    "required": ["file", "title"],
                                    "properties": {
                                        "file": {"type": "string", "format": "binary"},
                                        "title": {"type": "string"},
                                    },
                                }
                            }
                        }
                    },
                    "responses": {},
                }
            },
            "/json": {
                "post": {
                    "requestBody": {
                        "content": {
                            "application/json": {"schema": {"type": "object"}},
                            "application/x-www-form-urlencoded": {"schema": {"type": "object"}},
                        }
                    },
                    "responses": {},
                }
            },
        },
    }
    eps = by_id(openapi.parse(doc))
    assert (eps["POST /token"].body_encoding, eps["POST /token"].body_example) == ("form", {"grant_type": "string"})
    up = eps["POST /upload"]
    assert (up.body_encoding, up.body_example, up.body_schema["required"]) == (
        "multipart",
        {"title": "string"},
        ["title"],
    )
    assert "file" in up.description and eps["POST /json"].body_encoding == "json"

    sw2 = {
        "swagger": "2.0",
        "info": {},
        "host": "x.example.com",
        "paths": {
            "/up": {
                "post": {
                    "parameters": [
                        {"name": "f", "in": "formData", "type": "file"},
                        {"name": "n", "in": "formData", "type": "string"},
                    ],
                    "responses": {},
                }
            },
            "/form": {"post": {"parameters": [{"name": "n", "in": "formData", "type": "string"}], "responses": {}}},
            "/mp": {
                "post": {
                    "consumes": ["multipart/form-data"],
                    "parameters": [{"name": "n", "in": "formData", "type": "string"}],
                    "responses": {},
                }
            },
        },
    }
    eps = by_id(openapi.parse(sw2))
    assert (eps["POST /up"].body_encoding, list(eps["POST /up"].body_schema["properties"])) == ("multipart", ["n"])
    assert "f" in eps["POST /up"].description
    assert eps["POST /form"].body_encoding == "form" and eps["POST /mp"].body_encoding == "multipart"


def test_openapi_notes_external_refs_unsupported_auth_and_relative_base():
    doc = {
        "openapi": "3.0.0",
        "info": {"title": "t"},
        "servers": [{"url": "/api/v1"}],
        "components": {
            "securitySchemes": {
                "k": {"type": "apiKey", "in": "header", "name": "X-Key"},
                "mtls": {"type": "mutualTLS"},
                "digest": {"type": "http", "scheme": "digest"},
            }
        },
        "paths": {
            "/ext": {"get": {"parameters": [{"$ref": "common.yaml#/components/parameters/Page"}], "responses": {}}},
            "/ref": {"$ref": "paths.yaml#/item"},
            "/token": {"post": {"security": [{"mtls": []}, {"k": [], "digest": []}], "responses": {}}},
        },
    }
    src = openapi.parse(doc)
    eps = by_id(src)
    assert "'/api/v1' has no host" in src.description
    assert "mtls (mutualTLS)" in src.description and "digest (http digest)" in src.description
    assert "common.yaml#/components/parameters/Page" in src.description and "paths.yaml#/item" in src.description
    assert "common.yaml" in eps["GET /ext"].description
    token = eps["POST /token"]
    assert token.auth == ["k"] and "digest, mtls" in token.description and "k + digest" in token.description


def test_openapi_response_prefers_200_with_content():
    op = {
        "responses": {
            "204": {"description": "gone"},
            "200": {
                "content": {
                    "application/json": {"schema": {"type": "object", "properties": {"x": {"type": "integer"}}}}
                }
            },
        }
    }
    doc = {
        "openapi": "3.0.0",
        "info": {},
        "servers": [{"url": "https://x.example.com"}],
        "paths": {"/d": {"delete": op}},
    }
    assert openapi.parse(doc).endpoints[0].response_example == {"x": 0}


def test_har_path_emails_opaque_values_and_oauth_query():
    entries = [
        _entry("GET", "https://x.example.com/v1/users/jane%40leak.com/profile"),
        _entry("GET", "https://x.example.com/v1/reset/Ab9xQ2kLm7Pz4Rt8Yw3e"),
        _entry("GET", "https://x.example.com/v1/callback?code=OAUTHCODE77&state=STATE88&lang=fa"),
    ]
    src = har.parse({"log": {"entries": entries}})
    eps = by_id(src)
    assert set(eps) == {"GET /v1/users/{user_id}/profile", "GET /v1/reset/{reset_id}", "GET /v1/callback"}
    assert eps["GET /v1/reset/{reset_id}"].params[0].example is None
    q = {p.name: p.example for p in eps["GET /v1/callback"].params}
    assert q == {"code": "<redacted>", "state": "<redacted>", "lang": "fa"}
    dump = src.model_dump_json()
    assert not any(s in dump for s in ("jane", "Ab9xQ2kLm7Pz4Rt8Yw3e", "OAUTHCODE77", "STATE88"))


def test_har_keeps_writes_without_a_json_answer():
    entries = [
        _entry("DELETE", "https://x.example.com/v1/items/42", status=204, text="", mime=""),
        _entry("PUT", "https://x.example.com/v1/items/42", text="ok", mime="text/plain"),
        _entry("GET", "https://x.example.com/page", text="<html>", mime="text/html"),
        _entry("POST", "https://x.example.com/v1/fail", status=500, text="oops", mime="text/plain"),
    ]
    assert set(by_id(har.parse({"log": {"entries": entries}}))) == {
        "DELETE /v1/items/{item_id}",
        "PUT /v1/items/{item_id}",
    }


@pytest.mark.parametrize(
    "name, text",
    [
        (
            "bad.har",
            json.dumps(
                {
                    "log": {
                        "entries": [
                            {
                                "request": {"url": "https://x.example.com/a"},
                                "response": {"status": 200, "content": {"mimeType": "application/json", "text": "{}"}},
                            }
                        ]
                    }
                }
            ),
        ),
        (
            "bad.json",
            json.dumps({"openapi": "3.0.0", "info": {}, "paths": {"/a": {"get": {"parameters": [{"in": "query"}]}}}}),
        ),
        (
            "bad-rea.json",
            json.dumps(
                {
                    "predicate_type": "rea.browser-scenario-capture",
                    "normalized_result": {"events": {"items": [{"transaction_id": 1, "kind": "request"}]}},
                }
            ),
        ),
    ],
)
def test_malformed_input_is_a_value_error(tmp_path, name, text):
    (tmp_path / name).write_text(text, encoding="utf-8")
    with pytest.raises(ValueError, match="malformed") as e:
        importers.load(str(tmp_path / name))
    assert name in str(e.value)  # the error names the file
