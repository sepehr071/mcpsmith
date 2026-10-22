"""Postman Collection v2.0 / v2.1 -> Source: every request in every folder, auth schemes by type, never values."""

from __future__ import annotations

import re
from collections import Counter
from typing import Any
from urllib.parse import parse_qsl

from mcpsmith.importers.android import strip_userinfo
from mcpsmith.importers.har import (
    BROWSER_PREFIXES,
    SKIP_HEADERS,
    _convert,
    _infer,
    _is_credential,
    _is_secret,
    _json,
    _post_body,
    _scrub,
    _trim,
    cap_notes,
)
from mcpsmith.model import AuthScheme, Endpoint, Param, Source

SCHEMA = "getpostman.com/json/collection"
VAR = re.compile(r"\{\{\s*([^{}]+?)\s*\}\}")
ORIGIN = re.compile(r"([A-Za-z][A-Za-z0-9+.-]*://[^/?#]*)(.*)", re.S)


def sniff(doc: Any) -> bool:
    return isinstance(doc, dict) and SCHEMA in str((doc.get("info") or {}).get("schema", ""))


def parse(doc: dict) -> Source:
    try:
        return _parse(doc)
    except (KeyError, TypeError, AttributeError, IndexError) as e:
        raise ValueError(f"malformed Postman collection ({type(e).__name__}: {e})") from e


def _parse(doc: dict) -> Source:
    info = doc["info"]
    variables = {v["key"]: v.get("value") for v in doc.get("variable") or [] if isinstance(v, dict) and v.get("key")}
    notes: list[str] = []
    schemes: dict[str, AuthScheme] = {}
    # (base, unset, endpoint): base None when the host is a variable with no value, `unset` names it
    found: list[tuple[str | None, str, Endpoint]] = []
    _walk(doc.get("item") or [], [], doc.get("auth"), variables, schemes, found, notes)

    bases = Counter(b or "" for b, _, _ in found)
    base_url = bases.most_common(1)[0][0] if bases else ""
    if not base_url and found:
        unset = sorted({u for b, u, _ in found if b is None})
        notes.insert(
            0,
            f"base_url unknown: the requests' host ({', '.join(unset)}) has no value in the collection; "
            "set the real URL as the plan's base_url.",
        )
    endpoints: list[Endpoint] = []
    seen: dict[str, Endpoint] = {}
    for base, unset, ep in found:
        if (base or "") != base_url:
            if base is None:
                notes.append(f"{ep.id} ({ep.summary}): its host {unset} has no value, left out")
                continue
            ep.base_url = base
            ep.id = f"{ep.method} {base}{ep.path}"
        if ep.id in seen:  # the same call saved twice (often with other examples)
            seen[ep.id].count += 1
            continue
        seen[ep.id] = ep
        endpoints.append(ep)

    return Source(
        kind="postman",
        title=str(info.get("name") or "API"),
        description="\n".join(x for x in (_scrub(_text(info.get("description"))), *cap_notes(notes)) if x),
        base_url=base_url,
        auth=list(schemes.values()),
        endpoints=endpoints,
    )


def _walk(items, tags, auth, variables, schemes, found, notes) -> None:
    for item in items:
        if not isinstance(item, dict):
            continue
        if "item" in item:  # a folder; its auth, if any, applies to everything inside
            tag = str(item.get("name") or "")
            _walk(item["item"] or [], [*tags, tag], item.get("auth") or auth, variables, schemes, found, notes)
        elif "request" in item:
            found.append(_request(item, tags, auth, variables, schemes, notes))


def _request(item, tags, auth, variables, schemes, notes) -> tuple[str | None, str, Endpoint]:
    req = item["request"]
    if isinstance(req, str):  # v2: a bare URL is a GET
        req = {"url": req, "method": "GET"}
    method = str(req.get("method") or "GET").upper()
    raw, query, path_vars = _url(req.get("url") or "")
    base, unset, path, params = _split(raw, variables, path_vars)
    ep_notes: list[str] = []
    ep_auth: list[str] = []

    if s := _auth(req.get("auth") or auth, notes):
        schemes.setdefault(s.name, s)
        ep_auth.append(s.name)

    for q in query:
        key = q.get("key")
        if not key:
            continue
        if _is_secret(key):
            if _is_credential(key):
                schemes.setdefault(key, AuthScheme(name=key, type="query", param=key))
                ep_auth.append(key)
            continue
        params.append(_param(key, "query", q.get("value"), not q.get("disabled")))

    for h in _headers(req.get("header")):
        key, value = str(h.get("key") or ""), str(h.get("value") or "")
        low = key.lower()
        if not key or h.get("disabled"):
            continue
        if low == "authorization":
            kind = value.split(" ", 1)[0].lower()
            if kind in ("bearer", "basic"):
                s = AuthScheme(name=kind, type=kind)
            else:
                s = AuthScheme(name="authorization", type="header", param="Authorization")
        elif low in SKIP_HEADERS or low.startswith(BROWSER_PREFIXES):
            continue
        elif _is_secret(key):
            if not _is_credential(key):
                continue
            s = AuthScheme(name=low, type="header", param=low)
        else:
            params.append(_param(key, "header", value, True))
            continue
        schemes.setdefault(s.name, s)
        ep_auth.append(s.name)

    body, encoding = _body(req.get("body") or {}, ep_notes)
    return (
        base,
        unset,
        Endpoint(
            id=f"{method} {path}",
            method=method,
            path=path,
            summary=str(item.get("name") or ""),
            description="\n".join(x for x in (_scrub(_text(req.get("description"))), *ep_notes) if x),
            tags=[t for t in tags if t],
            params=params,
            body_example=body,
            body_encoding=encoding if body is not None else "json",
            response_example=_response(item.get("response") or []),
            auth=list(dict.fromkeys(ep_auth)),
        ),
    )


def _url(url: Any) -> tuple[str, list[dict], list[dict]]:
    """(URL without query, query items, path variables) of a request url (a string or an object)."""
    if isinstance(url, str):
        url = {"raw": url}
    raw = url.get("raw") or ""
    if not raw:
        host = url.get("host") or ""
        host = ".".join(map(str, host)) if isinstance(host, list) else str(host)
        path = url.get("path") or ""
        if isinstance(path, list):
            path = "/".join(p.get("value", "") if isinstance(p, dict) else str(p) for p in path)
        raw = (f"{url['protocol']}://" if url.get("protocol") else "") + host
        raw += (f":{url['port']}" if url.get("port") else "") + "/" + path.lstrip("/")
    query = url.get("query")
    if query is None:
        query = [{"key": k, "value": v} for k, v in parse_qsl(raw.partition("?")[2], keep_blank_values=True)]
    return raw.split("?")[0].split("#")[0], [q for q in query if isinstance(q, dict)], url.get("variable") or []


def _split(raw: str, variables: dict, path_vars: list) -> tuple[str | None, str, str, list[Param]]:
    """(base, or None when unknown; the variable that left it unknown; path template; path params) of a URL."""
    base: str | None = None
    unset, rest = "", raw
    if m := VAR.match(raw):  # {{baseUrl}}/users: the variable is the whole base, a path prefix included
        value, rest = variables.get(m[1]), raw[m.end() :]
        if isinstance(value, str) and ORIGIN.match(value) and "{{" not in value:
            base = strip_userinfo(value.rstrip("/"))
        else:
            unset = m[0]
    elif m := ORIGIN.match(raw):  # https://{{host}}/x: fill in the variables the collection gives a value
        origin, rest = VAR.sub(lambda v: str(variables.get(v[1], v[0])), m[1]), m[2]
        if v := VAR.search(origin):
            unset = v[0]
        else:
            base = strip_userinfo(origin)
    else:  # no scheme: Postman would pick one; leave the host to the user
        host, _, rest = raw.partition("/")
        unset, rest = host or "the host", "/" + rest
    values = {v.get("key"): v.get("value") for v in path_vars if isinstance(v, dict)}
    segs, params = [], []
    for seg in (s for s in rest.split("/") if s):
        if seg.startswith(":") and len(seg) > 1:  # :id
            names, seg = [seg[1:]], "{" + re.sub(r"\W", "_", seg[1:]) + "}"
        else:  # {{id}}, also inside a segment ('v{{version}}')
            names = VAR.findall(seg)
            seg = VAR.sub(lambda v: "{" + re.sub(r"\W", "_", v[1]) + "}", seg)
        params += [_param(re.sub(r"\W", "_", n), "path", values.get(n), True) for n in names]
        segs.append(seg)
    path = "/" + "/".join(segs) + ("/" if rest.endswith("/") and segs else "")
    return base, unset, path, params


def _param(name: str, location: str, value: Any, required: bool) -> Param:
    if value is None or "{{" in str(value):  # a variable, not an example
        return Param(name=name, location=location, required=required)
    t = _infer([str(value)])
    return Param(name=name, location=location, type=t, required=required, example=_scrub(_convert(str(value), t), name))


def _headers(header: Any) -> list[dict]:
    if isinstance(header, str):  # v2 allows "Name: value" lines
        return [{"key": k.strip(), "value": v.strip()} for k, _, v in (ln.partition(":") for ln in header.splitlines())]
    return [h for h in header or [] if isinstance(h, dict)]


def _auth(auth: Any, notes: list[str]) -> AuthScheme | None:
    """The scheme of a Postman auth object. Postman stores the values too; they are never read."""
    if not isinstance(auth, dict) or auth.get("type") in (None, "noauth", "inherit"):
        return None
    kind = auth["type"]
    if kind in ("bearer", "basic"):
        return AuthScheme(name=kind, type=kind)
    if kind == "apikey":
        attrs = auth.get("apikey") or []
        if not isinstance(attrs, dict):  # v2.1: [{key, value}]; v2.0: an object
            attrs = {a.get("key"): a.get("value") for a in attrs if isinstance(a, dict)}
        param = str(attrs.get("key") or "")
        if not param or "{{" in param:
            note = "an apikey auth names its key with a variable or not at all, left out"
        else:
            where = "query" if attrs.get("in") == "query" else "header"  # Postman's default is header
            return AuthScheme(name=param, type=where, param=param)
    else:
        note = f"auth type {kind!r} not supported, left out"
    if note not in notes:
        notes.append(note)
    return None


def _body(body: dict, notes: list[str]) -> tuple[Any, str]:
    mode = body.get("mode")
    if not mode or body.get("disabled"):
        return None, "json"
    if mode == "raw":
        raw = body.get("raw") or ""
        # {"id": {{id}}}: an unquoted variable is not JSON until it is replaced
        data = _json({"text": raw}) if raw.strip() else None
        if data is None and raw.strip() and (data := _json({"text": VAR.sub("null", raw)})) is None:
            notes.append("raw body is not JSON, left out")
        return _scrub(data), "json"
    if mode in ("urlencoded", "formdata"):
        fields = [f for f in body.get(mode) or [] if isinstance(f, dict) and not f.get("disabled")]
        data, encoding, files = _post_body(
            {
                "mimeType": "application/x-www-form-urlencoded" if mode == "urlencoded" else "multipart/form-data",
                "params": [
                    {"name": f.get("key"), "value": f.get("value"), "fileName": f.get("type") == "file"} for f in fields
                ],
            }
        )
        if files:
            notes.append(f"file parts left out (uploads are not supported): {', '.join(files)}")
        return _scrub(data), encoding
    notes.append(f"{mode} body not supported, left out")
    return None, "json"


def _response(responses: list) -> Any:
    """The first saved example with a JSON body, a success first; scrubbed, then trimmed."""
    bodies = [(r.get("code") or 0, _json({"text": r.get("body")})) for r in responses if isinstance(r, dict)]
    bodies = sorted(((c, b) for c, b in bodies if b is not None), key=lambda cb: not 200 <= cb[0] < 400)
    return _trim(_scrub(bodies[0][1])) if bodies else None


def _text(d: Any) -> str:
    """A Postman description: a string, or {content, type}."""
    if isinstance(d, dict):
        d = d.get("content")
    return d if isinstance(d, str) else ""
