"""Cut an API response down to what a tool returns: a root, chosen fields, a list cap, short strings."""

from __future__ import annotations

from typing import Any


def get(data: Any, path: str) -> Any:
    """Follow a dotted path: `a.b`, `images.0.url` (list index), `tags[].name` (map over a list). Missing -> None."""
    if not path:
        return data
    head, _, rest = path.partition(".")
    if head.endswith("[]"):
        items = get(data, head[:-2])
        return [get(x, rest) for x in items] if isinstance(items, list) else None
    if isinstance(data, dict):
        return get(data.get(head), rest) if head in data else None
    if isinstance(data, list) and head.lstrip("-").isdigit() and -len(data) <= int(head) < len(data):
        return get(data[int(head)], rest)
    return None


def shape(
    data: Any, root: str = "", fields: dict[str, str] | None = None, max_items: int | None = None, clip: int = 400
) -> Any:
    """Pick `root`, map each item through `fields` (name -> dotted path; empty keeps everything), cap lists.

    A list becomes {"items": [...], "count": <length before the cap>}; a dict stays a dict. Null and empty values
    are dropped and strings longer than `clip` characters are cut.
    """
    node = get(data, root)

    def pick(item: Any) -> Any:
        return {name: get(item, path) for name, path in fields.items()} if fields else item

    if isinstance(node, list):
        items = node[:max_items] if max_items is not None else node
        return {"items": clean([pick(x) for x in items], clip), "count": len(node)}
    return clean(pick(node) if isinstance(node, dict) else node, clip)


def clean(v: Any, clip: int = 0, top: bool = False) -> Any:
    """Drop None, "", [] and {} (`top` keeps top-level empties: "items": [] says none found), cut strings over `clip`."""
    if isinstance(v, dict):
        out = {k: clean(x, clip) for k, x in v.items()}
        return {k: x for k, x in out.items() if not _empty(x) or top and x is not None}
    if isinstance(v, list):
        return [x for x in (clean(x, clip) for x in v) if not _empty(x)]
    if isinstance(v, str) and clip and len(v) > clip:
        return v[:clip] + "...[cut]"
    return v


def _empty(x: Any) -> bool:
    return x is None or (isinstance(x, str | list | dict) and not x)
