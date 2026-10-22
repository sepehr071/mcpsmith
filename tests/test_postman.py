import json
from pathlib import Path

import pytest

from mcpsmith import importers
from mcpsmith.importers import postman

FIXTURES = Path(__file__).parent / "fixtures" / "postman"


def by_id(source):
    return {e.id: e for e in source.endpoints}


def test_collection_v21():
    src = importers.load(str(FIXTURES / "shop.postman_collection.json"))
    assert (src.kind, src.title) == ("postman", "Example Shop")
    assert src.base_url == "https://api.shop.example.com/v1"  # {{baseUrl}} with its path prefix
    assert {a.name: (a.type, a.param) for a in src.auth} == {
        "bearer": ("bearer", ""),
        "access_token": ("query", "access_token"),
        "x-api-key": ("header", "x-api-key"),
        "X-Shop-Key": ("header", "X-Shop-Key"),
        "basic": ("basic", ""),
    }
    eps = by_id(src)
    assert set(eps) == {
        "GET /products",
        "GET /products/{productId}",
        "POST /orders/{orderGroup}/items",
        "POST /orders/{orderId}/cancel",
        "POST /receipts",
        "GET https://status.example.com/ping",
        "GET /signed",
    }

    search = eps["GET /products"]
    assert search.tags == ["Products"] and search.summary == "Search products"
    params = {p.name: p for p in search.params}
    assert (params["page"].location, params["page"].type, params["page"].example) == ("query", "integer", 2)
    assert params["q"].required and not params["debug"].required  # disabled in Postman: optional
    assert params["X-Client-Version"].location == "header" and "access_token" not in params
    assert search.auth == ["bearer", "access_token", "x-api-key"]  # collection bearer inherited
    assert search.response_example["items"][0]["seller_email"] == "user@example.com"

    get = eps["GET /products/{productId}"]
    assert [(p.name, p.location, p.example) for p in get.params] == [("productId", "path", 7)]
    assert get.response_example == {"id": 7, "title": "Desk lamp"}  # the 200 example, not the 404

    create = eps["POST /orders/{orderGroup}/items"]
    assert create.auth == ["X-Shop-Key"]  # folder auth overrides the collection's
    assert create.body_example == {
        "product_id": None,
        "quantity": 2,
        "email": "user@example.com",
        "payment_token": "<redacted>",
    }
    assert [p.example for p in create.params] == [None]  # {{orderGroup}}: no example value

    cancel = eps["POST /orders/{orderId}/cancel"]
    assert cancel.body_encoding == "form" and cancel.body_example == {
        "reason": "changed my mind",
        "password": "<redacted>",
    }

    receipt = eps["POST /receipts"]
    assert receipt.auth == []  # noauth
    assert receipt.body_encoding == "multipart" and receipt.body_example == {"note": "paper copy"}
    assert "scan" in receipt.description

    assert eps["GET https://status.example.com/ping"].auth == ["basic"]
    assert "'awsv4' not supported" in src.description


def test_no_secret_survives():
    text = importers.load(str(FIXTURES / "shop.postman_collection.json")).model_dump_json()
    for leak in ("FAKE", "SECRET", "buyer@", "seller@", "owner@"):
        assert leak not in text, leak


def test_host_variable_without_value():
    src = importers.load(str(FIXTURES / "nohost.postman_collection.json"))
    assert src.base_url == "" and "{{host}}" in src.description and "base_url" in src.description
    eps = by_id(src)
    assert set(eps) == {"GET /notes", "GET /notes/{noteId}"}
    assert src.auth[0].param == "x-session-token" and eps["GET /notes"].auth == ["x-session-token"]
    assert "FAKESESSION" not in src.model_dump_json()


def test_sniff_and_malformed():
    assert not postman.sniff({"openapi": "3.0.0", "info": {"title": "x"}})
    with pytest.raises(ValueError, match="malformed Postman"):
        postman.parse({"info": {"schema": postman.SCHEMA}, "item": [{"request": {"url": {"query": 5}}}]})


def test_postman_detected_before_openapi(tmp_path):
    doc = json.loads((FIXTURES / "nohost.postman_collection.json").read_text(encoding="utf-8"))
    doc["openapi"] = "3.0.0"  # a stray key must not send it to the OpenAPI importer
    f = tmp_path / "c.json"
    f.write_text(json.dumps(doc), encoding="utf-8")
    assert importers.load(str(f)).kind == "postman"
