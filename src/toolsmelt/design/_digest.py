"""Compact markdown view of a Source for an LLM designing tools."""

from __future__ import annotations

import json
from typing import Any

from ..model import Endpoint, Source

# Detail levels per endpoint: (string clip, skeleton depth, body clip); level 3 = no examples, 4 = dropped.
LEVELS = [(60, 6, 600), (40, 4, 300), (24, 3, 150)]
BRIEF, DROPPED = 3, 4


def _clip(s: str, n: int) -> str:
    s = " ".join(s.split())
    return s if len(s) <= n else s[: n - 1] + "…"


def _json(v: Any) -> str:
    return json.dumps(v, ensure_ascii=False, separators=(",", ":"), default=str)


def skeleton(v: Any, clip: int = 60, depth: int = 6) -> Any:
    """The shape of a response: lists cut to one item, strings clipped, deep values elided."""
    if isinstance(v, dict):
        return {k: skeleton(x, clip, depth - 1) for k, x in v.items()} if depth > 0 else "{…}"
    if isinstance(v, list):
        return ([skeleton(v[0], clip, depth - 1)] if v else []) if depth > 0 else "[…]"
    if isinstance(v, str):
        return _clip(v, clip)
    return v


def list_paths(v: Any, path: str = "") -> list[tuple[str, int]]:
    """Dotted paths of every list in a response (nested lists via the first item, as `a[].b`), with lengths."""
    out: list[tuple[str, int]] = []
    if isinstance(v, list):
        out.append((path, len(v)))
        if v:
            out += list_paths(v[0], f"{path}[]" if path else "[]")
    elif isinstance(v, dict):
        for k, x in v.items():
            out += list_paths(x, f"{path}.{k}" if path else k)
    return out


def _rank(e: Endpoint) -> tuple:
    words = f"{e.id} {e.path} {e.summary}".lower()
    listy = bool(list_paths(e.response_example)) or "search" in words or "list" in words
    return (e.method.upper() != "GET", -e.count, not listy, e.path, e.method)


def _table(e: Endpoint, level: int) -> str:
    """A database table: its columns with type and keys, then sample rows, one compact JSON row per line."""
    lines = [f"### {e.id}" + (f"\n{e.description}" if e.description else "")]
    if level >= BRIEF:
        return "\n".join([*lines, "columns: " + ", ".join(p.name for p in e.params)])
    lines.append("columns: " + "; ".join(f"{p.name} {p.description}" for p in e.params))
    clip, depth, body_clip = LEVELS[level]
    rows = e.response_example if isinstance(e.response_example, list) else []
    lines += [_clip(_json(skeleton(r, clip, depth)), body_clip) for r in rows]
    return "\n".join(lines)


def _endpoint(e: Endpoint, level: int) -> str:
    if e.method == "SQL":
        return _table(e, level)
    head = f"### {e.id}" if e.method == "RPC" else f"### {e.id}\n{e.method.upper()} {e.path}"  # the id is the method
    if e.count > 1:
        head += f"  (seen {e.count}x)"
    if not e.verified:
        head += "  UNVERIFIED (read from app code, no response seen)"
    if e.base_url:
        head += f"  base: {e.base_url}"
    if e.auth:
        head += f"  auth: {', '.join(e.auth)}"
    lines = [head]
    text = " — ".join(x for x in (e.summary, _clip(e.description, 200) if e.description != e.summary else "") if x)
    if text:
        lines.append(_clip(text, 240 if level < BRIEF else 100))
    if level >= BRIEF:
        if e.params:
            lines.append("params: " + ", ".join(p.name + ("*" if p.required else "") for p in e.params))
        return "\n".join(lines)
    clip, depth, body_clip = LEVELS[level]
    for p in e.params:
        bits = [p.location, p.type] + (["required"] if p.required else [])
        line = f"- {p.name} ({', '.join(bits)})"
        if p.enum:
            line += " enum: " + _clip(_json(p.enum), 120)
        if p.example is not None:
            line += " e.g. " + _clip(_json(p.example), 60)
        elif p.default is not None:
            line += " default " + _clip(_json(p.default), 60)
        if p.description:
            line += " — " + _clip(p.description, 100)
        lines.append(line)
    if e.body_example is not None:
        lines.append("body: " + _clip(_json(skeleton(e.body_example, clip, depth)), body_clip))
    if e.response_example is not None:
        lists = [f"`{p or '(top level)'}` [{n}]" for p, n in list_paths(e.response_example)]
        lines.append("response" + (" (lists: " + ", ".join(lists[:6]) + ")" if lists else "") + ":")
        lines.append(_json(skeleton(e.response_example, clip, depth)))
    return "\n".join(lines)


def digest(source: Source, budget: int = 60_000) -> str:
    head = [f"# {source.title}", f"kind: {source.kind} | base_url: {source.base_url}"]
    if source.description:
        head.append(_clip(source.description, 300))
    for a in source.auth:
        head.append(f"auth: {a.name} ({a.type}{', ' + a.param if a.param else ''})")
    if source.headers:
        head.append("headers: " + "; ".join(f"{k}: {_clip(v, 60)}" for k, v in source.headers.items()))
    eps = sorted(source.endpoints, key=_rank)
    if source.kind == "sql":
        head.append(f"\n## Tables ({len(eps)}, most rows first; sample rows below the columns, personal values masked)")
    elif source.kind == "grpc":
        head.append(
            f"\n## Methods ({len(eps)} unary; request fields are body params with their proto types; responses are "
            "skeletons of the response message, not real data)"
        )
    else:
        head.append(f"\n## Endpoints ({len(eps)}, GET first, most used first; `*` = required)")
    header = "\n".join(head)

    levels = [0] * len(eps)
    parts = [_endpoint(e, 0) for e in eps]

    def size() -> int:
        return len(header) + sum(len(p) + 2 for p, lv in zip(parts, levels, strict=True) if lv < DROPPED) + 120

    # Shrink every skeleton first, then strip the least useful endpoints (end of the list) one step at a time.
    for lv in range(1, len(LEVELS)):
        if size() <= budget:
            break
        levels = [lv] * len(eps)
        parts = [_endpoint(e, lv) for e in eps]
    for target in (BRIEF, DROPPED):
        for i in reversed(range(len(eps))):
            if size() <= budget:
                break
            levels[i] = target
            if target == BRIEF:
                parts[i] = _endpoint(eps[i], BRIEF)

    body = [p for p, lv in zip(parts, levels, strict=True) if lv < DROPPED]
    text = "\n\n".join([header, *body])
    dropped, brief = levels.count(DROPPED), levels.count(BRIEF)
    notes = []
    if brief:
        notes.append(f"{brief} endpoints shown without examples")
    if dropped:
        notes.append(f"{dropped} endpoints omitted to fit the budget")
    note = f"\n\n_({'; '.join(notes)}.)_" if notes else ""
    if len(text) + len(note) > budget:
        text = text[: max(0, budget - len(note) - 1)] + "…"
    return text + note
