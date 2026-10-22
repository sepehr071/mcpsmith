"""Run a Plan's tools. `run_tool` turns arguments into requests and shapes the answer, for the hub and for
generated servers alike: both pass it a `tool_spec` (a generated server carries it as a literal)."""

from __future__ import annotations

import asyncio
import inspect
import re
from collections.abc import Awaitable, Callable
from typing import Annotated, Any, Literal
from urllib.parse import urljoin, urlsplit

from pydantic import Field

from .. import browser
from ..model import ARG_RESERVED, Plan, Source, Tool, ToolArg
from .http import Api, ApiError
from .shape import apply_transforms, get, shape
from .sql import Db, is_dsn
from .transforms import transform_in

PY_TYPES = {"string": str, "integer": int, "number": float, "boolean": bool, "array": list, "object": dict}
CALL_KWARGS = {"path": "path", "query": "query", "header": "headers", "cookie": "cookies", "body": "body"}
SPEC = {"name", "fixed", "output", "body_encoding", "paginate", "steps"}  # the Tool fields run_tool reads
ARG_SPEC = {"name", "to", "map", "transform"}  # and of each arg


def _endpoint(name: str, endpoint: str, plan: Plan, source: Source | None) -> tuple[str, str]:
    ep = source.endpoint(endpoint) if source else None
    if ep:
        base = ep.base_url.rstrip("/")
        url = f"{base}{ep.path}" if base and base != plan.base_url.rstrip("/") else ep.path
        if not ep.method:
            raise ValueError(
                f"Tool {name}: endpoint {ep.id!r} has no HTTP method (it could not be read from the app); set the method in the plan or source."
            )
        return ep.method.upper(), url
    m = re.fullmatch(r"([A-Za-z]+)\s+(\S+)", endpoint.strip())
    if not m:
        where = "is not in the source" if source else "needs a source"
        raise ValueError(f"Tool {name}: endpoint {endpoint!r} {where}; otherwise write it as 'METHOD /path'.")
    return m.group(1).upper(), m.group(2)


def endpoint_of(t: Tool, plan: Plan, source: Source | None) -> tuple[str, str]:
    """(METHOD, url) for a tool: a path relative to the base URL, or absolute when the endpoint lives elsewhere."""
    return _endpoint(t.name, t.endpoint, plan, source)


def tool_spec(tool: Tool, plan: Plan, source: Source | None) -> dict[str, Any]:
    """What `run_tool` needs of a tool as plain JSON: its mapping, pages, steps and output, plus the resolved
    method and URL of each request, so a generated server needs no source.json."""
    if clash := ARG_RESERVED & {a.name for a in tool.args}:
        raise ValueError(f"Tool {tool.name}: argument name(s) {sorted(clash)} are reserved; rename them.")
    method, url = endpoint_of(tool, plan, source)
    spec = tool.model_dump(mode="json", include={**dict.fromkeys(SPEC, True), "args": {"__all__": ARG_SPEC}})
    for step, s in zip(tool.steps, spec["steps"], strict=True):
        ep = source.endpoint(step.endpoint) if source else None
        s_method, s_url = _endpoint(f"{tool.name} step", step.endpoint, plan, source)
        s |= {"method": s_method, "url": s_url, "body_encoding": ep.body_encoding if ep else "json"}
    if tool.sql is not None:  # only SQL tools carry these, so HTTP specs stay as they were
        spec |= {"sql": tool.sql, "read_only": tool.read_only}
    return {"method": method, "url": url, **spec}


def auth_env(plan: Plan) -> str | None:
    return (plan.auth.env or f"{plan.env_prefix}_TOKEN") if plan.auth else None


def api_kwargs(plan: Plan, source: Source | None) -> dict[str, Any]:
    """The `Api(...)` keyword arguments for a plan (`Db(...)` for a database plan), in the order codegen writes them."""
    if is_dsn(plan.base_url):
        auth = {"auth": {"type": "basic", "env": auth_env(plan)}} if plan.auth else {}
        return {"base_url": plan.base_url, "env_prefix": plan.env_prefix, **auth}
    base_host = urlsplit(plan.base_url).hostname
    urls = (
        _endpoint(t.name, e, plan, source)[1] for t in plan.tools for e in [t.endpoint, *(s.endpoint for s in t.steps)]
    )
    extra = sorted({urlsplit(u).hostname for u in urls if "://" in u} - {base_host})
    kwargs: dict[str, Any] = {"base_url": plan.base_url, "env_prefix": plan.env_prefix}
    if extra:
        kwargs["hosts"] = [base_host, *extra]
    kwargs["headers"] = plan.headers
    if plan.auth and plan.auth.type == "browser":
        kwargs["auth"] = {"type": "browser"}
    elif plan.auth:
        kwargs["auth"] = (
            {"type": plan.auth.type, "env": auth_env(plan)}
            | ({"param": plan.auth.param} if plan.auth.param else {})
            | ({"provider": plan.auth.provider} if plan.auth.provider else {})
        )
    return kwargs | {"impersonate": plan.impersonate, "concurrency": plan.concurrency, "cache_ttl": plan.cache_ttl}


def make_api(
    plan: Plan, source: Source | None, *, proxy: str | None = None, cookie_provider=None, secret=None
) -> Api | Db:
    if is_dsn(plan.base_url):
        return Db(**api_kwargs(plan, source), secret=secret)
    extra: dict[str, Any] = {}
    if plan.auth and plan.auth.type == "browser":

        async def from_chrome(host: str) -> dict[str, str]:
            if not (st := browser.status()):
                raise ConnectionError("mcpsmith Chrome is not running")
            return await browser.cookies_for(host, st["port"])

        extra = {
            "cookie_provider": cookie_provider or from_chrome,
            "login_path": plan.auth.login_path,
            "session_cookie": plan.auth.session_cookie,
        }
    return Api(**api_kwargs(plan, source), secret=secret, proxy=proxy, **extra)


def _value(a: dict[str, Any], v: Any, tz: str) -> Any:
    if a["transform"]:
        return transform_in(a["transform"], v, tz)
    return a["map"].get(v, v) if a["map"] else v


def _kwargs(name: str, pairs: list[tuple[str, Any]], encoding: str) -> dict[str, Any]:
    """The `api.call` keyword arguments (path, query, body, headers, cookies, encoding) from (target, value) pairs."""
    groups: dict[str, dict[str, Any]] = {k: {} for k in CALL_KWARGS}
    whole, has_whole = None, False
    for target, value in pairs:
        if target == "body":
            whole, has_whole = value, True
        else:
            kind, _, key = target.partition(".")
            groups[kind][key] = value
    out: dict[str, Any] = {}
    for kind, items in groups.items():
        if kind == "body" and has_whole:
            if items and not isinstance(whole or {}, dict):
                raise ApiError(f"Tool {name}: a {type(whole).__name__} body cannot also take body.* fields.")
            out["body"] = {**(whole or {}), **items} if items else whole
        elif items:
            out[CALL_KWARGS[kind]] = items
    if encoding != "json":
        out["encoding"] = encoding
    return out


async def run_tool(api: Api | Db, spec: dict[str, Any], values: dict[str, Any], tz: str = "") -> Any:
    """Call a tool: its own request (every page of it with `paginate`), then each step fed by the result before,
    then shape the last result with `output`. `values` maps every arg name to its value (None when unset).
    A SQL tool runs its statement instead, with the sql.<name> values as parameters."""
    wire = {a["name"]: _value(a, values[a["name"]], tz) for a in spec["args"]}
    # an arg without a target feeds only steps ($arg.<name>), never the tool's own request
    pairs = [*((a["to"], wire[a["name"]]) for a in spec["args"] if a["to"]), *spec["fixed"].items()]
    o, pages = spec["output"], None
    if spec.get("sql") is not None:  # validate allows only sql.<name> targets here
        params = {t.partition(".")[2]: v for t, v in pairs}
        data = await api.query(spec["sql"], params, spec["read_only"], o["max_items"] or 100)
    elif spec["paginate"]:
        data, pages = await _pages(api, spec, pairs)
    else:
        data = await api.call(spec["method"], spec["url"], **_kwargs(spec["name"], pairs, spec["body_encoding"]))
    for n, step in enumerate(spec["steps"], 1):
        data = await _step(api, spec["name"], n, step, wire, data)
    # pages are already joined from output.root
    root = "" if spec["paginate"] else o["root"]
    result = shape(data, root=root, fields=o["fields"], max_items=o["max_items"], clip=o["clip"])
    if o["transforms"]:
        result = apply_transforms(result, o["transforms"], tz)
    if pages:
        result |= {"pages": pages, "more": True}  # stopped at max_pages: the model should know there may be more
    return result


async def _pages(api: Api, spec: dict[str, Any], pairs: list[tuple[str, Any]]) -> tuple[list[Any], int | None]:
    """The lists at output.root of each page joined, and the page count when max_pages stopped it (else None)."""
    p, o = spec["paginate"], spec["output"]
    joined: list[Any] = []
    kw: dict[str, Any] = {}
    nxt: Any = None
    for i in range(p["max_pages"]):
        if p["style"] == "next_url" and i:  # Api refuses hosts outside the plan's
            url = urljoin(f"{api.base_url}/", str(nxt))
            data = await api.call("GET", url, headers=kw.get("headers"), cookies=kw.get("cookies"))
        else:
            value = {"page": p["start"] + i, "offset": p["start"] + i * p["size"]}.get(p["style"], nxt)
            kw = _kwargs(spec["name"], [*pairs, *([(p["param"], value)] if p["param"] else [])], spec["body_encoding"])
            data = await api.call(spec["method"], spec["url"], **kw)  # a None cursor (first page) is not sent
        items = get(data, o["root"])
        if not isinstance(items, list) or not items:
            break
        joined += items
        nxt = get(data, p["next"]) if p["next"] else True
        if nxt is None or nxt is False or nxt == "" or (o["max_items"] is not None and len(joined) >= o["max_items"]):
            break
    else:
        return joined, p["max_pages"]
    return joined, None


async def _step(api: Api, name: str, n: int, step: dict[str, Any], wire: dict[str, Any], prev: Any) -> Any:
    """One step: a request per item of `for_each` (the list of their results), else one request on `prev`."""

    async def call(item: Any = None) -> Any:
        refs = {"arg": wire, "prev": prev, "item": item}
        pairs = [*step["fixed"].items()]
        for target, ref in step["args"].items():
            kind, _, path = ref[1:].partition(".")
            pairs.append((target, get(refs[kind], path)))
        kw = _kwargs(f"{name} step {n}", pairs, step["body_encoding"])
        try:
            return await api.call(step["method"], step["url"], **kw)
        except ApiError as e:
            raise ApiError(f"Tool {name} step {n} ({step['endpoint']}): {e}", e.status, e.expired) from e

    if not step["for_each"]:
        return await call()
    items = get(prev, step["for_each"])
    items = items[: step["max_items"]] if isinstance(items, list) else []
    return list(await asyncio.gather(*map(call, items)))  # Api caps how many run at once


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
    """An async function named `tool.name` with the generated one's signature; both run through `run_tool`."""
    spec = tool_spec(tool, plan, source)
    sig = inspect.Signature([_param(a) for a in sorted(tool.args, key=lambda a: not a.required)])

    async def run(**kwargs: Any) -> Any:
        bound = sig.bind(**kwargs)
        bound.apply_defaults()
        return await run_tool(api, spec, bound.arguments, plan.timezone)

    run.__name__ = run.__qualname__ = tool.name
    run.__doc__ = tool.description.strip()
    run.__signature__ = sig
    run.__annotations__ = {p.name: p.annotation for p in sig.parameters.values()}
    return run
