"""Recorded browser traffic (HAR) -> Source: JSON API calls grouped into endpoints with templated paths."""

from __future__ import annotations

import base64
import json
import re
from collections import Counter
from typing import Any
from urllib.parse import parse_qsl, urlsplit

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
    *"ticket otp nonce csrf xsrf jwt bearer credential credentials cookie".split(),
}
PERSONAL = {
    *"email mail phone mobile tel address password national ssn birth birthday firstname lastname fullname".split()
}
PERSON_OWNERS = {"user", "account", "customer", "device", "member", "profile", "client"}
# Names written without separators, or split across parts ("first_name").
SECRET_WHOLE = ("accesstoken", "refreshtoken", "authtoken", "apikey", "sessionid", "sessiontoken", "secretkey")
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


def parse(har: dict, hosts: list[str] | None = None) -> Source:
    entries = [e for e in map(dict, (har.get("log") or {}).get("entries") or []) if _keep(e, hosts)]
    for e in entries:
        u = urlsplit(e["request"]["url"])
        e["_base"] = f"{u.scheme}://{u.netloc}"
        e["_segs"] = [s for s in u.path.split("/") if s]
        e["_slash"] = u.path.endswith("/") and u.path != "/"
    _template(entries)

    groups: dict[tuple[str, str, str], list[dict]] = {}
    for e in entries:
        groups.setdefault((e["request"]["method"].upper(), e["_base"], e["_path"]), []).append(e)

    bases = Counter(e["_base"] for e in entries)
    base_url = bases.most_common(1)[0][0] if bases else ""
    schemes: dict[str, AuthScheme] = {}
    endpoints: list[Endpoint] = []
    ids: set[str] = set()
    for (method, base, path), group in groups.items():
        ok = [e for e in group if 0 < e["response"].get("status", 0) < 400]
        ep = _endpoint(method, path, ok or group, schemes)
        ep.base_url = "" if base == base_url else base
        if ep.id in ids:
            ep.id = f"{method} {base}{path}"
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
    return e["_json"] is not None or "json" in ((resp.get("content") or {}).get("mimeType") or "")


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


def _is_id(seg: str) -> bool:
    return seg.isdigit() or bool(UUID.match(seg)) or _opaque(seg)


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
        params.append(
            Param(name=name, location="path", type=t, required=True, example=_scrub(_convert(value, t), name))
        )

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
        params.append(
            Param(
                name=name,
                location="query",
                type=t,
                required=all(any(k == name for k, _ in q) for q in queries),
                example=_scrub(values if repeated else _convert(values[0], t), name),
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

    body_example = None
    for e in group:
        post = e["request"].get("postData") or {}
        body = _json({"text": post.get("text"), "encoding": post.get("encoding")})
        if body is not None:
            body_example = _scrub(body)
            break

    largest = max(group, key=lambda e: len(json.dumps(e.get("_json"))) if e.get("_json") is not None else -1)
    return Endpoint(
        id=f"{method} {path}",
        method=method,
        path=path,
        params=params,
        body_example=body_example,
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
        if _is_secret(name) or _is_personal(name) or _opaque(value) or value.startswith("eyJ"):
            continue
        if _scrub(value) != value:  # an email, phone or JWT inside the value
            continue
        out[name] = value
    return out


def _header_value(h: dict) -> str:
    value = h.get("value") or ""
    if h["name"].lower() == "referer":  # the page URL often carries tickets and ids in its query
        value = urlsplit(value)._replace(query="", fragment="").geturl()
    return value


def _parts(name: str) -> list[str]:
    """userId -> [user, id]; X-Api-Key -> [x, api, key]."""
    name = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", name)
    name = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1_\2", name)
    return [p for p in re.split(r"[^a-z0-9]+", name.lower()) if p]


def _is_secret(name: str) -> bool:
    parts = _parts(name)
    return bool(SECRET.intersection(parts)) or any(w in "".join(parts) for w in SECRET_WHOLE)


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
        return {k: _scrub(v, k) for k, v in data.items()}
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
