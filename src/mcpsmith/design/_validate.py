"""Plan validation: the rules in docs/design.md, with messages that say how to fix each problem."""

from __future__ import annotations

import json
import math
import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal

from ..model import (
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
from ..runtime import transforms

PATH_PARAM = re.compile(r"\{([^}]+)\}")


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
    if not plan.base_url.startswith(("http://", "https://")):
        err(
            "plan.base_url",
            f"{plan.base_url!r} is not an absolute URL; set it to e.g. 'https://api.example.com/v1' "
            "(specs read from a file may only have a relative server URL).",
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
    elif plan.auth and plan.auth.session_cookie:
        err("plan.auth.session_cookie", "session_cookie only applies to browser auth.")
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

        names: set[str] = set()
        for arg in tool.args:
            aw = f"{w} arg {arg.name}"
            check(aw, check_arg_name, arg.name)
            if arg.name in names:
                err(aw, "duplicate arg name; give each arg a distinct name.")
            names.add(arg.name)
            check(aw, check_target, arg.to)
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
        for out, name in tool.output.transforms.items():
            ow = f"{w}.output.transforms.{out}"
            if out not in tool.output.fields:
                err(ow, f"{out!r} is not in fields; transform a field you return, or add it to fields.")
            check(ow, lambda v: check_transform(v, transforms.OUTPUT_NAMES), name)
        for key in tool.fixed:
            check(f"{w}.fixed", check_target, key)
        if _nonfinite([tool.fixed, tool.example]):
            err(w, "NaN and infinity are not allowed in fixed or example values.")
        targets = [a.to for a in tool.args] + list(tool.fixed)
        for target in sorted({t for t in targets if targets.count(t) > 1}):
            err(w, f"target {target!r} is set more than once; keep one arg or fixed value per target.")

        missing = [a.name for a in tool.args if a.required and a.name not in tool.example]
        if missing:
            warn(
                f"{w}.example", f"add example values for required args {missing} (used by tests and `mcpsmith check`)."
            )

        ep = source.endpoint(tool.endpoint) if source else None
        method = ep.method.upper() if ep else endpoint_method(tool.endpoint)
        if method in WRITE_METHODS and tool.read_only:
            err(w, f"{method} endpoint marked read_only; set read_only: false (it changes data).")
        elif method and method != "GET" and tool.read_only:
            warn(w, f"{method} endpoint marked read_only; set read_only: false if it changes anything.")
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

        example = ep.response_example
        if example is None:
            if not ep.verified:
                warn(w, "endpoint is unverified (read from app code, no example response); run `mcpsmith probe`.")
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
