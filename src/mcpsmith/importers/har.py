"""Recorded browser traffic (HAR) -> Source: JSON API calls grouped into endpoints with templated paths."""

from __future__ import annotations

import base64
import email
import hashlib
import json
import re
from collections import Counter
from typing import Any
from urllib.parse import parse_qsl, unquote, urlsplit

from mcpsmith.model import AuthScheme, Endpoint, Param, Source

TRACKERS = (
    "google-analytics",
    "googletagmanager",
    "doubleclick",
    "googlesyndication",
    "googleadservices",
    "facebook.",
    "hotjar",
    "sentry",
    "clarity.ms",
    "yandex",
    "segment.io",
    "segment.com",
    "mixpanel",
    "amplitude",
    "newrelic",
    "nr-data.net",
    "datadoghq",
    "fullstory",
    "heapanalytics",
    "bat.bing",
    "ads.linkedin",
    "analytics.tiktok",
    "posthog",
    "plausible",
    "matomo",
    "smartlook",
    "mouseflow",
    "yektanet",
    "najva",
    "webengage",
)
STATIC = re.compile(r"\.(js|mjs|css|png|jpe?g|gif|svg|webp|avif|ico|woff2?|ttf|otf|eot|map|mp4|webm|mp3)$", re.I)
SKIP_HEADERS = {
    "cookie",
    "content-length",
    "content-type",
    "host",
    "user-agent",
    "accept-encoding",
    "connection",
    "keep-alive",
    "priority",
    "te",
    "transfer-encoding",
    "upgrade-insecure-requests",
    "dnt",
    "if-none-match",
    "if-modified-since",
    "authorization",
}
BROWSER_PREFIXES = (":", "sec-", "proxy-")
# Name classes, matched on the parts of a key (userId -> user, id). SECRET values are never stored;
# PERSONAL ones are replaced by a placeholder.
SECRET = {
    *"session sess sid token tokens key apikey secret password passwd pwd pass sig signature auth authorization".split(),
    *"ticket otp nonce csrf xsrf jwt bearer credential credentials cookie cvv cvc pin".split(),
}
PERSONAL = {
    *"email mail phone mobile tel address password national ssn birth birthday firstname lastname fullname".split()
}
PERSON_OWNERS = {"user", "account", "customer", "device", "member", "profile", "client"}
# Names written without separators, or split across parts ("first_name"), matched inside the joined name
# ("clientsecret", "x-app-key"). Only words no ordinary name contains: "key" or "pass" alone would hit monkey, compass.
SECRET_WHOLE = (
    *"token secret password passwd passphrase credential apikey appkey accesskey privatekey authkey".split(),
    *"clientkey masterkey signingkey encryptionkey secretkey sessionid".split(),
)
PERSONAL_WHOLE = (
    "firstname",
    "lastname",
    "fullname",
    "phonenumber",
    "mobilenumber",
    "emailaddress",
    "nationalid",
    "userid",
    "accountid",
    "customerid",
    "deviceid",
)
# Secrets that are not a reusable login, so they do not become an AuthScheme.
NOT_AUTH = {"csrf", "xsrf", "nonce", "otp", "ticket", "sig", "signature", "refresh"}
EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
JWT = re.compile(r"eyJ[\w-]+\.[\w-]+\.[\w-]*")
# Iranian mobiles and international numbers, in Latin, Persian or Arabic-Indic digits, with optional spaces/dashes.
_D = r"[0-9\u06F0-\u06F9\u0660-\u0669]"
_0, _9 = r"[0\u06F0\u0660]", r"[9\u06F9\u0669]"
PHONE = re.compile(
    rf"(?<!{_D})(?:(?:\+98|0098|{_0})[\s-]?{_9}(?:[\s-]?{_D}){{9}}|\+{_D}(?:[\s-]?{_D}){{9,13}})(?!{_D})"
)
UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)
OPAQUE = re.compile(r"^[A-Za-z0-9_=+-]{16,}$")
WORD = re.compile(r"^[A-Za-z]+$")
NOT_RESOURCE = {"api", "rest", "public", "v"}
REDACT_QUERY = {"code", "state"}  # OAuth redirect values: one-time, but still a login in flight
GRAPHQL = re.compile(r"\s*(?:(?:query|mutation|subscription)\b[^{]*)?\{")  # a GraphQL document, not a search text
GRAPHQL_OP = re.compile(r"\s*(?:query|mutation|subscription)\s+([_A-Za-z][_A-Za-z0-9]*)")
GRAPHQL_NAME = re.compile(r"[_A-Za-z][_A-Za-z0-9]*")


def parse(har: dict, hosts: list[str] | None = None) -> Source:
    try:
        return _parse(har, hosts)
    except (KeyError, TypeError, AttributeError, IndexError) as e:
        raise ValueError(f"malformed HAR ({type(e).__name__}: {e})") from e


def _parse(har: dict, hosts: list[str] | None) -> Source:
    entries = [e for e in map(dict, (har.get("log") or {}).get("entries") or []) if _keep(e, hosts)]
    for e in entries:
        u = urlsplit(e["request"]["url"])
        e["_base"] = f"{u.scheme}://{u.netloc}"
        e["_segs"] = [s for s in u.path.split("/") if s]
        e["_slash"] = u.path.endswith("/") and u.path != "/"
        e["_op"] = _graphql_op(e["request"], u)
    _template(entries)

    groups: dict[tuple[str, str, str, str], list[dict]] = {}
    for e in entries:
        groups.setdefault((e["request"]["method"].upper(), e["_base"], e["_path"], e["_op"]), []).append(e)

    bases = Counter(e["_base"] for e in entries)
    base_url = bases.most_common(1)[0][0] if bases else ""
    schemes: dict[str, AuthScheme] = {}
    endpoints: list[Endpoint] = []
    ids: set[str] = set()
    for (method, base, path, op), group in groups.items():
        ok = [e for e in group if 0 < e["response"].get("status", 0) < 400]
        ep = _endpoint(method, path, ok or group, schemes)
        ep.base_url = "" if base == base_url else base
        op = f"#{op}" if op else ""  # one endpoint per GraphQL operation: 'POST /graphql#GetUser'
        ep.id += op
        if ep.id in ids:
            ep.id = f"{method} {base}{path}{op}"
        ids.add(ep.id)
        endpoints.append(ep)

    host = urlsplit(base_url).hostname or ""
    return Source(
        kind="har",
        title=_title(host),
        base_url=base_url,
        headers=_static_headers(entries),
        auth=list(schemes.values()),
        endpoints=endpoints,
    )


def _keep(e: dict, hosts: list[str] | None) -> bool:
    req, resp = e.get("request") or {}, e.get("response") or {}
    u = urlsplit(req.get("url", ""))
    host = (u.hostname or "").lower()
    if u.scheme not in ("http", "https") or req.get("method", "").upper() in ("OPTIONS", "HEAD", "TRACE"):
        return False
    if hosts and not any(host == h.lower() or host.endswith("." + h.lower().lstrip(".")) for h in hosts):
        return False
    if any(t in host for t in TRACKERS) or STATIC.search(u.path):
        return False
    if e.get("_resourceType") and e["_resourceType"] not in ("xhr", "fetch"):
        return False
    e["_json"] = _json(resp.get("content") or {})
    if e["_json"] is not None or "json" in ((resp.get("content") or {}).get("mimeType") or ""):
        return True
    # a write that answers 204, text or nothing (DELETE, PUT, form posts) is still an endpoint
    return req.get("method", "").upper() != "GET" and 200 <= (resp.get("status") or 0) < 300


def _graphql_op(req: dict, u) -> str:
    """The GraphQL operation of a request: operationName, else the query's own name, else a hash; '' if not GraphQL."""
    post = req.get("postData") or {}
    body = _json({"text": post.get("text"), "encoding": post.get("encoding")})
    if not isinstance(body, dict) and "graphql" in u.path.lower():  # GET /graphql?query=...
        body = dict(parse_qsl(u.query))
    if not isinstance(body, dict):
        return ""
    query, name = body.get("query"), body.get("operationName")
    if isinstance(name, str) and GRAPHQL_NAME.fullmatch(name) and ("query" in body or "extensions" in body):
        return name  # 'extensions' alone: a persisted query sent by its hash
    if not isinstance(query, str) or not GRAPHQL.match(query):
        return ""
    m = GRAPHQL_OP.match(query)
    return m.group(1) if m else hashlib.sha256(query.encode()).hexdigest()[:8]


def _json(content: dict) -> Any:
    """The parsed JSON body, or None. HAR text may be base64."""
    text = content.get("text")
    if not text:
        return None
    if content.get("encoding") == "base64":
        try:
            text = base64.b64decode(text).decode("utf-8", "replace")
        except ValueError:
            return None
    text = text.strip()
    if not text.startswith(("{", "[")):
        return None
    try:
        return json.loads(text)
    except ValueError:
        return None


def _post_body(post: dict) -> tuple[Any, str, list[str]]:
    """(body, encoding, names of file parts left out) of a HAR postData; a form becomes {name: value}."""
    body = _json({"text": post.get("text"), "encoding": post.get("encoding")})
    if body is not None:
        return body, "json", []
    mime, text, params = (post.get("mimeType") or "").lower(), post.get("text") or "", post.get("params") or []
    if "multipart/" in mime:
        encoding = "multipart"
        fields = [(p.get("name"), p.get("value"), bool(p.get("fileName"))) for p in params]
        if not fields:
            msg = email.message_from_string(f"Content-Type: {post.get('mimeType')}\n\n{text}")
            parts = msg.get_payload() if msg.is_multipart() else []
            fields = [
                (p.get_param("name", header="content-disposition"), p.get_payload(), p.get_filename() is not None)
                for p in parts
            ]
    elif "x-www-form-urlencoded" in mime or params:
        encoding = "form"
        pairs = parse_qsl(text, keep_blank_values=True) or [(p.get("name"), p.get("value")) for p in params]
        fields = [(k, v, False) for k, v in pairs]
    else:
        return None, "json", []
    form: dict[str, list] = {}
    for name, value, is_file in fields:
        if isinstance(name, str) and not is_file:
            form.setdefault(name, []).append(value)
    files = [n for n, _, is_file in fields if is_file and isinstance(n, str)]
    return {k: v[0] if len(v) == 1 else v for k, v in form.items()} or None, encoding, files


def _is_id(seg: str) -> bool:
    return seg.isdigit() or bool(UUID.match(seg)) or _opaque(seg) or bool(EMAIL.fullmatch(unquote(seg)))


def _value_like(seg: str) -> bool:
    # shortcut: plain words (and dashed endpoint names that vary) are judged by look only; use response shapes if this misgroups
    return not WORD.match(seg) and not re.match(r"^v\d+$", seg)


def _template(entries: list[dict]) -> None:
    """Set e["_path"] (template) and e["_pvals"] ({name: value}) for every entry."""
    # A short number leading the path is an API version ('/1/indexes/...'), not an id.
    var = [[_is_id(s) and not (j == 0 and len(s) <= 2) for j, s in enumerate(e["_segs"])] for e in entries]
    # A position also becomes a variable when it varies across entries that agree on every other segment.
    for i in range(max((len(e["_segs"]) for e in entries), default=0)):
        seen: dict[tuple, set[str]] = {}
        for e, v in zip(entries, var, strict=True):
            segs = e["_segs"]
            if i < len(segs) and not v[i]:
                key = (e["_base"], len(segs), *("*" if v[j] else s for j, s in enumerate(segs) if j != i))
                seen.setdefault(key, set()).add(segs[i])
        for e, v in zip(entries, var, strict=True):
            segs = e["_segs"]
            if i < len(segs) and not v[i]:
                key = (e["_base"], len(segs), *("*" if v[j] else s for j, s in enumerate(segs) if j != i))
                values = seen[key]
                if len(values) > 1 and all(_value_like(s) for s in values):
                    v[i] = True
    for e, v in zip(entries, var, strict=True):
        parts, pvals = [], {}
        for j, s in enumerate(e["_segs"]):
            if not v[j]:
                parts.append(s)
                continue
            name = _var_name(e["_segs"][j - 1] if j and not v[j - 1] else "", pvals)
            pvals[name] = s
            parts.append("{" + name + "}")
        e["_path"] = "/" + "/".join(parts) + ("/" if e["_slash"] else "")
        e["_pvals"] = pvals


def _var_name(prev: str, used: dict) -> str:
    base = "id"
    if WORD.match(prev.replace("-", "").replace("_", "")) and prev.lower() not in NOT_RESOURCE:
        word = prev.lower().replace("-", "_")
        if word.endswith("ies"):
            word = word[:-3] + "y"
        elif word.endswith(("xes", "ches", "shes", "sses")):
            word = word[:-2]
        elif word.endswith("s") and not word.endswith("ss"):
            word = word[:-1]
        base = word + "_id"
    name, n = base, 2
    while name in used:
        name, n = f"{base}{n}", n + 1
    return name


def _endpoint(method: str, path: str, group: list[dict], schemes: dict[str, AuthScheme]) -> Endpoint:
    params: list[Param] = []
    for name, value in group[0]["_pvals"].items():
        values = [e["_pvals"][name] for e in group]
        t = _infer(values)
        # an opaque value (token, hash, key) may be a secret whatever the segment is called
        ex = None if any(_opaque(v) for v in values) else _scrub(_convert(unquote(value), t), name)
        params.append(Param(name=name, location="path", type=t, required=True, example=ex))

    auth: list[str] = []
    queries = [parse_qsl(urlsplit(e["request"]["url"]).query, keep_blank_values=True) for e in group]
    names = list(dict.fromkeys(k for q in queries for k, _ in q))
    for name in names:
        if _is_secret(name):
            if _is_credential(name):
                schemes.setdefault(name, AuthScheme(name=name, type="query", param=name))
                auth.append(name)
            continue
        values = [v for q in queries for k, v in q if k == name]
        repeated = any(sum(k == name for k, _ in q) > 1 for q in queries)
        t = "array" if repeated else _infer(values)
        ex = _scrub(values if repeated else _convert(values[0], t), name)
        params.append(
            Param(
                name=name,
                location="query",
                type=t,
                required=all(any(k == name for k, _ in q) for q in queries),
                example="<redacted>" if name.lower() in REDACT_QUERY else ex,
            )
        )

    for e in group:
        for h in e["request"].get("headers") or []:
            name, value = h["name"].lower(), h.get("value") or ""
            if name == "authorization":
                scheme = value.split(" ", 1)[0].lower()
                if scheme == "bearer":
                    s = AuthScheme(name="bearer", type="bearer")
                elif scheme == "basic":
                    s = AuthScheme(name="basic", type="basic")
                else:
                    s = AuthScheme(name="authorization", type="header", param="Authorization")
            elif name not in SKIP_HEADERS and not name.startswith(BROWSER_PREFIXES) and _is_credential(name):
                s = AuthScheme(name=name, type="header", param=name)
            else:
                continue
            schemes.setdefault(s.name, s)
            auth.append(s.name)

    body_example, encoding, files = None, "json", []
    for e in group:
        body, encoding, files = _post_body(e["request"].get("postData") or {})
        if body is not None:
            body_example = _scrub(body)
            break

    largest = max(group, key=lambda e: len(json.dumps(e.get("_json"))) if e.get("_json") is not None else -1)
    return Endpoint(
        id=f"{method} {path}",
        method=method,
        path=path,
        description=f"file parts left out (uploads are not supported): {', '.join(files)}" if files else "",
        params=params,
        body_example=body_example,
        body_encoding=encoding if body_example is not None else "json",
        response_example=_trim(_scrub(largest.get("_json"))),  # scrub before trimming so no half-cut email survives
        auth=list(dict.fromkeys(auth)),
        count=len(group),
    )


def _infer(values: list[str]) -> str:
    def all_match(pattern: str) -> bool:
        return bool(values) and all(re.fullmatch(pattern, v) for v in values)

    if all_match(r"-?\d+"):
        return "integer"
    if all_match(r"-?\d+(\.\d+)?"):
        return "number"
    if bool(values) and all(v.lower() in ("true", "false") for v in values):
        return "boolean"
    return "string"


def _convert(value: str, t: str) -> Any:
    if t == "integer":
        return int(value)
    if t == "number":
        return float(value)
    if t == "boolean":
        return value.lower() == "true"
    return value


def _trim(data: Any, items: int = 3, clip: int = 300) -> Any:
    if isinstance(data, list):
        return [_trim(v, items, clip) for v in data[:items]]
    if isinstance(data, dict):
        return {k: _trim(v, items, clip) for k, v in data.items()}
    if isinstance(data, str) and len(data) > clip:
        return data[:clip]
    return data


def _static_headers(entries: list[dict]) -> dict[str, str]:
    """Headers sent with the same value on at least 80% of calls, minus browser-managed and secret ones."""
    seen: Counter[tuple[str, str]] = Counter()
    for e in entries:
        for h in {(h["name"].lower(), _header_value(h)) for h in e["request"].get("headers") or []}:
            seen[h] += 1
    out: dict[str, str] = {}
    for (name, value), n in seen.most_common():
        if n < 0.8 * len(entries) or name in out:
            continue
        if name.startswith(BROWSER_PREFIXES) or name in SKIP_HEADERS:
            continue
        if _is_secret(name) or _is_personal(name) or value.startswith("eyJ"):
            continue
        if any(_opaque(s) for s in re.split(r"[/?&=#;,:\s]+", value)):  # a token or id anywhere in the value
            continue
        if _scrub(value) != value:  # an email, phone or JWT inside the value
            continue
        out[name] = value
    return out


def _header_value(h: dict) -> str:
    value = h.get("value") or ""
    if h["name"].lower() in ("referer", "origin"):  # the page URL carries tickets and ids in its path and query
        u = urlsplit(value)
        value = f"{u.scheme}://{u.netloc.rpartition('@')[2]}" if u.scheme and u.netloc else ""
    return value


def _parts(name: str) -> list[str]:
    """userId -> [user, id]; X-Api-Key -> [x, api, key]."""
    name = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", name)
    name = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1_\2", name)
    return [p for p in re.split(r"[^a-z0-9]+", name.lower()) if p]


def _is_secret(name: str) -> bool:
    parts = _parts(name)
    words = {p.rstrip("0123456789") for p in parts}  # token2, key1
    return bool(SECRET & words) or any(w in "".join(parts) for w in SECRET_WHOLE)


def _is_personal(name: str) -> bool:
    parts = set(_parts(name))
    whole = "".join(_parts(name))
    return (
        bool(PERSONAL & parts)
        or any(w in whole for w in PERSONAL_WHOLE)
        or bool({"id", "uid"} & parts and PERSON_OWNERS & parts)
    )


def _is_credential(name: str) -> bool:
    """A secret that is a login (token, key, session id), not a one-time value like a CSRF token."""
    return _is_secret(name) and not NOT_AUTH.intersection(_parts(name))


_LITERAL = re.compile(r"\"(?:\\.|[^\"\\])*\"|'(?:\\.|[^'\\])*'|`[^`]*`")
_KEY_VALUE = re.compile(r"([\w-]+)\s*([=:])\s*[^\s&,;)\]}]+")


def note_text(s: str, limit: int = 60) -> str:
    """App source quoted in a note: string literals blanked and secret-looking `key=value` masked, then cut."""
    s = _LITERAL.sub('"…"', " ".join(s.split()))
    s = _KEY_VALUE.sub(lambda m: f"{m[1]}{m[2]}<redacted>" if _is_secret(m[1]) else m[0], s)
    return s[:limit]


def cap_notes(notes: list[str], limit: int = 200) -> list[str]:
    return notes if len(notes) <= limit else [*notes[:limit], f"… {len(notes) - limit} more"]


def _scrub(data: Any, name: str = "") -> Any:
    """Copy of `data` with secrets and personal values replaced; ordinary ids, titles and prices stay."""
    if data is None:
        return None
    if _is_secret(name):
        return "<redacted>"
    if _is_personal(name):
        return _placeholder(name, data)
    if isinstance(data, list):
        return [_scrub(v) for v in data]
    if isinstance(data, dict):
        # {"name": "api_token", "value": "..."}: the value is judged by the name it carries
        label = next((data[k] for k in ("name", "key") if isinstance(data.get(k), str)), "")
        return {
            _scrub(k) if isinstance(k, str) else k: _scrub(v, label if k == "value" and label else k)
            for k, v in data.items()
        }
    if isinstance(data, str):
        return PHONE.sub("09000000000", EMAIL.sub("user@example.com", JWT.sub("<redacted>", data)))
    return data


def _placeholder(name: str, data: Any) -> Any:
    if isinstance(data, bool):
        return data
    if isinstance(data, (int, float)):
        return 0
    if not isinstance(data, str):
        return "<redacted>"  # a whole object or list under a personal key
    parts = _parts(name)
    if {"email", "mail"} & set(parts):
        return "user@example.com"
    if {"phone", "mobile", "tel"} & set(parts):
        return "09000000000"
    return "<redacted>"


def _opaque(value: str) -> bool:
    return bool(OPAQUE.match(value) and re.search(r"\d", value) and re.search(r"[A-Za-z]", value))


def _title(host: str) -> str:
    """Site name from the registered domain: api.shop.example.co.ir -> Example."""
    parts = host.split(".")[:-1] or [host]
    if len(parts) > 1 and parts[-1] in ("co", "com", "org", "net", "ac", "gov"):
        parts = parts[:-1]
    return (parts[-1] or "API").capitalize()
