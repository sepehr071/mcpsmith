"""Run a Plan directly as tools, without generating code. Mirrors what `codegen` writes, request for request."""

from __future__ import annotations

import inspect
import re
from collections.abc import Awaitable, Callable
from typing import Annotated, Any, Literal
from urllib.parse import urlsplit

from pydantic import Field

from ..model import ARG_RESERVED, Plan, Source, Tool, ToolArg
from .http import Api
from .shape import apply_transforms, shape
from .transforms import transform_in

PY_TYPES = {"string": str, "integer": int, "number": float, "boolean": bool, "array": list, "object": dict}
CALL_KWARGS = {"path": "path", "query": "query", "header": "headers", "cookie": "cookies", "body": "body"}


def endpoint_of(t: Tool, plan: Plan, source: Source | None) -> tuple[str, str]:
    """(METHOD, url) for a tool: a path relative to the base URL, or absolute when the endpoint lives elsewhere."""
    ep = source.endpoint(t.endpoint) if source else None
    if ep:
        base = ep.base_url.rstrip("/")
        url = f"{base}{ep.path}" if base and base != plan.base_url.rstrip("/") else ep.path
        return ep.method.upper(), url
    m = re.fullmatch(r"([A-Za-z]+)\s+(\S+)", t.endpoint.strip())
    if not m:
        where = "is not in the source" if source else "needs a source"
        raise ValueError(f"Tool {t.name}: endpoint {t.endpoint!r} {where}; otherwise write it as 'METHOD /path'.")
    return m.group(1).upper(), m.group(2)


def auth_env(plan: Plan) -> str | None:
    return (plan.auth.env or f"{plan.env_prefix}_TOKEN") if plan.auth else None


def api_kwargs(plan: Plan, source: Source | None) -> dict[str, Any]:
    """The `Api(...)` keyword arguments for a plan, in the order codegen writes them."""
    base_host = urlsplit(plan.base_url).hostname
    urls = (endpoint_of(t, plan, source)[1] for t in plan.tools)
    extra = sorted({urlsplit(u).hostname for u in urls if "://" in u} - {base_host})
    kwargs: dict[str, Any] = {"base_url": plan.base_url, "env_prefix": plan.env_prefix}
    if extra:
        kwargs["hosts"] = [base_host, *extra]
    kwargs["headers"] = plan.headers
    if plan.auth:
        kwargs["auth"] = {"type": plan.auth.type, "env": auth_env(plan)} | (
            {"param": plan.auth.param} if plan.auth.param else {}
        )
    return kwargs | {"impersonate": plan.impersonate, "concurrency": plan.concurrency, "cache_ttl": plan.cache_ttl}


def make_api(plan: Plan, source: Source | None, *, proxy: str | None = None, cookie_provider=None, secret=None) -> Api:
    # proxy and cookie_provider are accepted for the hub; Task 7 wires them into Api.
    return Api(**api_kwargs(plan, source), secret=secret)


def _value(a: ToolArg, v: Any, tz: str) -> Any:
    if a.transform:
        return transform_in(a.transform, v, tz)
    return a.map.get(v, v) if a.map else v


def request_kwargs(tool: Tool, values: dict[str, Any], tz: str) -> dict[str, Any]:
    """The `api.call` keyword arguments (path, query, body, headers, cookies) for one tool call."""
    if clash := ARG_RESERVED & {a.name for a in tool.args}:
        raise ValueError(f"Tool {tool.name}: argument name(s) {sorted(clash)} are reserved; rename them.")
    groups: dict[str, dict[str, Any]] = {k: {} for k in CALL_KWARGS}
    whole, has_whole = None, False
    for target, value in [*((a.to, _value(a, values[a.name], tz)) for a in tool.args), *tool.fixed.items()]:
        if target == "body":
            whole, has_whole = value, True
        else:
            kind, _, key = target.partition(".")
            groups[kind][key] = value
    out: dict[str, Any] = {}
    for kind, items in groups.items():
        if kind == "body" and has_whole:
            out["body"] = {**(whole or {}), **items} if items else whole
        elif items:
            out[CALL_KWARGS[kind]] = items
    return out


def _param(a: ToolArg) -> inspect.Parameter:
    values = a.enum or (list(a.map) if a.map else None)
    kind: Any = Literal[tuple(values)] if values else PY_TYPES[a.type]
    field: dict[str, Any] = {"description": a.description}
    for key, bound in (("ge", a.minimum), ("le", a.maximum)):
        if bound is not None:
            field[key] = int(bound) if float(bound).is_integer() else bound
    if a.required:
        return inspect.Parameter(a.name, inspect.Parameter.KEYWORD_ONLY, annotation=Annotated[kind, Field(**field)])
    kind = kind | None if a.default is None else kind
    return inspect.Parameter(
        a.name, inspect.Parameter.KEYWORD_ONLY, default=a.default, annotation=Annotated[kind, Field(**field)]
    )


def make_tool(tool: Tool, plan: Plan, source: Source | None, api: Api) -> Callable[..., Awaitable[Any]]:
    """An async function named `tool.name` that calls the endpoint and shapes the result, like the generated one."""
    method, url = endpoint_of(tool, plan, source)
    o, tz = tool.output, plan.timezone
    sig = inspect.Signature([_param(a) for a in sorted(tool.args, key=lambda a: not a.required)])

    async def run(**kwargs: Any) -> Any:
        bound = sig.bind(**kwargs)
        bound.apply_defaults()
        data = await api.call(method, url, **request_kwargs(tool, bound.arguments, tz))
        result = shape(data, root=o.root, fields=o.fields, max_items=o.max_items, clip=o.clip)
        return apply_transforms(result, o.transforms, tz) if o.transforms else result

    run.__name__ = run.__qualname__ = tool.name
    run.__doc__ = tool.description.strip()
    run.__signature__ = sig
    run.__annotations__ = {p.name: p.annotation for p in sig.parameters.values()}
    return run
