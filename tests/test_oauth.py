import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import ec, rsa
from test_server_transport import INIT, FakeServer, _first, _hub, _rpc, _stop

from toolsmelt.runtime import run
from toolsmelt.runtime.oauth import JwtVerifier, from_env

ISS = "https://idp.example"
AUD = "http://127.0.0.1:8000/mcp"


def _key(kid, alg="RS256"):
    """A private key and its public JWK (with kid)."""
    priv = (
        rsa.generate_private_key(public_exponent=65537, key_size=2048)
        if alg == "RS256"
        else ec.generate_private_key(ec.SECP256R1())
    )
    algo = jwt.get_algorithm_by_name(alg)
    return priv, {**json.loads(algo.to_jwk(priv.public_key())), "kid": kid, "use": "sig", "alg": alg}


K1 = _key("k1")
K2 = _key("k2")
E1 = _key("e1", "ES256")


def _token(key=K1, alg=None, **claims):
    priv, jwk = key
    body = {"iss": ISS, "aud": AUD, "sub": "user-1", "exp": int(time.time()) + 300, "scope": "mcp:tools"} | claims
    return jwt.encode(
        {k: v for k, v in body.items() if v is not None}, priv, algorithm=alg or jwk["alg"], headers={"kid": jwk["kid"]}
    )


def _verifier(*keys):
    v = JwtVerifier(ISS, AUD, "https://idp.example/jwks")
    v.fetches = 0

    async def fetch():
        v.fetches += 1
        return {"keys": [k[1] for k in keys]}

    v._fetch_jwks = fetch
    return v


@pytest.mark.anyio
@pytest.mark.parametrize("key", [K1, E1])
async def test_good_token(key):
    t = await _verifier(key, K2).verify_token(_token(key, scope="mcp:tools other"))
    assert t.scopes == ["mcp:tools", "other"] and t.subject == "user-1" and t.resource == AUD
    assert t.claims["iss"] == ISS


@pytest.mark.anyio
async def test_scp_list_claim():
    t = await _verifier(K1).verify_token(_token(scope=None, scp=["a", "b"]))
    assert t.scopes == ["a", "b"]


@pytest.mark.anyio
@pytest.mark.parametrize(
    "claims",
    [
        {"aud": "https://other.example/mcp"},
        {"iss": "https://evil.example"},
        {"exp": int(time.time()) - 120},
        {"exp": None},
        {"nbf": int(time.time()) + 600},
    ],
)
async def test_bad_claims_are_rejected(claims):
    assert await _verifier(K1).verify_token(_token(**claims)) is None


@pytest.mark.anyio
async def test_alg_none_hs256_and_mismatched_alg_are_rejected():
    v = _verifier(K1, E1)
    body = {"iss": ISS, "aud": AUD, "exp": int(time.time()) + 300}
    assert await v.verify_token(jwt.encode(body, None, algorithm="none", headers={"kid": "k1"})) is None
    assert await v.verify_token(jwt.encode(body, "s" * 32, algorithm="HS256", headers={"kid": "k1"})) is None
    # an ES256 header pointing at the RSA key
    assert await v.verify_token(jwt.encode(body, E1[0], algorithm="ES256", headers={"kid": "k1"})) is None
    assert await v.verify_token("not-a-jwt") is None


@pytest.mark.anyio
async def test_unknown_kid_refetches_at_most_every_30s():
    v = _verifier(K1)
    assert await v.verify_token(_token()) is not None
    assert await v.verify_token(_token(K2)) is None  # unknown kid, but fetched just now
    assert v.fetches == 1
    keys = [K1, K2]

    async def rotated():
        v.fetches += 1
        return {"keys": [k[1] for k in keys]}

    v._fetch_jwks, v._fetched = rotated, float("-inf")  # 30 s later, the IdP has a new key
    assert await v.verify_token(_token(K2)) is not None
    assert v.fetches == 2


@pytest.mark.anyio
async def test_jwks_fetch_error_keeps_known_keys():
    v = _verifier(K1)
    assert await v.verify_token(_token()) is not None

    async def down():
        raise httpx.ConnectError("down")

    v._fetch_jwks, v._fetched = down, float("-inf")
    assert await v.verify_token(_token(K2)) is None
    assert await v.verify_token(_token()) is not None


@pytest.fixture(autouse=True)
def oauth_env(monkeypatch):
    for k in ("MCP_OAUTH_ISSUER", "MCP_OAUTH_AUDIENCE", "MCP_OAUTH_JWKS_URL", "MCP_OAUTH_SCOPES", "MCP_PUBLIC_URL"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.delenv("MCP_AUTH_TOKEN", raising=False)


def test_from_env_defaults_and_overrides(monkeypatch):
    monkeypatch.setenv("MCP_OAUTH_ISSUER", ISS)
    monkeypatch.setenv("MCP_OAUTH_JWKS_URL", "https://idp.example/jwks")
    settings, v = from_env("127.0.0.1", 9000)
    assert str(settings.resource_server_url) == v.audience == "http://127.0.0.1:9000/mcp"
    assert settings.required_scopes is None and v.jwks_url == "https://idp.example/jwks"

    monkeypatch.setenv("MCP_PUBLIC_URL", "https://mcp.example/mcp")
    monkeypatch.setenv("MCP_OAUTH_AUDIENCE", "api://demo")
    monkeypatch.setenv("MCP_OAUTH_SCOPES", "mcp:tools, mcp:write")
    settings, v = from_env("0.0.0.0", 9000)
    assert str(settings.resource_server_url) == "https://mcp.example/mcp" and v.audience == "api://demo"
    assert settings.required_scopes == ["mcp:tools", "mcp:write"]


@pytest.mark.parametrize(
    "env",
    [
        {"MCP_OAUTH_ISSUER": "http://idp.example", "MCP_OAUTH_JWKS_URL": "https://idp.example/jwks"},
        {"MCP_OAUTH_ISSUER": ISS, "MCP_OAUTH_JWKS_URL": "http://idp.example/jwks"},
        {"MCP_OAUTH_ISSUER": ISS, "MCP_OAUTH_JWKS_URL": "https://idp.example/jwks", "MCP_AUTH_TOKEN": "x"},
    ],
)
def test_insecure_or_conflicting_config_is_an_error(monkeypatch, env):
    for k, val in env.items():
        monkeypatch.setenv(k, val)
    s = FakeServer()
    with pytest.raises(SystemExit):
        run(s, ["--transport", "http"])
    assert s.calls == []


class _Idp(BaseHTTPRequestHandler):
    doc: dict = {}

    def do_GET(self):
        body = self.doc.get(self.path)
        self.send_response(200 if body else 404)
        self.send_header("content-type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(body or {}).encode())

    def log_message(self, *args):
        pass


def test_hub_accepts_oauth_jwts(tmp_path):
    idp = ThreadingHTTPServer(("127.0.0.1", 0), _Idp)
    issuer = f"http://127.0.0.1:{idp.server_port}"
    _Idp.doc = {
        "/.well-known/openid-configuration": {"issuer": issuer, "jwks_uri": issuer + "/jwks"},
        "/jwks": {"keys": [K1[1]]},
    }
    thread = threading.Thread(target=idp.serve_forever, daemon=True)
    thread.start()
    proc, url = _hub(tmp_path, MCP_OAUTH_ISSUER=issuer, MCP_OAUTH_SCOPES="mcp:tools", MCP_AUTH_TOKEN="")
    headers = {"accept": "application/json, text/event-stream", "content-type": "application/json"}

    def bearer(**claims):
        return {**headers, "authorization": "Bearer " + _token(**{"iss": issuer, "aud": url} | claims)}

    try:
        with httpx.Client(trust_env=False, timeout=10) as http:
            r = _first(http, proc, url, headers)
            assert r.status_code == 401
            meta_url = url.replace("/mcp", "/.well-known/oauth-protected-resource/mcp")
            assert f'resource_metadata="{meta_url}"' in r.headers["www-authenticate"]
            assert http.post(url, json=INIT, headers=bearer(aud="https://other.example")).status_code == 401
            assert http.post(url, json=INIT, headers=bearer(scope="other")).status_code == 403
            ok = http.post(url, json=INIT, headers=bearer())
            assert ok.status_code == 200, ok.text
            assert _rpc(ok)["result"]["serverInfo"]["name"] == "toolsmelt"
            meta = http.get(meta_url).json()
            assert meta["resource"].rstrip("/") == url
            assert [s.rstrip("/") for s in meta["authorization_servers"]] == [issuer]
            assert meta["scopes_supported"] == ["mcp:tools"]
    finally:
        _stop(proc)
        idp.shutdown()
        idp.server_close()
        thread.join(timeout=5)
