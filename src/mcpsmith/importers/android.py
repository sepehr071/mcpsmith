"""Read the Retrofit interfaces of a decompiled Android app into a Source of unverified endpoints.

Only declarations the app ships are read (request lines, parameter kinds); nothing here calls the app's API or
defeats a protection. `rea_class_source` and `find_api_classes` ask the REA CLI (already installed by
`mcpsmith setup`, with its own decompiler provider configured) for the decompiled text.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from urllib.parse import urlsplit

from mcpsmith.importers.har import _is_credential, _scrub
from mcpsmith.model import AuthScheme, Endpoint, Param, Source
from mcpsmith.setup.run import REA

VERBS = {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"}
IGNORED = {"Override", "Nullable", "NonNull", "NotNull", "Keep", "Deprecated", "Streaming", "Suppress", "Tag"}
PRIMITIVES = {"int", "long", "short", "byte", "float", "double", "boolean", "char"}
PATH_LIKE = re.compile(r"[\w\-./{}~:%]*")
CLASS_NAME = re.compile(r"[A-Za-z_$][\w$]*(?:\.[\w$]+)*")
FQ_CLASS = re.compile(r"(?:[A-Za-z_]\w*\.)+[A-Z_$][\w$]*")
API_STRING = r"\"(api|rest|v[0-9]+)/|/v[0-9]+/"
HINT = "REA must be installed (`mcpsmith setup`) and its Android decompiler provider configured; `npx -y {rea} doctor` says what is missing."


def parse_interface(java: str, base_url: str = "", title: str = "") -> Source:
    """Endpoints from one decompiled Retrofit interface; skipped methods and unresolved names are listed in `description`."""
    text = _strip_comments(java)
    schemes: dict[str, AuthScheme] = {}
    notes: list[str] = []
    found: list[Endpoint] = []
    for stmt in _split(text, ";", nest=False):
        if (parsed := _statement(stmt)) is None:
            continue
        name, annotations, params = parsed
        try:
            ep = _endpoint(name, annotations, params, schemes)
        except _Skip as e:
            notes.append(f"skipped {name}: {e}")
            continue
        if ep is not None:
            found.append(ep)
            if ep.method == "":
                notes.append(f"{name}: {ep.description}")
    if not found and not notes:
        notes.append("no Retrofit-annotated methods found; is this class an HTTP interface?")
    keys = [f"{e.method} {e.path}" for e in found]
    for e, key in zip(found, keys, strict=True):
        e.id = e.summary if e.method == "" or keys.count(key) > 1 else key
    iface = re.search(r"\binterface\s+(\w+)", text)
    return Source(
        kind="har",
        title=title or (iface.group(1) if iface else "Android app"),
        description="\n".join(notes),
        base_url=base_url,
        auth=list(schemes.values()),
        endpoints=found,
    )


class _Skip(Exception):
    pass


# ---------- text scanning ----------


def _strip_comments(text: str) -> str:
    def keep(m: re.Match) -> str:
        return m.group(0) if m.group(0).startswith('"') else " "

    return re.sub(r'"(?:\\.|[^"\\])*"|/\*.*?\*/|//[^\n]*', keep, text, flags=re.S)


def _split(s: str, sep: str, nest: bool = True) -> list[str]:
    """Split on `sep` outside string literals (and, with nest, outside brackets)."""
    parts, depth, start, i = [], 0, 0, 0
    while i < len(s):
        c = s[i]
        if c == '"':
            i += 1
            while i < len(s) and s[i] != '"':
                i += 2 if s[i] == "\\" else 1
        elif nest and c in "([{<":
            depth += 1
        elif nest and c in ")]}>":
            depth -= 1
        elif c == sep and depth <= 0:
            parts.append(s[start:i])
            start = i + 1
        i += 1
    parts.append(s[start:])
    return parts


def _balanced(s: str, i: int) -> int:
    """Index just past the parenthesis group opening at s[i]."""
    depth = 0
    while i < len(s):
        c = s[i]
        if c == '"':
            i += 1
            while i < len(s) and s[i] != '"':
                i += 2 if s[i] == "\\" else 1
        elif c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1
    raise _Skip("unbalanced parentheses")


def _annotations(s: str) -> tuple[list[tuple[str, str]], str]:
    """Leading `@Name(args)` annotations (simple name, raw args) and the rest of the text."""
    out, i = [], 0
    while True:
        m = re.compile(r"\s*@([\w.$]+)").match(s, i)
        if not m:
            return out, s[i:].strip()
        i, args = m.end(), ""
        if (p := re.compile(r"\s*\(").match(s, i)) is not None:
            end = _balanced(s, p.end() - 1)
            args, i = s[p.end() : end - 1], end
        out.append((m.group(1).rsplit(".", 1)[-1], args))


def _statement(stmt: str) -> tuple[str, list[tuple[str, str]], list[str]] | None:
    """(method name, its annotations, raw parameters) of an annotated interface method, else None."""
    s = stmt[_last_open_brace(stmt) + 1 :].strip().strip("}").strip()
    try:
        annotations, rest = _annotations(s)
        if not annotations or "(" not in rest:
            return None
        open_ = rest.index("(")
        head = rest[:open_].split()
        if len(head) < 2 or not re.fullmatch(r"\w+", head[-1]):
            return None
        inner = rest[open_ + 1 : _balanced(rest, open_) - 1]
    except _Skip:
        return None
    return head[-1], annotations, [p for p in _split(inner, ",") if p.strip()]


def _last_open_brace(s: str) -> int:
    """Index of the last `{` outside strings and parentheses (class header end), or -1."""
    last, depth, i = -1, 0, 0
    while i < len(s):
        c = s[i]
        if c == '"':
            i += 1
            while i < len(s) and s[i] != '"':
                i += 2 if s[i] == "\\" else 1
        elif c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
        elif c == "{" and depth == 0:
            last = i
        i += 1
    return last


def _args(raw: str) -> list[tuple[str | None, str]]:
    out = []
    for part in _split(raw, ","):
        if part.strip():
            m = re.fullmatch(r"\s*(\w+)\s*=(?!=)(.*)", part, flags=re.S)
            out.append((m.group(1), m.group(2).strip()) if m else (None, part.strip()))
    return out


def _literal(expr: str) -> str | None:
    """The value of a single string literal, None for anything else (a concatenation, a constant)."""
    m = re.fullmatch(r'"((?:\\.|[^"\\])*)"', expr.strip())
    return m.group(1) if m else None


# ---------- one method ----------


def _endpoint(name: str, annotations: list[tuple[str, str]], params: list[str], schemes: dict[str, AuthScheme]):
    method, path, has_body, encoding, headers, unresolved = None, None, False, "", [], []
    guessed: list[tuple[str, str]] = []
    for an, raw in annotations:
        args = _args(raw)
        keys = {k for k, _ in args}
        if an in VERBS:
            method, path = an, _path(args[0][1]) if args else ""
        elif {"method", "path"} <= keys:  # @HTTP, whatever it is called now
            kv = dict(args)
            if (m := _literal(kv["method"])) is None:
                raise _Skip("HTTP method is not a string literal")
            method, path, has_body = m.upper(), _path(kv["path"]), kv.get("hasBody", "").strip() == "true"
        elif an == "HTTP":
            raise _Skip("@HTTP without method and path")
        elif an == "FormUrlEncoded":
            encoding = "form-encoded body"
        elif an == "Multipart":
            encoding = "multipart body"
        elif an == "Headers":
            headers += [h for a in _split(raw.strip().strip("{}"), ",") if (h := _literal(a)) is not None]
        elif an not in IGNORED and len(args) == 1 and args[0][0] is None:
            if (p := _literal(args[0][1])) is not None and PATH_LIKE.fullmatch(p):
                guessed.append((an, p))
    if method is None and path is None:
        if len(guessed) == 0:
            return None  # not an HTTP method
        if len(guessed) > 1:
            raise _Skip("several unnamed string annotations; cannot tell which is the request line")
        method, path = "", guessed[0][1]
        unresolved.append(f"annotation @{guessed[0][0]} looks like a minified request line, HTTP method unknown")
    base = ""
    if path.startswith(("http://", "https://")):
        u = urlsplit(path)
        base, path = f"{u.scheme}://{u.netloc}", u.path + (f"?{u.query}" if u.query else "")
    path, _, fixed = path.partition("?")
    placeholders = set(re.findall(r"\{(\w+)\}", path))

    plist: list[Param] = []
    auth: list[str] = []

    def credential(header: str, kind: str = "header") -> None:
        scheme = AuthScheme(name=header.lower(), type=kind, param=header)  # type: ignore[arg-type]
        schemes.setdefault(scheme.name, scheme)
        auth.append(scheme.name)

    for pair in filter(None, fixed.split("&")):
        k, _, v = pair.partition("=")
        plist.append(Param(name=k, location="query", example=_scrub(v, k) if v else None))
    for h in headers:
        k, _, v = h.partition(":")
        k, v = k.strip(), v.strip()
        if k.lower() == "authorization" or _is_credential(k):
            credential(k)
        elif k:
            plist.append(Param(name=k, location="header", example=_scrub(v, k)))
    for raw in params:
        ans, rest = _annotations(raw)
        ans = [a for a in ans if a[0] not in IGNORED]
        tokens = re.sub(r"\bfinal\b", "", rest).split()
        if not ans or len(tokens) < 2:
            continue  # not an HTTP parameter (e.g. a Kotlin Continuation)
        var, jtype = tokens[-1], " ".join(tokens[:-1])
        an, braw = ans[0]
        first = _args(braw)[:1]
        label = _literal(first[0][1]) if first and first[0][0] in (None, "value") else None
        p = _param(an, label, var, jtype, placeholders, unresolved)
        if p is None:
            continue
        if p.location == "header" and (p.name.lower() == "authorization" or _is_credential(p.name)):
            credential(p.name)
        elif p.location == "query" and _is_credential(p.name):
            credential(p.name, "query")
        else:
            plist.append(p)
    if has_body and not any(p.location == "body" for p in plist):
        plist.append(Param(name="body", location="body", type="object", required=True))
    if not path and (url := next((p for p in plist if p.description == "full URL"), None)):
        path = "{" + url.name + "}"
    if not path.startswith("/") and not path.startswith("{"):
        path = "/" + path
    note = "; ".join(unresolved)
    return Endpoint(
        id="",
        method="" if unresolved else (method or ""),
        path=path,
        base_url=base,
        summary=name,
        description="; ".join(x for x in (encoding, note) if x),
        params=plist,
        auth=list(dict.fromkeys(auth)),
        count=0,
        verified=False,
    )


def _path(expr: str) -> str:
    if (p := _literal(expr)) is None:
        raise _Skip(f"path is built at run time ({expr.strip()[:60]})")
    return p


def _type(jtype: str) -> str:
    t = jtype.split("<")[0].split(".")[-1]
    if t.endswith("[]") or t in {"List", "Set", "Collection", "ArrayList", "Iterable"}:
        return "array"
    if t in {"int", "long", "short", "byte", "Integer", "Long", "Short", "Byte"}:
        return "integer"
    if t in {"float", "double", "Float", "Double", "Number"}:
        return "number"
    if t in {"boolean", "Boolean"}:
        return "boolean"
    if t in {"Map", "HashMap", "LinkedHashMap"}:
        return "object"
    return "string"


def _param(an, label, var, jtype, placeholders, unresolved) -> Param | None:
    t = _type(jtype)
    primitive = jtype in PRIMITIVES
    name = label or var
    if an == "Path":
        return Param(name=name, location="path", type=t, required=True)
    if an == "Query":
        return Param(name=name, location="query", type=t, required=primitive)
    if an == "QueryMap":
        return Param(name=var, location="query", type="object", description="map of extra query parameters")
    if an == "Field":
        return Param(name=name, location="body", type=t, required=primitive)
    if an in {"FieldMap", "PartMap"}:
        return Param(name=var, location="body", type="object", description="map of extra body fields")
    if an == "Part":
        return Param(name=name, location="body", type=t if label else "object")
    if an == "Body":
        return Param(
            name=var, location="body", type="object" if t == "string" and jtype != "String" else t, required=True
        )
    if an == "Header":
        return Param(name=name, location="header", type=t)
    if an == "HeaderMap":
        return Param(name=var, location="header", type="object", description="map of extra headers")
    if an == "Url":
        return Param(name=var, location="path", required=True, description="full URL")
    # a minified annotation name: use its structure and the path placeholders
    if label is not None:
        if label in placeholders:
            return Param(name=label, location="path", type=t, required=True)
        unresolved.append(f"parameter {label!r} has minified annotation @{an}; query assumed")
        return Param(name=label, location="query", type=t, required=primitive, description="kind unresolved")
    if t == "string" and jtype not in {"String", "CharSequence"}:  # only @Body takes an arbitrary object
        return Param(name=var, location="body", type="object", required=True)
    unresolved.append(f"parameter {var!r} has minified annotation @{an} with no name; kind unknown")
    return None


# ---------- REA ----------


def _rea(args: list[str], runner, apk: Path):
    if not apk.is_file():
        raise ValueError(f"{apk}: not a file")
    cmd = [shutil.which("npx") or "npx", "-y", REA, *args, "--format", "json"]
    # a neutral working directory: nothing is ever run from where the APK lives
    with tempfile.TemporaryDirectory() as cwd:
        try:
            r = runner(cmd, capture_output=True, text=True, encoding="utf-8", timeout=600, cwd=cwd)
        except (OSError, subprocess.TimeoutExpired) as e:
            raise RuntimeError(f"could not run REA ({e}). {HINT.format(rea=REA)}") from e
    if r.returncode:
        tail = (r.stderr or r.stdout or "").strip()[-400:]
        raise RuntimeError(f"REA failed: {tail}. {HINT.format(rea=REA)}")
    try:
        return json.loads(r.stdout)
    except ValueError:
        return r.stdout


def _strings(obj) -> list[str]:
    if isinstance(obj, str):
        return [obj]
    if isinstance(obj, dict):
        return [s for v in obj.values() for s in _strings(v)]
    if isinstance(obj, list):
        return [s for v in obj for s in _strings(v)]
    return []


def rea_class_source(apk: Path, class_name: str, runner=subprocess.run) -> str:
    """The decompiled source of one class, from REA."""
    if not CLASS_NAME.fullmatch(class_name):
        raise ValueError(f"{class_name!r} is not a class name like com.example.api.Service")
    return "\n".join(_strings(_rea(["decompile", str(apk.resolve()), class_name], runner, apk)))


def find_api_classes(apk: Path, runner=subprocess.run) -> list[str]:
    """Class names whose code holds a path-like string such as "api/..." or "/v1/...", via REA's string search."""
    out = _rea(["search", str(apk.resolve()), f"--pattern={API_STRING}", "--mode", "regex"], runner, apk)
    names: list[str] = []

    def walk(o) -> None:
        if isinstance(o, dict):
            for k, v in o.items():
                if "class" in k.lower() and isinstance(v, str) and CLASS_NAME.fullmatch(v):
                    names.append(v)
                else:
                    walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)
        elif isinstance(o, str) and FQ_CLASS.fullmatch(o):
            names.append(o)

    walk(out)
    return sorted(dict.fromkeys(names))
