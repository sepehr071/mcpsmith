"""OAuth 2.1 resource server for Streamable HTTP: accept bearer JWTs from the user's identity provider.

Env: `MCP_OAUTH_ISSUER` (turns it on), `MCP_OAUTH_AUDIENCE` (default: the resource URL), `MCP_OAUTH_JWKS_URL`
(default: from the issuer's discovery document), `MCP_OAUTH_SCOPES` (comma list every request needs) and
`MCP_PUBLIC_URL` (the resource URL clients use, default http://HOST:PORT/mcp). The mcp SDK answers 401/403 with a
WWW-Authenticate header and serves the RFC 9728 metadata at /.well-known/oauth-protected-resource/mcp.
"""

from __future__ import annotations

import logging
import os
import time
from typing import Any
from urllib.parse import urlparse

import httpx
import jwt
from mcp.server.auth.provider import AccessToken
from mcp.server.auth.settings import AuthSettings

from .http import tls_context

log = logging.getLogger("toolsmelt")

ALGORITHMS = ("RS256", "ES256")  # asymmetric only: never "none" or HS* (the public JWK must not be a shared secret)
LEEWAY = 30  # seconds of clock skew allowed on nbf/iat (the SDK re-checks exp without leeway)
REFETCH_AFTER = 30  # an unknown kid refetches the JWKS at most this often, so random kids cannot flood the IdP
LOOPBACK = {"127.0.0.1", "localhost", "::1"}


def _http() -> dict[str, Any]:
    return {"trust_env": False, "verify": tls_context(), "timeout": 10}


def _secure(url: str, what: str) -> str:
    u = urlparse(url)
    if u.scheme == "https" or (u.scheme == "http" and u.hostname in LOOPBACK):
        return url
    raise ValueError(f"{what} {url!r} must be an https URL (http only on loopback)")


def discover_jwks(issuer: str) -> str:
    """The issuer's `jwks_uri`, from its OpenID Connect or RFC 8414 discovery document."""
    u = urlparse(issuer)
    urls = [
        issuer.rstrip("/") + "/.well-known/openid-configuration",
        f"{u.scheme}://{u.netloc}/.well-known/oauth-authorization-server{u.path.rstrip('/')}",
    ]
    errors = []
    with httpx.Client(**_http()) as http:
        for url in urls:
            try:
                r = http.get(url)
                r.raise_for_status()
                doc = r.json()
            except (httpx.HTTPError, ValueError) as e:
                errors.append(f"{url}: {e}")
                continue
            if not isinstance(doc, dict) or doc.get("issuer") != issuer:
                errors.append(f"{url}: its issuer is not {issuer!r}")
            elif not isinstance(doc.get("jwks_uri"), str):
                errors.append(f"{url}: no jwks_uri")
            else:
                return doc["jwks_uri"]
    raise ValueError(f"cannot find the JWKS of {issuer} (set MCP_OAUTH_JWKS_URL): " + "; ".join(errors))


class JwtVerifier:
    """The SDK's TokenVerifier: a JWT signed by a key of `jwks_url`, issued by `issuer` for `audience`."""

    def __init__(self, issuer: str, audience: str, jwks_url: str) -> None:
        self.issuer, self.audience, self.jwks_url = issuer, audience, jwks_url
        self._keys: dict[str | None, jwt.PyJWK] = {}
        self._fetched = float("-inf")

    async def _fetch_jwks(self) -> Any:
        async with httpx.AsyncClient(**_http()) as http:
            r = await http.get(self.jwks_url)
            r.raise_for_status()
            return r.json()

    async def _key(self, kid: str | None) -> jwt.PyJWK | None:
        if kid not in self._keys and time.monotonic() - self._fetched >= REFETCH_AFTER:
            self._fetched = time.monotonic()
            try:
                data = await self._fetch_jwks()
            except (httpx.HTTPError, ValueError) as e:
                log.warning("cannot fetch the JWKS %s: %s", self.jwks_url, e)
                return self._keys.get(kid)
            keys = {}
            for k in data.get("keys", []) if isinstance(data, dict) else []:
                if not isinstance(k, dict) or k.get("use", "sig") != "sig":
                    continue
                try:
                    jwk = jwt.PyJWK(k)
                except jwt.PyJWTError:
                    continue  # a key type or curve we do not accept
                if jwk.algorithm_name in ALGORITHMS:
                    keys[k.get("kid")] = jwk
            self._keys = keys
        return self._keys.get(kid)

    async def verify_token(self, token: str) -> AccessToken | None:
        try:
            header = jwt.get_unverified_header(token)
            if header.get("alg") not in ALGORITHMS or not (key := await self._key(header.get("kid"))):
                return None
            claims = jwt.decode(
                token,
                key.key,
                algorithms=[key.algorithm_name],  # the header's alg must match the key's
                audience=self.audience,
                issuer=self.issuer,
                leeway=LEEWAY,
                options={"require": ["exp", "iss", "aud"]},
            )
        except jwt.PyJWTError as e:
            log.info("bearer token rejected: %s", e)
            return None
        scope = claims.get("scope", claims.get("scp", ""))
        scopes = scope.split() if isinstance(scope, str) else [str(s) for s in scope]
        return AccessToken(
            token=token,
            client_id=str(claims.get("client_id") or claims.get("azp") or claims.get("sub") or ""),
            scopes=scopes,
            expires_at=int(claims["exp"]),
            resource=self.audience,
            subject=claims.get("sub"),
            claims=claims,
        )


def from_env(host: str, port: int) -> tuple[AuthSettings, JwtVerifier]:
    """Auth settings and verifier from the MCP_OAUTH_* env (MCP_OAUTH_ISSUER must be set); ValueError if invalid."""
    issuer = _secure(os.environ["MCP_OAUTH_ISSUER"], "MCP_OAUTH_ISSUER")
    netloc = f"[{host}]" if ":" in host else host
    resource = os.environ.get("MCP_PUBLIC_URL") or f"http://{netloc}:{port}/mcp"
    scopes = [s.strip() for s in os.environ.get("MCP_OAUTH_SCOPES", "").split(",") if s.strip()]
    jwks = _secure(os.environ.get("MCP_OAUTH_JWKS_URL") or discover_jwks(issuer), "MCP_OAUTH_JWKS_URL")
    settings = AuthSettings(
        issuer_url=issuer,
        resource_server_url=resource,
        required_scopes=scopes or None,
        validate_token_resource=False,  # JwtVerifier checks aud itself
    )
    return settings, JwtVerifier(issuer, os.environ.get("MCP_OAUTH_AUDIENCE") or resource, jwks)
