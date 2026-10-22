"""cURL commands (as copied from browser DevTools, "Copy as cURL (bash)") -> Source, through the HAR importer.

Each command becomes a HAR entry, so grouping, path templating, auth detection and scrubbing are the HAR rules.
"""

from __future__ import annotations

import re
import shlex
from typing import Any
from urllib.parse import quote_plus

from mcpsmith.importers import har
from mcpsmith.importers.android import strip_userinfo
from mcpsmith.model import Source

ANSI_C = re.compile(r"\$'((?:\\.|[^'\\])*)'", re.S)
ESCAPE = re.compile(r"\\(u[0-9a-fA-F]{1,4}|U[0-9a-fA-F]{1,8}|x[0-9a-fA-F]{1,2}|[0-7]{1,3}|.)", re.S)
SIMPLE = {"n": "\n", "r": "\r", "t": "\t", "a": "\a", "b": "\b", "f": "\f", "v": "\v", "e": "\x1b", "E": "\x1b"}
DATA = {"-d", "--data", "--data-raw", "--data-binary", "--data-ascii", "--json"}
# Options whose next word is their value and that say nothing about the request.
SKIP_ARG = {
    *"-A --user-agent -e --referer -o --output -m --max-time --connect-timeout -x --proxy -U --proxy-user".split(),
    *"-w --write-out --cacert --capath --cert --key -c --cookie-jar -r --range --retry --resolve -T".split(),
    *"--upload-file --limit-rate --interface --max-redirs -K --config --http-version --tls-max".split(),
}
SEPARATORS = {";", "&&", "||", "|", "&"}


def sniff(text: str | None) -> bool:
    """True when the first line that is not blank or a comment is a curl command."""
    for line in (text or "").splitlines():
        if line.strip() and not line.lstrip().startswith("#"):
            return line.lstrip().startswith("curl ")
    return False


def parse(text: str, hosts: list[str] | None = None) -> Source:
    notes: list[str] = []
    entries = [e for n, words in enumerate(_commands(text), 1) if (e := _entry(n, words, notes))]
    if not entries:
        raise ValueError("no curl command with an http(s) URL found" + (f" ({'; '.join(notes)})" if notes else ""))
    src = har.parse({"log": {"entries": entries}}, hosts=hosts)
    src.kind = "curl"
    src.description = "\n".join(dict.fromkeys(notes))
    return src


def _commands(text: str) -> list[list[str]]:
    """The words of each curl command in the text."""
    lines = [ln for ln in text.splitlines() if not ln.lstrip().startswith("#")]
    text = re.sub(r"\\\r?\n", " ", "\n".join(lines))  # line continuations
    try:
        words = shlex.split(_ansi_c_quotes(text))
    except ValueError as e:  # an unclosed quote
        raise ValueError(f"cannot read the curl commands: {e}") from e
    commands: list[list[str]] = []
    for w in words:
        if w == "curl":
            commands.append([])
        elif commands and w not in SEPARATORS:
            commands[-1].append(w)
    return commands


def _ansi_c_quotes(text: str) -> str:
    """Rewrite bash $'...' strings (DevTools uses them for quotes and control characters) as plain shell quotes."""
    out, i, quote = [], 0, ""
    while i < len(text):
        c = text[i]
        if quote:  # inside '...' or "..."
            if c == "\\" and quote == '"':
                out.append(text[i : i + 2])
                i += 2
                continue
            quote = "" if c == quote else quote
        elif c == "\\":
            out.append(text[i : i + 2])
            i += 2
            continue
        elif m := ANSI_C.match(text, i):
            out.append(shlex.quote(ESCAPE.sub(_unescape, m[1])))
            i = m.end()
            continue
        elif c in "'\"":
            quote = c
        out.append(c)
        i += 1
    return "".join(out)


def _unescape(m: re.Match) -> str:
    x = m[1]
    if x[0] in "uUx":
        return chr(int(x[1:], 16))
    if x[0] in "01234567":
        return chr(int(x, 8))
    if x in "\\'\"?":
        return x
    return SIMPLE.get(x, "\\" + x)  # bash keeps an unknown escape as it is


def _entry(n: int, words: list[str], notes: list[str]) -> dict[str, Any] | None:
    """A HAR entry for one curl command; None (with a note) when it has no URL."""
    url, method = "", ""
    headers: list[dict[str, str]] = []
    data: list[str] = []
    form: list[dict[str, Any]] = []
    get = is_json = False
    i = 0

    def value() -> str:
        nonlocal i
        i += 1
        return words[i] if i < len(words) else ""

    while i < len(words):
        w = words[i]
        if w in ("-X", "--request"):
            method = value().upper()
        elif w.startswith("-X"):  # -XPOST
            method = w[2:].upper()
        elif w in ("-H", "--header"):
            name, _, v = value().partition(":")
            if name.strip().lower() == "cookie":
                notes.append("cookies left out (a Cookie header); if the API needs a login session, use browser auth")
            elif name.strip():
                headers.append({"name": name.strip(), "value": v.strip()})
        elif w in ("-b", "--cookie"):
            value()
            notes.append("cookies left out (-b); if the API needs a login session, use browser auth")
        elif w in ("-u", "--user"):
            value()
            headers.append({"name": "Authorization", "value": "Basic <dropped>"})
        elif w in DATA:
            d = value()
            is_json = is_json or w == "--json"
            if d.startswith("@") and w != "--data-raw":
                notes.append(f"command {n}: a body read from a file ({w} @...) left out")
            else:
                data.append(d)
        elif w == "--data-urlencode":
            d = value()
            name, eq, content = d.partition("=")
            if not eq and "@" in d:
                notes.append(f"command {n}: a body read from a file (--data-urlencode ...@...) left out")
            else:
                data.append((f"{name}=" if eq and name else "") + quote_plus(content if eq else d))
        elif w in ("-F", "--form", "--form-string"):
            name, _, v = value().partition("=")
            is_file = w != "--form-string" and v.startswith(("@", "<"))
            form.append({"name": name, "value": "" if is_file else v, "fileName": is_file})
        elif w in ("-G", "--get"):
            get = True
        elif w in ("-I", "--head"):
            method = "HEAD"
        elif w == "--url":
            url = value()
        elif w in SKIP_ARG:
            value()
        elif not w.startswith("-") and w.startswith(("http://", "https://")) and not url:
            url = w
        i += 1

    if not url:
        notes.append(f"command {n}: no http(s) URL, left out")
        return None
    url = strip_userinfo(url)
    body = "&".join(data)
    if get and data:
        url += ("&" if "?" in url else "?") + body
        body, data = "", []
    method = method or ("POST" if data or form else "GET")
    if is_json:
        headers.append({"name": "Content-Type", "value": "application/json"})
    mime = next((h["value"] for h in headers if h["name"].lower() == "content-type"), "")
    post: dict[str, Any] = {}
    if form:
        post = {"mimeType": "multipart/form-data", "params": form}
    elif data:
        post = {"mimeType": mime or "application/x-www-form-urlencoded", "text": body}
    return {
        "request": {"method": method, "url": url, "headers": headers, "postData": post},
        # curl shows no response; a JSON one is assumed so the HAR rules keep every call
        "response": {"status": 200, "content": {"mimeType": "application/json"}},
    }
