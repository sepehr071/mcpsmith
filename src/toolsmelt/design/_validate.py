"""Plan validation: the rules in docs/design.md, with messages that say how to fix each problem."""

from __future__ import annotations

import json
import math
import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal
from urllib.parse import urlsplit

from ..model import (
    STEP_REF,
    WRITE_METHODS,
    Plan,
    Source,
    check_arg_name,
    check_env,
    check_name,
    check_prefix,
    check_repository,
    check_target,
    check_timezone,
    check_tool_name,
    check_transform,
    endpoint_method,
)
from ..runtime import signin, transforms
from ..runtime.grpc import AUTH_TYPES as GRPC_AUTH
from ..runtime.grpc import is_grpc
from ..runtime.sql import is_dsn, placeholders, read_only_problem

PATH_PARAM = re.compile(r"\{([^}]+)\}")
MAX_REQUESTS = 50  # per tool call, pages x steps x for_each items
VERSION = re.compile(r"\d+\.\d+\.\d+")
NEWLINE = re.compile(r"[\r\n\x85\u2028\u2029]")
# Values that have no wire form there: an object in a query is sent as a Python repr; headers and cookies are text.
NO_WIRE_FORM = {"object": ("query", "header", "cookie"), "array": ("header", "cookie")}


@dataclass
class Issue:
    level: Literal["error", "warning"]
    where: str
    message: str


def resolve(data: Any, path: str) -> Any:
    """Follow a dotted path (`a.b`, `items.0.url`, `tags[].name`); a missing step gives None."""
    if not path:
        return data
    parts = path.split(".")
    for i, part in enumerate(parts):
        many = part.endswith("[]")
        key = part[:-2] if many else part
        if key:
            if isinstance(data, dict):
                data = data.get(key)
            elif isinstance(data, list) and key.isdigit() and int(key) < len(data):
                data = data[int(key)]
            else:
                return None
        if many:
            if not isinstance(data, list):
                return None
            rest = ".".join(parts[i + 1 :])
            return [resolve(x, rest) for x in data]
    return data


def _empty(v: Any) -> bool:
    if isinstance(v, list):
        return all(_empty(x) for x in v)
    return v is None


def _nonfinite(v: Any) -> bool:
    if isinstance(v, float):
        return not math.isfinite(v)
    if isinstance(v, list | tuple):
        return any(map(_nonfinite, v))
    if isinstance(v, dict):
        return any(map(_nonfinite, v.values()))
    return False


def validate(plan: Plan, source: Source | None = None) -> list[Issue]:
    """All problems of a plan. Repeats the model's syntax rules: assignment and `model_copy` skip its validators."""
    issues: list[Issue] = []

    def err(where: str, msg: str) -> None:
        issues.append(Issue("error", where, msg))

    def warn(where: str, msg: str) -> None:
        issues.append(Issue("warning", where, msg))

    if len(plan.description) > 100:
        err("plan.description", f"{len(plan.description)} chars; shorten it to at most 100 (MCP Registry limit).")
    rpc = is_grpc(plan.base_url)  # a gRPC plan: endpoints 'RPC pkg.Svc/Method', values sent as message and metadata
    db = is_dsn(plan.base_url) and not rpc  # a database plan: its tools run SQL (grpc:// also looks like a DSN)
    if db:
        if plan.auth and plan.auth.type != "basic":
            err("plan.auth", f"a database plan's auth is {{type: basic, env: {plan.env_prefix}_DB_PASSWORD}}.")
    elif rpc:
        if not urlsplit(plan.base_url).hostname:
            err("plan.base_url", f"{plan.base_url!r} has no host; set it to e.g. 'grpcs://api.example.com:443'.")
        if plan.auth and plan.auth.type not in GRPC_AUTH:
            err("plan.auth", f"gRPC sends credentials as metadata: auth type must be one of {sorted(GRPC_AUTH)}.")
        elif plan.auth and plan.auth.type == "header" and not plan.auth.param:
            err("plan.auth.param", "gRPC header auth needs param: the metadata key that carries the credential.")
    elif not plan.base_url.startswith(("http://", "https://")):
        err(
            "plan.base_url",
            f"{plan.base_url!r} is not an absolute URL; set it to e.g. 'https://api.example.com/v1' "
            "(specs read from a file may only have a relative server URL).",
        )
    elif not urlsplit(plan.base_url).hostname:
        err("plan.base_url", f"{plan.base_url!r} has no host; set it to e.g. 'https://api.example.com/v1'.")
    base_host = urlsplit(plan.base_url).hostname
    if not VERSION.fullmatch(plan.version):
        err("plan.version", f"{plan.version!r} must look like X.Y.Z, e.g. '0.1.0' (it goes into pyproject.toml).")
    for where, text in (("plan.title", plan.title), ("plan.description", plan.description)):
        if NEWLINE.search(text):
            err(where, "must be one line; it is inserted into README.md and SKILL.md.")
    if not plan.repository:
        warn(
            "plan.repository",
            f"empty: the generated server.json is named 'io.github.example/{plan.name}', which the MCP Registry "
            "refuses; set it to https://github.com/<owner>/<repo> before publishing.",
        )

    def check(where: str, fn: Callable[[str], Any], value: str) -> None:
        try:
            fn(value)
        except ValueError as e:
            err(where, str(e))

    check("plan.name", check_name, plan.name)
    check("plan.prefix", check_prefix, plan.prefix)
    check("plan.env_prefix", check_env, plan.env_prefix)
    if not plan.env_prefix.endswith("_MCP"):  # else env_prefix 'ANTHROPIC' + auth.env 'ANTHROPIC_API_KEY' leaks a key
        err("plan.env_prefix", f"{plan.env_prefix!r} must end with '_MCP', e.g. 'PETSTORE_MCP'.")
    check("plan.repository", check_repository, plan.repository)
    if plan.concurrency < 1 or plan.cache_ttl < 0:
        err("plan", "concurrency must be at least 1 and cache_ttl at least 0.")
    if plan.auth and plan.auth.env:
        check("plan.auth.env", lambda v: check_env(v, "auth.env"), plan.auth.env)
        if not plan.auth.env.startswith(f"{plan.env_prefix}_"):
            err(
                "plan.auth.env",
                f"{plan.auth.env!r} must start with '{plan.env_prefix}_' so a plan cannot read other credentials; "
                f"rename it, e.g. '{plan.env_prefix}_TOKEN'.",
            )
    check("plan.timezone", check_timezone, plan.timezone)
    if plan.auth and plan.auth.type == "browser":
        warn("plan.auth", "browser auth needs the hub's Chrome; switch to token auth before exporting.")
        if not plan.auth.session_cookie:
            warn(
                "plan.auth.session_cookie",
                "set auth.session_cookie so an ended session is reported instead of returning empty data.",
            )
    elif plan.auth and plan.auth.type == "signin":
        if plan.auth.provider not in signin.PROVIDERS:
            err("plan.auth.provider", f"signin auth needs a provider, one of {sorted(signin.PROVIDERS)}.")
        if f"{plan.prefix}_login" in {t.name for t in plan.tools}:
            err("plan.tools", f"{plan.prefix}_login is the sign-in tool signin auth adds; rename that tool.")
    if plan.auth and plan.auth.type != "browser" and plan.auth.session_cookie:
        err("plan.auth.session_cookie", "session_cookie only applies to browser auth.")
    if plan.auth and plan.auth.type != "signin" and plan.auth.provider:
        err("plan.auth.provider", "provider only applies to signin auth.")
    texts = [plan.instructions, *(t.description for t in plan.tools)]
    if hub_only := sorted({m for text in texts for m in re.findall(r"\bsmith_\w+", text)}):
        login = f"; its sign-in tool is {plan.prefix}_login" if plan.auth and plan.auth.type == "signin" else ""
        warn(
            "plan.instructions",
            f"mentions {', '.join(hub_only)}: toolsmelt hub tools a generated server does not have{login}.",
        )
    if len(plan.tools) > 20:
        warn("plan.tools", f"{len(plan.tools)} tools; merge near-duplicates and drop noise to stay at 20 or fewer.")

    seen: set[str] = set()
    for tool in plan.tools:
        w = f"tool {tool.name}"
        check(w, check_tool_name, tool.name)
        if not tool.name.startswith(plan.prefix):
            err(w, f"name must start with the plan prefix: rename it to '{plan.prefix}_...'.")
        if tool.name in seen:
            err(w, "duplicate tool name; rename or merge the tools.")
        seen.add(tool.name)
        if not (tool.description.strip().splitlines() or [""])[0].endswith("."):
            warn(f"{w}.description", "make the first line one complete sentence ending with '.'.")
        if NEWLINE.search(tool.title):
            err(f"{w}.title", "must be one line.")

        names: set[str] = set()
        stepped = {r[5:].split(".")[0] for s in tool.steps for r in s.args.values() if r.startswith("$arg.")}
        for arg in tool.args:
            aw = f"{w} arg {arg.name}"
            check(aw, check_arg_name, arg.name)
            if arg.name in names:
                err(aw, "duplicate arg name; give each arg a distinct name.")
            names.add(arg.name)
            if arg.to:
                check(aw, check_target, arg.to)
            elif arg.name not in stepped:
                err(aw, f"no target; set to (e.g. 'query.{arg.name}') or use it in a step as '$arg.{arg.name}'.")
            if arg.transform is not None:
                check(aw, lambda v: check_transform(v, transforms.INPUT_NAMES), arg.transform)
            if arg.required and arg.default is not None:
                err(aw, "required arg has a default; drop the default or make the arg optional.")
            if arg.map and arg.enum and set(arg.enum) - set(arg.map):
                err(aw, "every enum value needs a key in map; drop enum (map keys become the allowed values).")
            if arg.map and arg.default is not None and arg.default not in arg.map:
                err(aw, f"default {arg.default!r} is not a key of map; use one of {list(arg.map)}.")
            if _nonfinite([arg.default, arg.enum, arg.map, arg.minimum, arg.maximum]):
                err(aw, "NaN and infinity are not allowed in default, enum, map, minimum or maximum.")
            if arg.transform is not None and arg.map:
                err(aw, "transform and map on one arg: the map would be ignored; keep one of them.")
            if arg.to.partition(".")[0] in NO_WIRE_FORM.get(arg.type, ()):
                err(aw, f"an {arg.type} arg cannot go to {arg.to!r}; map its fields to separate args or use the body.")
        for out, name in tool.output.transforms.items():
            ow = f"{w}.output.transforms.{out}"
            if out not in tool.output.fields:
                err(ow, f"{out!r} is not in fields; transform a field you return, or add it to fields.")
            check(ow, lambda v: check_transform(v, transforms.OUTPUT_NAMES), name)
        for key in tool.fixed:
            check(f"{w}.fixed", check_target, key)
        if _nonfinite([tool.fixed, tool.example]):
            err(w, "NaN and infinity are not allowed in fixed or example values.")
        targets = [a.to for a in tool.args if a.to] + list(tool.fixed)
        for target in sorted({t for t in targets if targets.count(t) > 1}):
            err(w, f"target {target!r} is set more than once; keep one arg or fixed value per target.")
        given = {t[4:] for t in targets if t.startswith("sql.")}
        if tool.sql is None:
            if db:
                err(w, "a database plan's tools need sql: one statement with :name placeholders.")
            elif given:
                err(w, "sql.<name> targets need the tool's sql statement, which a database plan runs.")
        else:
            if not db:
                err(w, "sql tools need a database base_url, e.g. 'sqlite:///C:/data/shop.db' or 'postgresql://u@h/db'.")
            if other := sorted(t for t in targets if not t.startswith("sql.")):
                err(w, f"{other} are HTTP targets; a sql tool takes only sql.<name> values.")
            if missing := [n for n in placeholders(tool.sql) if n not in given]:
                err(
                    f"{w}.sql",
                    f"placeholder(s) {[':' + n for n in missing]} have no arg or fixed with to='sql.<name>'.",
                )
            if unused := sorted(given - set(placeholders(tool.sql))):
                err(w, f"{['sql.' + n for n in unused]} match no :placeholder in the statement.")
            if tool.read_only and (problem := read_only_problem(tool.sql)):
                err(f"{w}.sql", f"the statement of a read_only tool {problem}.")
            if tool.paginate or tool.steps:
                err(w, "sql tools cannot paginate or have steps; use LIMIT/OFFSET args in one statement.")
        body_keys = [t for t in targets if t.startswith("body.")]
        whole_types = [a.type for a in tool.args if a.to == "body"]
        if "body" in tool.fixed:
            whole_types.append("object" if isinstance(tool.fixed["body"], dict) else "value")
        if body_keys and any(t != "object" for t in whole_types):
            err(w, "a whole-body value that is not an object cannot be combined with body.<key> targets.")
        if tool.body_encoding != "json" and "body" in targets:
            err(w, f"body_encoding {tool.body_encoding} sends named fields; use body.<name> targets, not 'body'.")
        if tool.body_encoding in ("xml", "soap"):
            if len(roots := sorted({t[5:].split(".")[0] for t in body_keys})) != 1:
                err(
                    w,
                    f"body_encoding {tool.body_encoding} sends one XML document: put every body target under one root "
                    f"element, body.<Root>.<field> (found {roots or 'none'}).",
                )
            if tool.body_encoding == "soap" and "header.soapaction" not in {t.lower() for t in targets}:
                warn(w, "no SOAPAction: most SOAP services need the operation's action as fixed header.SOAPAction.")
        if rpc:
            step_targets = [k for s in tool.steps for k in [*s.args, *s.fixed]]
            if bad := sorted({t for t in targets + step_targets if t.partition(".")[0] in ("path", "query", "cookie")}):
                err(w, f"{bad}: a gRPC call takes body.<field> (message fields) and header.<key> (metadata) only.")
            if tool.body_encoding != "json":
                err(w, "gRPC messages are sent from JSON fields; set body_encoding: json.")
            if tool.paginate and tool.paginate.style == "next_url":
                err(f"{w}.paginate", "gRPC has no next URL; use style cursor with the response's page token field.")
        if p := tool.paginate:
            pw = f"{w}.paginate"
            if p.style != "next_url" and not p.param:
                err(pw, f"style {p.style} needs param: the target that carries it, e.g. 'query.page'.")
            if p.param:
                check(pw, check_target, p.param)
                if p.param in targets:
                    err(pw, f"param {p.param!r} is also set by an arg or fixed value; the page would overwrite it.")
            if p.style in ("cursor", "next_url") and not p.next:
                err(pw, f"style {p.style} needs next: the response path of the next cursor or URL, e.g. 'next'.")
            if p.style == "offset" and p.size <= 0:
                err(pw, "style offset needs size > 0: the items per page the endpoint returns.")
            if not tool.output.root:
                err(pw, "paginate needs output.root: the path of the list that pages are joined from, e.g. 'hits'.")
            if tool.steps:
                err(pw, "paginate and steps cannot be combined (output.root names the pages' list); use two tools.")
        requests = 1 + sum(s.max_items if s.for_each else 1 for s in tool.steps)
        if requests * (tool.paginate.max_pages if tool.paginate else 1) > MAX_REQUESTS:
            err(w, f"one call could make over {MAX_REQUESTS} requests; lower max_items or max_pages, or drop steps.")
        for n, step in enumerate(tool.steps, 1):
            sw = f"{w} step {n}"
            for key in [*step.args, *step.fixed]:
                check(sw, check_target, key)
            if dup := sorted(set(step.args) & set(step.fixed)):
                err(sw, f"target(s) {dup} are set in both args and fixed; keep one.")
            for target, ref in step.args.items():
                m = STEP_REF.fullmatch(ref)
                kind, path = (m.group(1), (m.group(2) or "")[1:]) if m else ("", "")
                if not m:
                    err(sw, f"{target}: {ref!r} must be '$arg.<name>', '$prev.<path>' or '$item.<path>'.")
                elif kind == "arg" and path not in names:
                    err(
                        sw,
                        f"{target}: {ref!r} names no arg of this tool; use '$arg.<name>' with one of {sorted(names)}.",
                    )
                elif kind == "item" and not step.for_each:
                    err(sw, f"{target}: {ref!r} needs for_each (the list whose items it reads).")
            sep = source.endpoint(step.endpoint) if source else None
            if source and sep is None:
                err(sw, f"unknown endpoint {step.endpoint!r}; use an endpoint id from the digest.")
                continue
            smethod = sep.method.upper() if sep else endpoint_method(step.endpoint)
            if sep and not smethod:
                err(sw, f"endpoint {sep.id!r} has no HTTP method (not readable from the app); set it in the source.")
            if tool.read_only and smethod and smethod != "GET" and not rpc:  # an RPC's read_only is the designer's
                err(sw, f"{smethod} step in a read_only tool; set read_only: false or use a GET endpoint.")
            spath = sep.path if sep else (step.endpoint.split()[-1] if smethod else "")
            for param in PATH_PARAM.findall(spath):
                if f"path.{param}" not in [*step.args, *step.fixed]:
                    err(
                        sw,
                        f"path parameter {{{param}}} has no value; add args 'path.{param}': '$prev.<path>' or fixed.",
                    )

        missing = [a.name for a in tool.args if a.required and a.name not in tool.example]
        if missing:
            warn(
                f"{w}.example", f"add example values for required args {missing} (used by tests and `toolsmelt check`)."
            )
        if unknown := sorted(set(tool.example) - names):
            warn(f"{w}.example", f"{unknown} are not args of this tool; drop them or add the args.")

        ep = source.endpoint(tool.endpoint) if source else None
        method = None if tool.sql is not None else ep.method.upper() if ep else endpoint_method(tool.endpoint)
        if rpc:  # a method name says nothing reliable about writes
            if "read_only" not in tool.model_fields_set:
                (err if "schema_version" in plan.model_fields_set else warn)(
                    w, "set read_only explicitly for a gRPC method: true only if it changes nothing."
                )
        elif method in WRITE_METHODS and tool.read_only:
            err(w, f"{method} endpoint marked read_only; set read_only: false (it changes data).")
        elif (
            method == "POST" and "read_only" not in tool.model_fields_set and "schema_version" in plan.model_fields_set
        ):
            # plans from before schema_version (0.5 and older) keep the warning below, so saved connections still load
            err(w, "set read_only explicitly: true for a search sent as POST, false if it changes data.")
        elif method and method != "GET" and tool.read_only:
            warn(w, f"{method} endpoint marked read_only; set read_only: false if it changes anything.")
        if method == "GET" and (body_keys or "body" in targets):
            err(w, "GET requests send no body; move body values to query.<name> or use the right endpoint.")
        url = ep.base_url if ep else ("" if source else (tool.endpoint.split() or [""])[-1])
        if (host := urlsplit(url).hostname) and host != base_host:
            warn(f"{w}.endpoint", f"goes to {host}, not the base_url host {base_host}; check that host is the API's.")
        if source is None:
            continue
        if ep is None:
            err(f"{w}.endpoint", f"unknown endpoint {tool.endpoint!r}; use an endpoint id from the digest.")
            continue
        if not ep.method:
            err(
                f"{w}.endpoint",
                f"endpoint {ep.id!r} has no HTTP method (not readable from the app); set the method in the plan.",
            )
        for p in PATH_PARAM.findall(ep.path):
            if f"path.{p}" not in targets:
                err(w, f"path parameter {{{p}}} has no value; add an arg with to='path.{p}' or a fixed value.")
        for t in targets:
            if t.startswith("path.") and t[5:] not in PATH_PARAM.findall(ep.path):
                err(w, f"{t!r} has no {{{t[5:]}}} in the endpoint path {ep.path!r}; the value would be dropped.")

        example = ep.response_example
        if example is None:
            if not ep.verified:
                warn(w, "endpoint is unverified (read from app code, no example response); run `toolsmelt probe`.")
            continue
        if not tool.output.fields and len(json.dumps(example, ensure_ascii=False)) > 4096:
            warn(f"{w}.output", "response example is over 4 KB; pick 5-15 useful `fields` instead of returning all.")
        base = resolve(example, tool.output.root)
        if _empty(base):
            warn(
                f"{w}.output.root",
                f"root {tool.output.root!r} resolves to nothing in the response example; fix the path.",
            )
            continue
        if isinstance(base, list):
            base = base[0] if base else None
        if base is None:
            continue
        for out, path in tool.output.fields.items():
            if _empty(resolve(base, path)):
                warn(f"{w}.output.fields.{out}", f"path {path!r} resolves to nothing in the response example; fix it.")
    return issues
