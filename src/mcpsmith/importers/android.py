"""Read the Retrofit interfaces of a decompiled Android app into a Source of unverified endpoints.

Only declarations the app ships are read (request lines, parameter kinds); nothing here calls the app's API or
defeats a protection. The decompiled text comes from `importers.jadx` (or any file the developer supplies). Release
builds rename the Retrofit annotations; `resolve_annotations` reads the app's own Retrofit request builder, which
names them again.
"""

from __future__ import annotations

import re
import zipfile
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from mcpsmith.importers.har import _is_credential, _scrub, cap_notes, note_text
from mcpsmith.model import AuthScheme, Endpoint, Param, Source

VERBS = {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"}
IGNORED = {"Override", "Nullable", "NonNull", "NotNull", "Keep", "Deprecated", "Streaming", "Suppress", "Tag"}
PRIMITIVES = {"int", "long", "short", "byte", "float", "double", "boolean", "char"}
PATH_LIKE = re.compile(r"[\w\-./{}~:%]*")
PARAM_NAMES = {"Path", "Query", "QueryMap", "QueryName", "Field", "FieldMap", "Body", "Part", "PartMap"} | {
    "Header",
    "HeaderMap",
    "Url",
    "Tag",
    "Headers",
}
NOT_APP_TYPE = ("java.", "javax.", "android.", "kotlin.", "okhttp3.")


def strip_userinfo(url: str) -> str:
    """`url` without a `user:pass@` before the host."""
    return re.sub(r"^([a-zA-Z][\w+.-]*://)[^/?#]*@", r"\1", url)  # greedy: up to the last @ of the authority


def parse_interface(
    java: str, base_url: str = "", title: str = "", annotations: dict[str, str] | None = None
) -> Source:
    """Endpoints from one decompiled Retrofit interface; skipped methods and unresolved names are listed in `description`.

    `annotations` maps a minified annotation name to its Retrofit meaning (see `resolve_annotations`).
    """
    names = annotations or {}
    text = _strip_comments(java)
    schemes: dict[str, AuthScheme] = {}
    notes: list[str] = []
    found: list[Endpoint] = []
    for stmt in _split(text, ";", nest=False):
        try:
            parsed = _statement(stmt)
        except _Skip as e:
            notes.append(f"skipped {note_text(stmt)!r}: {e}")
            continue
        if parsed is None:
            continue
        name, anns, params = parsed
        try:
            ep = _endpoint(name, anns, params, schemes, names)
        except _Skip as e:
            notes.append(f"skipped {name}: {e}")
            continue
        if ep is not None:
            found.append(ep)
            if ep.method == "" or "minified annotation" in ep.description:
                notes.append(f"{name}: {ep.description}")
    if not found and not notes:
        notes.append("no Retrofit-annotated methods found; is this class an HTTP interface?")
    keys = [f"{e.method} {e.path}" for e in found]
    for e, key in zip(found, keys, strict=True):
        e.id = e.summary if e.method == "" or keys.count(key) > 1 else key
    seen: Counter[str] = Counter()
    for e in found:  # overloads and same-named minified methods must not share an id
        seen[e.id] += 1
        if seen[e.id] > 1:
            e.id = f"{e.id}-{seen[e.id]}"
    iface = re.search(r"\binterface\s+(\w+)", text)
    return Source(
        kind="app",
        title=title or (iface.group(1) if iface else "Android app"),
        description="\n".join(cap_notes(notes)),
        base_url=strip_userinfo(base_url),
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


def _balanced(s: str, i: int, open_: str = "(", close: str = ")") -> int:
    """Index just past the bracket group opening at s[i]."""
    depth = 0
    while i < len(s):
        c = s[i]
        if c == '"':
            i += 1
            while i < len(s) and s[i] != '"':
                i += 2 if s[i] == "\\" else 1
        elif c == open_:
            depth += 1
        elif c == close:
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
    annotations, rest = _annotations(s)
    if not annotations or "(" not in rest:
        return None
    open_ = rest.index("(")
    head = rest[:open_].split()
    if len(head) < 2 or not re.fullmatch(r"\w+", head[-1]):
        raise _Skip("declaration not understood")
    inner = rest[open_ + 1 : _balanced(rest, open_) - 1]
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


def _endpoint(
    name: str,
    annotations: list[tuple[str, str]],
    params: list[str],
    schemes: dict[str, AuthScheme],
    names: dict[str, str],
):
    method, path, has_body, encoding, headers, unresolved = None, None, False, "", [], []
    guessed: list[tuple[str, str]] = []
    odd: list[str] = []
    for an, raw in annotations:
        an = names.get(an, an)
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
            if (p := _literal(args[0][1])) is not None:
                if PATH_LIKE.fullmatch(p):
                    guessed.append((an, p))
                else:
                    odd.append(an)
    if method is None and path is None:
        if len(guessed) == 0:
            if odd:
                raise _Skip(f"annotation @{odd[0]} holds a string that is not a path")
            return None  # not an HTTP method
        if len(guessed) > 1:
            raise _Skip("several unnamed string annotations; cannot tell which is the request line")
        method, path = "", guessed[0][1]
        unresolved.append(f"annotation @{guessed[0][0]} looks like a minified request line, HTTP method unknown")
    base = ""
    if path.startswith(("http://", "https://")):
        u = urlsplit(path)
        base, path = f"{u.scheme}://{u.netloc.rpartition('@')[2]}", u.path + (f"?{u.query}" if u.query else "")
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
        ans = [(names.get(a, a), r) for a, r in ans]
        ans = [a for a in ans if a[0] not in IGNORED]
        tokens = re.sub(r"\bfinal\b", "", rest).split()
        if not ans or len(tokens) < 2:
            continue  # not an HTTP parameter (e.g. a Kotlin Continuation)
        var, jtype = tokens[-1], " ".join(tokens[:-1])
        an, braw = ans[0]
        if an in ("Field", "FieldMap"):  # Retrofit sends these only as a form, @Part only as multipart
            encoding = encoding or "form-encoded body"
        elif an in ("Part", "PartMap"):
            encoding = encoding or "multipart body"
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
        method=method or "",  # "" only when the request line itself was unresolved
        path=path,
        base_url=base,
        summary=name,
        description="; ".join(x for x in (encoding, note) if x),
        params=plist,
        body_encoding={"form-encoded body": "form", "multipart body": "multipart"}.get(encoding, "json"),
        auth=list(dict.fromkeys(auth)),
        count=0,
        verified=False,
    )


def _path(expr: str) -> str:
    if (p := _literal(expr)) is None:
        raise _Skip(f"path is built at run time ({note_text(expr)})")
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
    if an == "QueryName":
        return None  # the parameter's value is the query key; nothing to describe
    # a minified annotation name: use its structure and the path placeholders
    if label is not None:
        if label in placeholders:
            return Param(name=label, location="path", type=t, required=True)
        unresolved.append(f"parameter {label!r} has minified annotation @{an}; kind unknown, left out of the request")
        return None
    if t == "string" and jtype not in {"String", "CharSequence"}:  # only @Body takes an arbitrary object
        return Param(name=var, location="body", type="object", required=True)
    unresolved.append(f"parameter {var!r} has minified annotation @{an} with no name; kind unknown")
    return None


# ---------- minified annotation names ----------

IF_INSTANCEOF = re.compile(r"\bif\s*\(\s*\w+\s+instanceof\s+([\w.$]+)\s*\)\s*\{")
FLAG_INSTANCEOF = re.compile(r"\b(\w+)\s*=\s*\w+\s+instanceof\s+([\w.$]+)\s*;")
STRING = re.compile(r'"(?:\\.|[^"\\])*"')


def resolve_annotations(builder_java: str) -> dict[str, str]:
    """Minified annotation name -> Retrofit meaning, read from the app's own Retrofit request builder.

    That class tests each annotation with `instanceof` and names it again in its error messages: a block holding
    "DELETE" is the @DELETE class; a block whose strings mention "@Query" most is @Query. Only the block of the
    `instanceof` is read, so a stray literal elsewhere counts for nothing. A block with no clear answer is left out.
    """
    text = _strip_comments(builder_java)
    blocks: list[tuple[str, str]] = []  # (annotation class, block text)
    for m in IF_INSTANCEOF.finditer(text):
        blocks.append((m.group(1), text[m.end() - 1 : _balanced(text, m.end() - 1, "{", "}")]))
    for m in FLAG_INSTANCEOF.finditer(text):  # `boolean z = a instanceof X;` ... `if (z) {`
        if use := re.compile(rf"\bif\s*\(\s*{re.escape(m.group(1))}\s*\)\s*\{{").search(text, m.end()):
            blocks.append((m.group(2), text[use.end() - 1 : _balanced(text, use.end() - 1, "{", "}")]))
    out: dict[str, str] = {}
    for cls, block in blocks:
        if "." not in cls or cls.startswith(NOT_APP_TYPE):
            continue
        literals = STRING.findall(block)
        counts = Counter(n for lit in literals for n in re.findall(r"@(\w+)", lit) if n in PARAM_NAMES)
        top = counts.most_common(2)
        if top and (len(top) == 1 or top[0][1] > top[1][1]):
            meaning = top[0][0]
        elif not top and (verb := next((v for v in (lit.strip('"') for lit in literals) if v in VERBS), None)):
            meaning = verb
        else:
            continue
        # shortcut: keyed on the simple class name, so same-named annotations in two minified packages conflate;
        # carry the package through `_annotations` to fix.
        out.setdefault(cls.rsplit(".", 1)[-1], meaning)
    # Blocks that name nothing: @Path's block checks a flag per earlier parameter kind and names it in the message
    # ("... must not come after a @Query"); the block that sets that flag is that kind. @Header is the parameter
    # block that reads allowUnsafeNonAsciiValues().
    flags: dict[str, str] = {}
    for cls, block in blocks:
        if out.get(cls.rsplit(".", 1)[-1]) == "Path":
            for flag, kind in re.findall(r'if\s*\(\s*\w+\.(\w+)\s*\)\s*\{\s*throw[^;]*?"[^"]*after a @(\w+)', block):
                flags[flag] = kind
    for cls, block in blocks:
        simple = cls.rsplit(".", 1)[-1]
        if simple in out or "." not in cls or cls.startswith(NOT_APP_TYPE):
            continue
        kinds = {flags[f] for f in re.findall(r"\b\w+\.(\w+)\s*=\s*true\s*;", block) if f in flags}
        if len(kinds) == 1:
            out[simple] = kinds.pop()
        elif not kinds and "allowUnsafeNonAsciiValues()" in block and ".value()" in block:
            out[simple] = "Header"
    return out


# ---------- apps without a declared HTTP interface ----------

# (app or SDK, mcpsmith sign-in provider, class-name fragments of its own request signer). Matched on class names
# only: nothing from the classes' code reaches the output.
KNOWN_SIGNERS = [
    ("Xiaomi Mi Home", "xiaomi", ("com.xiaomi.smarthome", "SmartHomeRc4", "com.xiaomi.youpin.login")),
]


RETROFIT_MARKS = (b"Lretrofit2/", b"HTTP method annotation is required")  # the second survives class renaming
HERMES_MAGIC = b"\xc6\x1f\xbc\x03"
DEX = re.compile(r"classes\d*\.dex")


@dataclass
class Triage:
    """What the APK is, read from its zip listing and dex bytes in seconds, before any decompiling."""

    framework: str = ""  # "flutter", "hermes", "react-native", or "" (plain Java/Kotlin)
    retrofit: bool | None = None  # None: not checked (not a readable APK)
    signer: tuple[str, str] | None = None


def triage(apk: Path) -> Triage:
    try:
        z = zipfile.ZipFile(apk)
    except (zipfile.BadZipFile, OSError):
        return Triage()
    with z:
        names = set(z.namelist())
        framework = ""
        if any(n.startswith("lib/") and n.endswith("/libflutter.so") for n in names):
            framework = "flutter"
        elif "assets/index.android.bundle" in names:
            with z.open("assets/index.android.bundle") as f:
                framework = "hermes" if f.read(4) == HERMES_MAGIC else "react-native"
        dex = sorted(n for n in names if DEX.fullmatch(n))
        retrofit, signer = False, None
        for name in dex:  # one file at a time: big apps ship 100 MB of dex
            data = z.read(name)
            retrofit = retrofit or any(m in data for m in RETROFIT_MARKS)
            signer = signer or next(
                (
                    (app, provider)
                    for app, provider, fragments in KNOWN_SIGNERS
                    if any(f.replace(".", "/").encode() in data for f in fragments)  # dex holds names as a/b/C
                ),
                None,
            )
    return Triage(framework, retrofit if dex else None, signer)


def framework_advice(framework: str, signer: tuple[str, str] | None = None) -> str:
    """What to do for an app built with a framework that has no Java HTTP interface to read."""
    record = (
        "Record its traffic instead: proxy the phone through mitmproxy (`mcpsmith app-desktop` prints the steps), "
        "then import the HAR with `mcpsmith app-desktop capture.har`."
    )
    text = {
        "flutter": f"the app is Flutter (Dart compiled to native code): no Java HTTP interface to read. {record}",
        "hermes": f"the app is React Native compiled to Hermes bytecode: no readable JS or Java interface. {record}",
        "react-native": "the app is React Native: its API calls are in the JS bundle. Extract "
        "assets/index.android.bundle from the APK (it is a zip), save it as bundle.js, then run "
        "`mcpsmith app-js bundle.js --base-url URL`.",
    }[framework]
    return text + (" " + signer_note(signer) if signer else "")


def known_signer(class_names) -> tuple[str, str] | None:
    """(app, provider) of the first known request-signing SDK among `class_names`, else None."""
    names = list(class_names)
    for app, provider, fragments in KNOWN_SIGNERS:
        if any(f in n for f in fragments for n in names):
            return app, provider
    return None


def dead_end(signer: tuple[str, str] | None = None) -> str:
    """What to do when the app declares no HTTP interface (no Retrofit-style class to read)."""
    text = (
        "no candidate API classes: the app declares no HTTP interface (no Retrofit-style class), so it likely builds "
        "or signs its requests itself. Next: record its traffic instead. Desktop app or website: run `mcpsmith "
        "browser`, then smith_record_start / smith_record_stop. Phone: proxy the phone through mitmproxy "
        "(`mcpsmith app-desktop` prints the steps), then import the HAR with `mcpsmith app-desktop capture.har`."
    )
    return text + (" " + signer_note(signer) if signer else "")


def signer_note(signer: tuple[str, str]) -> str:
    """The sign-in advice for an app whose request-signing SDK was found."""
    app, provider = signer
    return (
        f"Found {app}'s request-signing SDK: its calls are signed by the app, so sign in instead of copying keys; "
        f"plans use `auth: {{type: signin, provider: {provider}}}` (endpoints from a candidate class or recorded traffic)."
    )
