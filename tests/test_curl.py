from pathlib import Path

import pytest

from toolsmelt import importers
from toolsmelt.importers import curl

FIXTURES = Path(__file__).parent / "fixtures" / "curl"


def by_id(source):
    return {e.id: e for e in source.endpoints}


def test_devtools_commands():
    src = importers.load(str(FIXTURES / "devtools.txt"))
    assert src.kind == "curl" and src.base_url == "https://api.shop.example.com"
    assert src.headers == {"x-app-version": "5.1"}
    assert {a.name: a.type for a in src.auth} == {
        "api_key": "query",
        "bearer": "bearer",
        "x-auth-token": "header",
        "basic": "basic",
    }
    assert "cookies left out" in src.description
    eps = by_id(src)
    assert set(eps) == {"GET /v1/products/{product_id}", "POST /v1/cart", "PUT /v1/login", "POST /v1/avatar"}

    product = eps["GET /v1/products/{product_id}"]
    assert product.count == 2  # two calls, one templated endpoint
    assert [(p.name, p.location) for p in product.params] == [("product_id", "path"), ("lang", "query")]

    cart = eps["POST /v1/cart"]  # $'...' quoting decoded
    assert cart.body_example == {
        "product_id": 123,
        "note": "it's a gift\nthanks",
        "refresh_token": "<redacted>",
        "email": "user@example.com",
    }

    login = eps["PUT /v1/login"]  # -X wins over the POST that --data implies
    assert login.body_encoding == "form"
    assert login.body_example == {"username": "someone", "password": "<redacted>", "remember": "1"}

    avatar = eps["POST /v1/avatar"]
    assert avatar.body_encoding == "multipart" and avatar.body_example == {"caption": "hello world"}
    assert "image" in avatar.description and avatar.auth == ["basic"]


def test_no_secret_survives():
    text = importers.load(str(FIXTURES / "devtools.txt")).model_dump_json()
    for leak in ("FAKE", "eyJ", "buyer@", "theme=dark"):
        assert leak not in text, leak


def test_get_flag_and_userinfo(tmp_path):
    f = tmp_path / "c.sh"
    f.write_text(
        "\ncurl -G 'https://me:pw@api.example.com/search' --data-urlencode 'q=a b' -d page=2\n", encoding="utf-8"
    )
    src = importers.load(str(f))
    (ep,) = src.endpoints
    assert (ep.method, ep.path) == ("GET", "/search") and ep.body_example is None
    assert {p.name: p.example for p in ep.params} == {"q": "a b", "page": 2}
    assert "pw" not in src.model_dump_json() and src.base_url == "https://api.example.com"


def test_sniff_and_errors(tmp_path):
    assert curl.sniff("# copied\n\ncurl 'https://x.example.com'")
    assert not curl.sniff("wget https://x.example.com\ncurl https://y.example.com")
    f = tmp_path / "c.txt"
    f.write_text("curl -H 'a: b'\n", encoding="utf-8")
    with pytest.raises(ValueError, match="no curl command with an http"):
        importers.load(str(f))
    f.write_text("curl 'https://x.example.com\n", encoding="utf-8")
    with pytest.raises(ValueError, match="cannot read"):
        importers.load(str(f))
