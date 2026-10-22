"""OpenAPI 3.x and Swagger 2.0 documents -> Source."""

from __future__ import annotations

from typing import Any
from urllib.parse import unquote, urljoin, urlparse

from mcpsmith.importers.har import cap_notes
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
MAX_REFS = 500  # ref expansions per path item; every inlined copy counts, shared or not
MAX_REF_DEPTH = 4  # refs inside refs; deeper ones stay `{"$ref": ...}` (big specs link most schemas to each other)


def parse(doc: dict, origin: str | None = None) -> Source:
    try:
        return _parse(doc, origin)
    except (KeyError, TypeError, AttributeError, IndexError) as e:
        raise ValueError(f"malformed OpenAPI document ({type(e).__name__}: {e})") from e


def _parse(doc: dict, origin: str | None) -> Source:
    swagger2 = "swagger" in doc
    info = doc.get("info") or {}
    memo: dict = {}
    schemes, unsupported = _auth_schemes(doc, swagger2, memo)
    base_url = _swagger2_base(doc, origin) if swagger2 else _server_url((doc.get("servers") or [{}])[0], origin)
    top_security = doc.get("security")
    notes: list[str] = []
    if not urlparse(base_url).netloc:
        notes.append(f"base URL {base_url!r} has no host (a relative server URL); set the real one in the plan.")
    if unsupported:
        notes.append("auth schemes not supported, left out: " + ", ".join(f"{n} ({k})" for n, k in unsupported.items()))
    if external := _external_refs(doc):
        notes.append("external $refs not read, what they define is missing: " + ", ".join(cap_notes(external, 20)))

    endpoints: list[Endpoint] = []
    ids: set[str] = set()
    for path, item in (doc.get("paths") or {}).items():
        item = _deref(item, doc, memo=memo)
        for method in METHODS:
            op = item.get(method)
            if not isinstance(op, dict):
                continue
            ep = _endpoint(path, method, item, op, swagger2, origin, doc.get("consumes") or [])
            if ep.base_url == base_url:
                ep.base_url = ""
            if ep.id in ids:
                ep.id = f"{method.upper()} {path}"
            ids.add(ep.id)
            security = op.get("security", top_security) or []
            ep.auth = list(dict.fromkeys(name for req in security for name in req if name in schemes))
            extra = []
            if lost := sorted({name for req in security for name in req if name in unsupported}):
                extra.append(f"Note: needs auth mcpsmith does not support: {', '.join(lost)}.")
            if together := [" + ".join(req) for req in security if len(req) > 1]:
                extra.append(f"Note: these auth schemes are needed together: {'; '.join(together)}.")
            ep.description = "\n".join(x for x in (ep.description, *extra) if x)
            endpoints.append(ep)

    return Source(
        kind="openapi",
        title=info.get("title") or urlparse(base_url).hostname or "API",
        description="\n\n".join(x for x in (info.get("description") or "", "\n".join(notes)) if x),
        base_url=base_url,
        auth=list(schemes.values()),
        endpoints=endpoints,
    )


def _endpoint(
    path: str, method: str, item: dict, op: dict, swagger2: bool, origin: str | None, consumes: list[str]
) -> Endpoint:
    """`item` and `op` are already dereferenced; `consumes` is the Swagger 2 document default."""
    plist = [*(item.get("parameters") or []), *(op.get("parameters") or [])]
    raw = {(p.get("name"), p.get("in")): p for p in plist}
    notes = [f"parameter $ref {p['$ref']} not read, left out" for p in plist if "$ref" in p]

    params: list[Param] = []
    body_schema = body_example = None
    encoding = "json"
    files: list[str] = []
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
        files = [name for name, p in form.items() if _is_file(p)]
        multipart = files or "multipart/form-data" in (op.get("consumes") or consumes)
        encoding = "multipart" if multipart else "form"
        form = {name: p for name, p in form.items() if name not in files}
        body_schema = {
            "type": "object",
            "properties": {
                name: {k: v for k, v in p.items() if k not in ("name", "in", "required")} for name, p in form.items()
            },
            "required": [name for name, p in form.items() if p.get("required")],
        }

    if not swagger2 and op.get("requestBody"):
        content = op["requestBody"].get("content") or {}
        key = _media_key(content)
        media = content.get(key) or {}
        body_schema = media.get("schema")
        body_example = _media_example(media)
        if "x-www-form-urlencoded" in key:
            encoding = "form"
        elif key.startswith("multipart/"):
            encoding = "multipart"
        props = body_schema.get("properties") if isinstance(body_schema, dict) else None
        if encoding != "json" and isinstance(props, dict):
            if files := [name for name, s in props.items() if _is_file(s)]:
                body_schema = {**body_schema, "properties": {k: v for k, v in props.items() if k not in files}}
                if "required" in body_schema:
                    body_schema["required"] = [r for r in body_schema["required"] if r not in files]
                if isinstance(body_example, dict):
                    body_example = {k: v for k, v in body_example.items() if k not in files}
    if files:
        notes.append(f"file parts left out (uploads are not supported): {', '.join(files)}")
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
        description="\n".join(x for x in (op.get("description") or "", *(f"Note: {n}." for n in notes)) if x),
        tags=op.get("tags") or [],
        params=params,
        body_schema=body_schema,
        body_example=body_example,
        body_encoding=encoding,
        response_schema=response_schema,
        response_example=response_example,
    )


def _is_file(schema: Any) -> bool:
    """A file upload: Swagger 2 `type: file`, OpenAPI `format: binary`, a 3.1 `contentMediaType`, or a list of them."""
    if not isinstance(schema, dict):
        return False
    return (
        schema.get("type") == "file"
        or schema.get("format") == "binary"
        or "contentMediaType" in schema
        or _is_file(schema.get("items"))
    )


def _external_refs(doc: dict) -> list[str]:
    """`$ref`s to other files or URLs, which are not read (iterative: a deep document must not hit the stack)."""
    out: set[str] = set()
    stack: list[Any] = [doc]
    while stack:
        node = stack.pop()
        if isinstance(node, dict):
            if isinstance(ref := node.get("$ref"), str) and not ref.startswith("#"):
                out.add(ref)
            stack.extend(node.values())
        elif isinstance(node, list):
            stack.extend(node)
    return sorted(out)


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
    ok = [(str(code), r) for code, r in responses.items() if str(code).startswith("2") and isinstance(r, dict)]
    # a 204 listed before the 200 must not hide the 200's schema
    ok.sort(key=lambda cr: (not cr[1].get("schema" if swagger2 else "content"), cr[0] not in ("200", "201")))
    resp = ok[0][1] if ok else responses.get("default")
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
    return content.get(_media_key(content)) or {}


def _media_key(content: dict) -> str:
    return next((k for k in content if "json" in k), next(iter(content), ""))


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


def _deref(
    node: Any, root: dict, seen: tuple[str, ...] = (), budget: list[int] | None = None, memo: dict | None = None
) -> Any:
    """Resolve local `#/...` refs recursively. A ref inside itself, MAX_REF_DEPTH refs deep, or past MAX_REFS
    expansions stays as `{"$ref": ...}`. `memo` shares results across calls (one per document): the same component
    is resolved once, and every path item that uses it gets the same object."""
    if budget is None:
        budget = [MAX_REFS]  # refs that each point at the next one twice would otherwise expand to 2^depth nodes
    if memo is None:
        memo = {}
    if isinstance(node, list):
        return [_deref(v, root, seen, budget, memo) for v in node]
    if not isinstance(node, dict):
        return node
    ref = node.get("$ref")
    if isinstance(ref, str) and ref.startswith("#/"):
        if ref in seen or len(seen) >= MAX_REF_DEPTH or budget[0] <= 0:
            return {"$ref": ref}
        target = _pointer(root, ref)
        if not isinstance(target, dict):
            return {"$ref": ref}
        siblings = {k: v for k, v in node.items() if k != "$ref"}  # 3.1 allows description etc. next to $ref
        key = (ref, seen)
        if not siblings and key in memo and memo[key][1] <= budget[0]:
            out, cost = memo[key]
            budget[0] -= cost
            return out
        before = budget[0]
        budget[0] -= 1
        out = _deref({**target, **siblings}, root, (*seen, ref), budget, memo)
        if not siblings and budget[0] > 0:  # a result cut short by the budget is not reused
            memo[key] = (out, before - budget[0])
        return out
    return {k: _deref(v, root, seen, budget, memo) for k, v in node.items()}


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


def _auth_schemes(doc: dict, swagger2: bool, memo: dict) -> tuple[dict[str, AuthScheme], dict[str, str]]:
    """(supported schemes, {name: kind} of the ones mcpsmith cannot send, e.g. mutualTLS or http digest)."""
    defs = doc.get("securityDefinitions") if swagger2 else (doc.get("components") or {}).get("securitySchemes")
    out: dict[str, AuthScheme] = {}
    unsupported: dict[str, str] = {}
    for name, s in (defs or {}).items():
        s = _deref(s, doc, memo=memo)
        kind = s.get("type")
        if kind == "apiKey" and s.get("in") in ("header", "query", "cookie"):
            out[name] = AuthScheme(name=name, type=s["in"], param=s.get("name", ""))
        elif kind == "basic" or (kind == "http" and str(s.get("scheme", "")).lower() == "basic"):
            out[name] = AuthScheme(name=name, type="basic")
        elif kind in ("oauth2", "openIdConnect") or (kind == "http" and str(s.get("scheme", "")).lower() == "bearer"):
            out[name] = AuthScheme(name=name, type="bearer")
        else:
            unsupported[name] = " ".join(str(x) for x in (kind, s.get("scheme") or s.get("in")) if x) or "unknown"
    return out, unsupported
