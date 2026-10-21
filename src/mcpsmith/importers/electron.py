"""Read the HTTP calls of an Electron (or any bundled JavaScript) app's own code into a Source of unverified endpoints.

Only code the app ships is read, as text: nothing is run, imported or resolved, and nothing here defeats a protection.
The original sources inside `.map` files (`sourcesContent`) win over the bundle they belong to. Extraction is regex over
readable source, so every endpoint stays unverified until `mcpsmith probe` sees it answer.
"""

from __future__ import annotations

import bisect
import json
import re
import struct
from collections import Counter
from pathlib import Path
from urllib.parse import urlsplit

from mcpsmith.importers.har import _is_credential, _scrub
from mcpsmith.model import AuthScheme, Endpoint, Param, Source

EXTS = {".js", ".mjs", ".cjs", ".ts"}
VERBS = "GET|POST|PUT|PATCH|DELETE|HEAD|OPTIONS"
WINDOW = 8000  # how far past a call's opening parenthesis its arguments are read
WRAPPERS = {"encodeURIComponent", "encodeURI", "String", "Number", "toString", "this", "toLowerCase", "toUpperCase"}

HELPER = re.compile(rf"""(?<![\w$])(?:[\w$]+\.)*[\w$]+\s*\(\s*(['"`])({VERBS})\1\s*,""")  # request('GET', path, opts)
FETCH = re.compile(r"(?<![\w$])(?:[\w$]+\.)*fetch\s*\(")
AXIOS = re.compile(r"(?<![\w$])axios\.(get|post|put|patch|delete|head|options)\s*\(")
METHOD_KEY = re.compile(r"""\bmethod\s*:\s*(['"`])([A-Za-z]+)\1""")
HEADERS_KEY = re.compile(r"\bheaders\s*:\s*\{")
HEADER_PAIR = re.compile(r"""(?:(['"])([\w-]+)\1|([\w-]+))\s*:\s*(['"])((?:\\.|(?!\4).)*)\4""")


# shortcut: a comment is only recognised after whitespace, so "http://" and "**/*.js" in strings survive; a comment
# glued to code (`x;//c`) stays visible. Strings are not tracked: a regex literal's quote would desync the scan.
COMMENT = re.compile(r"(?<!\S)(?://[^\n]*|/\*.*?\*/)", re.S)
ASSIGN_WINDOW = 3000  # how far above a call a variable's assignment is looked for


class _Skip(Exception):
    pass


# ---------- reading an app ----------


def read_app(
    path: Path,
    notes: list[str] | None = None,
    max_files: int = 400,
    max_bytes: int = 8_000_000,
    max_map_bytes: int = 64_000_000,
) -> dict[str, str]:
    """{logical file name: text} of an app's JavaScript/TypeScript from a directory, an .asar or one file.

    A bundle with a `.map` that carries `sourcesContent` is replaced by those original sources. `node_modules` is the
    app's dependencies, not its own code, and is left out. Files past the caps are dropped and counted in `notes`.
    """
    notes = notes if notes is not None else []
    skipped: list[tuple[str, int]] = []
    path = Path(path)
    if path.is_dir():
        found = sorted(
            (p.relative_to(path).as_posix(), p)
            for p in path.rglob("*")
            if p.is_file() and p.suffix in EXTS | {".map"} and "node_modules" not in p.relative_to(path).parts
        )
        cands = [(n, p.stat().st_size, p.read_bytes) for n, p in found]
        raw = _cap(cands, notes, skipped, max_files, max_bytes, max_map_bytes)
    elif path.suffix == ".asar":
        raw = _cap(_asar_entries(path), notes, skipped, max_files, max_bytes, max_map_bytes)
    elif path.is_file():
        raw = _cap(
            [(path.name, path.stat().st_size, path.read_bytes)], notes, skipped, max_files, max_bytes, max_map_bytes
        )
    else:
        raise ValueError(f"{path}: no such file or directory")
    texts = {n: b.decode("utf-8-sig", errors="replace") for n, b in raw.items() if not n.endswith(".map")}
    out: dict[str, str] = {}
    covered: set[str] = set()
    for name, text in raw.items():
        if name.endswith(".map"):
            if sources := _map_sources(name, text.decode("utf-8-sig", errors="replace"), notes):
                texts.pop(name[:-4], None)
                covered.add(name[:-4])
                for logical, content in sources.items():
                    out[f"{name}:{logical}" if logical in out else logical] = content
    for name, text in texts.items():
        out[f"{name}:bundle" if name in out else name] = text
    for name, size in skipped:
        if name.endswith(".map"):
            tail = "its original sources are not read"
        elif name in covered:
            tail = "its .map sources were read instead"
        else:
            tail = "not read, and no .map sources cover it"
        cap = max_map_bytes if name.endswith(".map") else max_bytes
        notes.append(f"skipped {name}: {size} bytes is over the {cap}-byte cap; {tail}")
    return out


def _cap(
    cands, notes: list[str], skipped: list[tuple[str, int]], max_files: int, max_bytes: int, max_map_bytes: int
) -> dict[str, bytes]:
    """Read what fits: `.map` files are JSON whose sourcesContent is the payload, so they get their own larger cap."""
    if len(cands) > max_files:
        notes.append(f"dropped {len(cands) - max_files} files beyond the {max_files}-file cap")
    out = {}
    for name, size, read in cands[:max_files]:
        if size > (max_map_bytes if name.endswith(".map") else max_bytes):
            skipped.append((name, size))
        else:
            out[name] = read()
    return out


def _map_sources(name: str, text: str, notes: list[str]) -> dict[str, str]:
    try:
        doc = json.loads(text)
        contents, sources = doc.get("sourcesContent"), doc.get("sources")
    except (ValueError, AttributeError):
        notes.append(f"skipped {name}: not a source map")
        return {}
    if not isinstance(contents, list) or not isinstance(sources, list):
        return {}  # no embedded sources: the bundle is read instead
    out = {}
    for src, content in zip(sources, contents, strict=False):
        if not (isinstance(src, str) and isinstance(content, str)):
            continue
        logical = "/".join(
            p for p in re.sub(r"^[\w+.-]+:/+", "", src.split("?")[0]).split("/") if p not in ("", ".", "..")
        )
        if logical and "node_modules" not in logical.split("/"):
            out[logical] = content
    return out


def _asar_entries(path: Path):
    """(name, size, reader) of every JS/map file in an .asar: 8-byte pickle header, JSON index, then file data."""
    try:
        with path.open("rb") as f:
            head = f.read(16)
            if len(head) < 16:
                raise ValueError("truncated header")
            marker, header_size, _, json_len = struct.unpack("<4I", head)
            total = path.stat().st_size
            if marker != 4 or json_len > header_size or 8 + header_size > total:
                raise ValueError("bad header")
            index = json.loads(f.read(json_len))
            base = 8 + header_size
        entries = []
        stack = [("", index)]
        while stack:
            prefix, node = stack.pop()
            for name, info in node["files"].items():
                full = f"{prefix}{name}"
                if "files" in info:
                    stack.append((f"{full}/", info))
                elif "offset" in info and not info.get("unpacked"):
                    offset, size = int(info["offset"]), int(info["size"])
                    if offset < 0 or size < 0 or base + offset + size > total:
                        raise ValueError(f"{full} runs past the end of the file")
                    suffix = "." + full.rpartition(".")[2]
                    if (suffix in EXTS or suffix == ".map") and "node_modules" not in full.split("/"):
                        entries.append((full, offset, size))
    except (ValueError, KeyError, TypeError, AttributeError, struct.error, OSError) as e:
        raise ValueError(f"{path}: malformed .asar ({e})") from e

    def reader(offset: int, size: int):
        def read() -> bytes:
            with path.open("rb") as f:
                f.seek(base + offset)
                return f.read(size)

        return read

    return [(n, size, reader(offset, size)) for n, offset, size in sorted(entries)]


# ---------- extracting calls ----------


def parse_sources(files: dict[str, str], base_url: str = "", title: str = "", max_endpoints: int = 2000) -> Source:
    """Endpoints from the request calls in `files`; skipped calls and unreadable methods are listed in `description`."""
    notes: list[str] = []
    schemes: dict[str, AuthScheme] = {}
    found: list[Endpoint] = []
    dropped = 0
    for fname, text in files.items():
        text = COMMENT.sub(_blank, text)  # a comment saying "fetch (like..." is not a call
        newlines = [m.start() for m in re.finditer("\n", text)]
        for pos, end, method, kind in sorted(_sites(text)):
            where = f"{fname}:{bisect.bisect(newlines, pos) + 1}"
            try:
                args = _call_args(text[end : end + WINDOW])
                ep = _endpoint(kind, method, args, where, schemes, text[max(0, pos - ASSIGN_WINDOW) : pos])
            except _Skip as e:
                notes.append(f"skipped {where}: {e}")
                continue
            if ep.method == "":
                notes.append(f"{where}: HTTP method not readable for {ep.path}")
            if len(found) < max_endpoints:
                found.append(ep)
            else:
                dropped += 1
    if dropped:
        notes.append(f"dropped {dropped} endpoints beyond the {max_endpoints}-endpoint cap")
    if not found and not notes:
        notes.append("no request calls found; does this app use request(method, path), fetch or axios?")
    keys = [f"{e.method} {e.path}" for e in found]
    for e, key in zip(found, keys, strict=True):
        e.id = key if e.method and keys.count(key) == 1 else f"{key} @ {e.summary}"
    seen: Counter[str] = Counter()
    for e in found:
        seen[e.id] += 1
        if seen[e.id] > 1:
            e.id = f"{e.id}-{seen[e.id]}"
    return Source(
        kind="app",
        title=title or "JavaScript app",
        description="\n".join(notes),
        base_url=base_url,
        auth=list(schemes.values()),
        endpoints=found,
    )


def _sites(text: str):
    """(start, end of the opening parenthesis, method or None, kind) of each request call."""
    for m in HELPER.finditer(text):
        yield m.start(), m.end(), m.group(2), "helper"
    for m in FETCH.finditer(text):
        yield m.start(), m.end(), None, "fetch"
    for m in AXIOS.finditer(text):
        yield m.start(), m.end(), m.group(1).upper(), "axios"


def _skip_string(s: str, i: int) -> int:
    """Index just past the string or template literal starting at s[i]."""
    q, i = s[i], i + 1
    while i < len(s):
        if s[i] == "\\":
            i += 2
        elif s[i] == q:
            return i + 1
        elif q == "`" and s.startswith("${", i):
            i = _close(s, i + 1)
        else:
            i += 1
    raise _Skip("unterminated string")


def _close(s: str, i: int) -> int:
    """Index just past the bracket group opening at s[i]."""
    depth = 0
    while i < len(s):
        if s[i] in "'\"`":
            i = _skip_string(s, i)
            continue
        if s[i] in "([{":
            depth += 1
        elif s[i] in ")]}":
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1
    raise _Skip("unbalanced brackets")


def _call_args(s: str) -> list[str]:
    """The top-level arguments of a call whose text starts just after its `(`."""
    args, depth, start, i = [], 0, 0, 0
    while i < len(s):
        c = s[i]
        if c in "'\"`":
            i = _skip_string(s, i)
            continue
        if c in "([{":
            depth += 1
        elif c in ")]}":
            if depth == 0:
                args.append(s[start:i])
                return [a.strip() for a in args]
            depth -= 1
        elif c == "," and depth == 0:
            args.append(s[start:i])
            start = i + 1
        i += 1
    raise _Skip("call arguments not closed within reach")


def _clean(s: str) -> str:
    return _scrub(re.sub(r"//[^/\s@'\"`]*@", "//", s))


def _endpoint(
    kind: str, method: str | None, args: list[str], where: str, schemes: dict[str, AuthScheme], before: str
) -> Endpoint:
    rest = args[1:]  # `args` starts at the path: a helper's verb was already read from its first argument
    if kind == "fetch":
        opts = rest[0] if rest else None
        if opts is None:
            method = "GET"  # fetch's own default
        elif opts.startswith("{") and (m := METHOD_KEY.search(opts)):
            method = m.group(2).upper()
        elif opts.startswith("{") and "..." not in opts:
            method = "GET"
        else:
            method = ""
        if method not in VERBS.split("|"):
            method = ""
    path = _path(_assigned(args[0], before) if args else "")
    base = ""
    if path.startswith(("http://", "https://")):
        u = urlsplit(path)
        if "{" in u.netloc:
            raise _Skip(f"host is built at run time ({_clean(path)[:60]})")
        base, path = f"{u.scheme}://{u.netloc.rpartition('@')[2]}", u.path or "/"
        path += f"?{u.query}" if u.query else ""
    path, _, query = path.partition("?")
    path = path.partition("#")[0]
    if not path.startswith("/") and not path.startswith("{"):
        path = "/" + path
    names = re.findall(r"\{(\w+)\}", path)
    if re.search(r"""[${}+'"`\\\s]""", re.sub(r"\{\w+\}", "", path)):
        raise _Skip(f"path not understood ({_clean(path)[:60]})")
    auth: list[str] = []

    def credential(header: str, where_: str = "header") -> None:
        scheme = AuthScheme(name=header.lower(), type=where_, param=header)  # type: ignore[arg-type]
        schemes.setdefault(scheme.name, scheme)
        auth.append(scheme.name)

    params = [Param(name=n, location="path", required=True) for n in dict.fromkeys(names)]
    for pair in filter(None, query.split("&")):
        k, _, v = pair.partition("=")
        if not k or "{" in k:
            continue
        if _is_credential(k):
            credential(k, "query")
        else:
            params.append(Param(name=k, location="query", example=_scrub(v, k) if v and "{" not in v else None))
    joined = ",".join(rest)
    for hm in HEADERS_KEY.finditer(joined):
        try:
            block = joined[hm.end() - 1 : _close(joined, hm.end() - 1)]
        except _Skip:
            continue
        for pm in HEADER_PAIR.finditer(block):
            k, v = pm.group(2) or pm.group(3), pm.group(5)
            if k.lower() == "authorization" or _is_credential(k):
                credential(k)
            else:
                params.append(Param(name=k, location="header", example=_scrub(v, k)))
    return Endpoint(
        id="",
        method=method or "",
        path=path,
        base_url=base,
        summary=where,
        params=params,
        auth=list(dict.fromkeys(auth)),
        count=0,
        verified=False,
    )


def _blank(m: re.Match) -> str:
    """A comment as spaces, newlines kept so offsets and line numbers hold."""
    return re.sub(r"[^\n]", " ", m.group(0))


def _assigned(arg: str, before: str) -> str:
    """The literal a bare identifier was set to by its one `const|let|var` above the call, else `arg` unchanged.

    shortcut: one level, a fixed window, no scope analysis; a second assignment in the window leaves it unresolved.
    """
    if not re.fullmatch(r"[A-Za-z_$][\w$]*", arg):
        return arg
    sets = list(re.finditer(rf"(?<![\w$.]){re.escape(arg)}\s*=(?![=>])", before))
    if len(sets) != 1 or not re.search(r"\b(?:const|let|var)\s+$", before[: sets[0].start()]):
        return arg
    rest = before[sets[0].end() :].lstrip()
    try:
        if rest[:1] in ("'", '"', "`"):
            end = _skip_string(rest, 0)
            if rest[end:].lstrip()[:1] not in tuple("+-*/%.?([&|<>=!:"):
                return rest[:end]
    except _Skip:
        pass
    return arg


def _path(arg: str) -> str:
    """The path of a string or template literal with `${expr}` turned into `{name}`; anything else is skipped."""
    if len(arg) < 2 or arg[0] not in "'\"`" or _skip_string(arg, 0) != len(arg):
        raise _Skip(f"path is built at run time ({_clean(arg)[:60]})")
    body = arg[1:-1]
    if "\\" in body:
        raise _Skip(f"path has escapes ({_clean(arg)[:60]})")
    if arg[0] != "`":
        return body
    out, names, i = "", [], 0
    while i < len(body):
        if body.startswith("${", i):
            end = _close(body, i + 1)
            names.append(_name(body[i + 2 : end - 1], names))
            out += "{" + names[-1] + "}"
            i = end
        else:
            out += body[i]
            i += 1
    return out


def _name(expr: str, taken: list[str]) -> str:
    ids = [w for w in re.findall(r"[A-Za-z_$][\w$]*", expr) if w not in WRAPPERS]
    name = ids[-1].replace("$", "") if ids else ""
    if not name:
        name = f"p{len(taken) + 1}"
    return name if name not in taken else f"{name}{len(taken) + 1}"
