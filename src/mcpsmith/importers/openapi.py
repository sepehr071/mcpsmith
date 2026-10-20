"""OpenAPI 3.x and Swagger 2.0 documents -> Source."""

from __future__ import annotations

from typing import Any
from urllib.parse import unquote, urljoin, urlparse

from mcpsmith.model import AuthScheme, Endpoint, Param, Source

METHODS = ("get", "post", "put", "patch", "delete")  # HEAD/OPTIONS/TRACE skipped
TYPES = {"string", "integer", "number", "boolean", "array", "object"}
FORMATS = {
    "date": "2024-01-01",
    "date-time": "2024-01-01T00:00:00Z",
    "time": "12:00:00",
    "email": "user@example.com",
    "uri": "https://example.com",
    "url": "https://example.com",
    "uuid": "00000000-0000-0000-0000-000000000000",
}
MAX_DEPTH = 6
MAX_REFS = 5000  # ref expansions per _deref call


def parse(doc: dict, origin: str | None = None) -> Source:
    swagger2 = "swagger" in doc
    info = doc.get("info") or {}
    schemes = _auth_schemes(doc, swagger2)
    base_url = _swagger2_base(doc, origin) if swagger2 else _server_url((doc.get("servers") or [{}])[0], origin)
    top_security = doc.get("security")

    endpoints: list[Endpoint] = []
    ids: set[str] = set()
    for path, item in (doc.get("paths") or {}).items():
        item = _deref(item, doc)
        for method in METHODS:
            op = item.get(method)
            if not isinstance(op, dict):
                continue
            ep = _endpoint(path, method, item, op, swagger2, origin)
            if ep.base_url == base_url:
                ep.base_url = ""
            if ep.id in ids:
                ep.id = f"{method.upper()} {path}"
            ids.add(ep.id)
            security = op.get("security", top_security) or []
            ep.auth = list(dict.fromkeys(name for req in security for name in req if name in schemes))
            endpoints.append(ep)

    return Source(
        kind="openapi",
        title=info.get("title") or urlparse(base_url).hostname or "API",
        description=info.get("description") or "",
        base_url=base_url,
        auth=list(schemes.values()),
        endpoints=endpoints,
    )


def _endpoint(path: str, method: str, item: dict, op: dict, swagger2: bool, origin: str | None) -> Endpoint:
    """`item` and `op` are already dereferenced."""
    raw = {(p.get("name"), p.get("in")): p for p in item.get("parameters") or []}
    raw.update({(p.get("name"), p.get("in")): p for p in op.get("parameters") or []})

    params: list[Param] = []
    body_schema = body_example = None
    form: dict[str, Any] = {}
    for p in raw.values():
        where = p.get("in")
        if where == "body":
            body_schema = p.get("schema") or {}
        elif where == "formData":
            form[p["name"]] = p
        elif where in ("path", "query", "header", "cookie"):
            params.append(_param(p))
    if form:
        body_schema = {
            "type": "object",
            "properties": {
                name: {k: v for k, v in p.items() if k not in ("name", "in", "required")} for name, p in form.items()
            },
            "required": [name for name, p in form.items() if p.get("required")],
        }

    if not swagger2 and op.get("requestBody"):
        media = _media(op["requestBody"].get("content"))
        body_schema = media.get("schema")
        body_example = _media_example(media)
    if body_schema is not None and body_example is None:
        body_example = example(body_schema)

    response_schema, response_example = _response(op, swagger2)

    servers = op.get("servers") or item.get("servers")
    return Endpoint(
        id=op.get("operationId") or f"{method.upper()} {path}",
        method=method.upper(),
        path=path,
        base_url=_server_url(servers[0], origin) if servers else "",
        summary=op.get("summary") or "",
        description=op.get("description") or "",
        tags=op.get("tags") or [],
        params=params,
        body_schema=body_schema,
        body_example=body_example,
        response_schema=response_schema,
        response_example=response_example,
    )


def _param(p: dict) -> Param:
    schema = p.get("schema") or p  # Swagger 2 puts type/enum/default on the parameter itself
    examples = p.get("examples")
    ex = p.get("example", schema.get("example"))
    if ex is None and isinstance(examples, dict) and examples:
        ex = (next(iter(examples.values())) or {}).get("value")
    return Param(
        name=p["name"],
        location=p["in"],
        type=_type(schema),
        required=p["in"] == "path" or bool(p.get("required")),
        description=p.get("description") or schema.get("description") or "",
        enum=schema.get("enum") or (schema.get("items") or {}).get("enum"),
        default=schema.get("default"),
        example=ex,
    )


def _response(op: dict, swagger2: bool) -> tuple[dict | None, Any]:
    responses = op.get("responses") or {}
    resp = next((r for code, r in responses.items() if str(code).startswith("2")), responses.get("default"))
    if not resp:
        return None, None
    if swagger2:
        schema = resp.get("schema")
        examples = resp.get("examples") or {}
        ex = next((v for k, v in examples.items() if "json" in k), None)
    else:
        media = _media(resp.get("content"))
        schema = media.get("schema")
        ex = _media_example(media)
    if ex is None and schema is not None:
        ex = example(schema)
    return schema, ex


def _media(content: dict | None) -> dict:
    """The JSON media type of a content map; else the first one (the contract has no content type)."""
    content = content or {}
    key = next((k for k in content if "json" in k), next(iter(content), None))
    return content.get(key) or {}


def _media_example(media: dict) -> Any:
    if "example" in media:
        return media["example"]
    examples = media.get("examples")
    if isinstance(examples, dict) and examples:
        return (next(iter(examples.values())) or {}).get("value")
    return None


def _type(schema: dict) -> str:
    t = schema.get("type")
    if isinstance(t, list):  # OpenAPI 3.1: ["string", "null"]
        t = next((x for x in t if x != "null"), None)
    if t in TYPES:
        return t
    if "properties" in schema:
        return "object"
    if "items" in schema:
        return "array"
    return "string"


def example(schema: Any, depth: int = 0) -> Any:
    """A plausible value for a (dereferenced) JSON schema: one item per array, format-aware strings."""
    if not isinstance(schema, dict) or depth > MAX_DEPTH or "$ref" in schema:
        return None
    for key in ("example", "default", "const"):
        if key in schema:
            return schema[key]
    if isinstance(schema.get("examples"), list) and schema["examples"]:
        return schema["examples"][0]
    if schema.get("enum"):
        return schema["enum"][0]
    if schema.get("allOf"):
        merged: dict = {}
        for sub in schema["allOf"]:
            ex = example(sub, depth + 1)
            if isinstance(ex, dict):
                merged.update(ex)
        return merged
    for key in ("oneOf", "anyOf"):
        if schema.get(key):
            return example(schema[key][0], depth + 1)
    t = _type(schema)
    if t == "object":
        out = {k: example(v, depth + 1) for k, v in (schema.get("properties") or {}).items()}
        if not out and isinstance(schema.get("additionalProperties"), dict):
            out = {"key": example(schema["additionalProperties"], depth + 1)}
        return out
    if t == "array":
        item = example(schema.get("items"), depth + 1)
        return [] if item is None else [item]
    if t == "integer":
        return 0
    if t == "number":
        return 0.0
    if t == "boolean":
        return True
    return FORMATS.get(schema.get("format"), "string")


def _deref(node: Any, root: dict, seen: tuple[str, ...] = (), budget: list[int] | None = None) -> Any:
    """Resolve local `#/...` refs recursively; a ref inside itself, or past MAX_REFS expansions, stays as `{"$ref": ...}`."""
    if budget is None:
        budget = [MAX_REFS]  # refs that each point at the next one twice would otherwise expand to 2^depth nodes
    if isinstance(node, list):
        return [_deref(v, root, seen, budget) for v in node]
    if not isinstance(node, dict):
        return node
    ref = node.get("$ref")
    if isinstance(ref, str) and ref.startswith("#/"):
        if ref in seen or budget[0] <= 0:
            return {"$ref": ref}
        target = _pointer(root, ref)
        if not isinstance(target, dict):
            return {"$ref": ref}
        budget[0] -= 1
        siblings = {k: v for k, v in node.items() if k != "$ref"}  # 3.1 allows description etc. next to $ref
        return _deref({**target, **siblings}, root, (*seen, ref), budget)
    return {k: _deref(v, root, seen, budget) for k, v in node.items()}


def _pointer(root: dict, ref: str) -> Any:
    node: Any = root
    for part in ref[2:].split("/"):
        part = unquote(part).replace("~1", "/").replace("~0", "~")
        if isinstance(node, list) and part.isdigit():
            node = node[int(part)] if int(part) < len(node) else None
        elif isinstance(node, dict):
            node = node.get(part)
        else:
            return None
    return node


def _server_url(server: dict, origin: str | None) -> str:
    url = server.get("url") or "/"
    for name, var in (server.get("variables") or {}).items():
        url = url.replace("{" + name + "}", str(var.get("default", "")))
    if origin and not urlparse(url).scheme:
        url = urljoin(origin, url)
    return url.rstrip("/")


def _swagger2_base(doc: dict, origin: str | None) -> str:
    o = urlparse(origin or "")
    schemes = doc.get("schemes") or []
    scheme = "https" if "https" in schemes else (schemes[0] if schemes else o.scheme or "https")
    host = doc.get("host") or o.netloc
    base = (doc.get("basePath") or "").rstrip("/")
    return f"{scheme}://{host}{base}" if host else base


def _auth_schemes(doc: dict, swagger2: bool) -> dict[str, AuthScheme]:
    defs = doc.get("securityDefinitions") if swagger2 else (doc.get("components") or {}).get("securitySchemes")
    out: dict[str, AuthScheme] = {}
    for name, s in (defs or {}).items():
        s = _deref(s, doc)
        kind = s.get("type")
        if kind == "apiKey" and s.get("in") in ("header", "query", "cookie"):
            out[name] = AuthScheme(name=name, type=s["in"], param=s.get("name", ""))
        elif kind == "basic" or (kind == "http" and str(s.get("scheme", "")).lower() == "basic"):
            out[name] = AuthScheme(name=name, type="basic")
        elif kind in ("oauth2", "openIdConnect") or (kind == "http" and str(s.get("scheme", "")).lower() == "bearer"):
            out[name] = AuthScheme(name=name, type="bearer")
    return out
