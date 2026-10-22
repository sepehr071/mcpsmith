"""The two documents every stage passes along.

`Source` is what an importer understood about an API (from an OpenAPI file or captured traffic).
`Plan` is the tool design: which tools the MCP server exposes, how their arguments map onto requests
and which response fields they return. An LLM (the developer's agent, or `toolsmelt design`) writes the
plan; code generation reads only the plan, plus the source for test fixtures.
"""

from __future__ import annotations

import json
import keyword
import re
import sys
from pathlib import Path
from typing import Any, Literal
from zoneinfo import ZoneInfo

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator

from .runtime import transforms

ParamIn = Literal["path", "query", "header", "cookie", "body"]
ArgType = Literal["string", "integer", "number", "boolean", "array", "object"]
AuthType = Literal["bearer", "header", "query", "cookie", "basic", "browser", "signin"]
BodyEncoding = Literal["json", "form", "multipart"]
SCHEMA_VERSION = 1  # plan.yaml format; raise it (and migrate in load_plan) when a change breaks old plans
# Plan models refuse unknown keys: a typo such as 'readonly: false' must fail, not leave a write tool read-only.
STRICT = ConfigDict(extra="forbid")

# Every pattern is used with fullmatch: `$` would accept a trailing newline.
PLAN_NAME = re.compile(r"[a-z0-9][a-z0-9-]{0,62}")
PREFIX = re.compile(r"[a-z][a-z0-9_]*")
ENV_NAME = re.compile(r"[A-Z][A-Z0-9_]*")
TOOL_NAME = re.compile(r"[a-z][a-z0-9_]{0,63}")
ARG_NAME = re.compile(r"[A-Za-z][A-Za-z0-9_]*")
# Where an argument or fixed value goes: path.<name> | query.<name> | header.<name> | cookie.<name> | body | body.<a.b.c>
# Split on the first dot only: query names may contain dots ('query.filters.minPrice').
TARGET = re.compile(r"(path|query|header|cookie|sql)\.\S+|body(\.\S+)?")  # sql.<n>: a :n placeholder of Tool.sql
REPO = re.compile(r"https://github\.com/([\w.-]+)/([\w.-]+)", re.ASCII)
TOOL_RESERVED = {"api", "tool", "mcp", "main", "run", "shape"}  # module-level names of the generated server
ARG_RESERVED = {"api", "shape"}  # names the generated tool bodies use
# A generated package must not shadow what the generated code or its tests import.
PACKAGE_RESERVED = set(sys.stdlib_module_names) | {"toolsmelt", "mcp", "pydantic", "httpx", "pytest", "anyio", "yaml"}
WRITE_METHODS = {"DELETE", "PUT", "PATCH"}


def package_of(name: str) -> str:
    return name.replace("-", "_")


def endpoint_method(endpoint: str) -> str | None:
    """The HTTP method of an endpoint written 'METHOD /path' (None for an operationId)."""
    m = re.fullmatch(r"([A-Za-z]+)\s+/\S*", endpoint.strip())
    return m.group(1).upper() if m else None


# The check_* functions return the (normalized) value or raise ValueError with a fix hint. The model validators and
# design.validate() share them, so a Plan built any way (assignment, model_copy) is held to the same rules.


def check_name(v: str) -> str:
    if not PLAN_NAME.fullmatch(v):
        raise ValueError(f"name {v!r} must match [a-z0-9][a-z0-9-]{{0,62}}, e.g. 'petstore-mcp'.")
    pkg = package_of(v)
    if not pkg.isidentifier() or keyword.iskeyword(pkg) or pkg in PACKAGE_RESERVED:
        raise ValueError(f"name {v!r} gives the Python package {pkg!r}, which is not usable; use e.g. 'petstore-mcp'.")
    return v


def check_prefix(v: str) -> str:
    if not PREFIX.fullmatch(v):
        raise ValueError(f"prefix {v!r} must match [a-z][a-z0-9_]*, e.g. 'pet'.")
    return v


def check_env(v: str, what: str = "env_prefix") -> str:
    if not ENV_NAME.fullmatch(v):
        raise ValueError(f"{what} {v!r} must match [A-Z][A-Z0-9_]*, e.g. 'PETSTORE_MCP'.")
    return v


def check_tool_name(v: str) -> str:
    if not TOOL_NAME.fullmatch(v):
        raise ValueError(f"name {v!r} must be snake_case matching [a-z][a-z0-9_]{{0,63}}, e.g. 'shop_search_products'.")
    if keyword.iskeyword(v) or v in TOOL_RESERVED:
        raise ValueError(f"tool name {v!r} is a Python keyword or reserved in generated code; use '<prefix>_...'.")
    return v


def check_arg_name(v: str) -> str:
    if not ARG_NAME.fullmatch(v) or keyword.iskeyword(v):
        raise ValueError(
            f"arg name {v!r} must be a Python identifier of letters, digits and '_' starting with a letter "
            "(no leading underscore) and not a keyword; use snake_case like 'max_price'."
        )
    if v in ARG_RESERVED:
        raise ValueError(f"arg name {v!r} is reserved in generated code; rename it (e.g. '{v}_name').")
    return v


def check_target(v: str) -> str:
    if not TARGET.fullmatch(v):
        raise ValueError(
            f"bad target {v!r}; use path.<n>, query.<n>, header.<n>, cookie.<n>, body, body.<a.b> or sql.<n>."
        )
    return v


def check_transform(v: str, names: frozenset[str] = transforms.NAMES) -> str:
    if v not in names:
        raise ValueError(f"unknown transform {v!r}; use one of {sorted(names)}.")
    return v


def check_timezone(v: str) -> str:
    """Empty (the system's local zone) or an IANA name such as 'Asia/Tehran'."""
    if v:
        try:
            ZoneInfo(v)
        except (KeyError, ValueError, OSError):  # unknown key, malformed key, unreadable tz data
            raise ValueError(f"timezone {v!r} is not an IANA zone; use e.g. 'Asia/Tehran' or leave it empty.") from None
    return v


def check_repository(v: str) -> str:
    """Empty, or a github.com/<owner>/<repo> URL; a trailing '/' or '.git' is dropped."""
    v = v.strip().rstrip("/").removesuffix(".git").rstrip("/")
    if v and not REPO.fullmatch(v):
        raise ValueError(f"repository {v!r} must be empty or look like 'https://github.com/<owner>/<repo>'.")
    return v


# ---------- Source: what the API offers ----------


class Param(BaseModel):
    name: str = Field(description="Wire name, exactly as the API expects it.")
    location: ParamIn
    type: ArgType = "string"
    required: bool = False
    description: str = ""
    enum: list[Any] | None = None
    default: Any = None
    example: Any = None


class AuthScheme(BaseModel):
    model_config = STRICT

    name: str = Field(description="Scheme name (OpenAPI securitySchemes key, or a label for captured traffic).")
    type: AuthType
    param: str = Field("", description="Header, query or cookie name that carries the credential.")
    env: str = Field(
        "", description="Environment variable the generated server reads the credential from; starts with env_prefix_."
    )
    login_path: str = Field(
        "", description="Browser auth: path prefix whose redirect means logged out, e.g. '/Account/Login'."
    )
    session_cookie: str = Field(
        "",
        description="Browser auth: name of the cookie that proves the user is logged in; absent means session ended.",
    )
    provider: str = Field(
        "", description="Sign-in auth: the provider that signs the user in and signs each request, e.g. 'xiaomi'."
    )

    @field_validator("env")
    @classmethod
    def _env(cls, v: str) -> str:
        return check_env(v, "auth.env") if v else v


class Endpoint(BaseModel):
    id: str = Field(description="Stable key: operationId, else 'METHOD /path/{template}'.")
    method: str
    path: str = Field(description="Path template with {name} placeholders, relative to base_url.")
    base_url: str = Field("", description="Scheme + host (+ base path). Empty means Source.base_url.")
    summary: str = ""
    description: str = ""
    tags: list[str] = []
    params: list[Param] = []
    body_schema: dict[str, Any] | None = None
    body_example: Any = None
    body_encoding: BodyEncoding = Field("json", description="How the request body is sent: JSON, form or multipart.")
    response_schema: dict[str, Any] | None = None
    response_example: Any = Field(None, description="A real (captured) or synthesized response body, trimmed.")
    auth: list[str] = Field([], description="AuthScheme names this endpoint needs.")
    count: int = Field(1, description="How often it was seen in captured traffic (1 for OpenAPI).")
    verified: bool = Field(True, description="False: read from the app's code, not yet seen answering (see probe).")


class Source(BaseModel):
    kind: str = Field(description="Importer that made it: 'openapi', 'har', 'app', or a plugin importer's name.")
    title: str
    description: str = ""
    base_url: str
    headers: dict[str, str] = Field({}, description="Static headers the site expects on every call (captured traffic).")
    auth: list[AuthScheme] = []
    endpoints: list[Endpoint] = []

    def endpoint(self, id: str) -> Endpoint | None:
        return next((e for e in self.endpoints if e.id == id), None)


# ---------- Plan: the MCP server to build ----------


class ToolArg(BaseModel):
    model_config = STRICT

    name: str = Field(description="Python identifier the model sees, e.g. 'query', 'max_price'.")
    type: ArgType = "string"
    description: str = Field(description="What it means, units and one example value.")
    required: bool = False
    default: Any = None
    enum: list[Any] | None = None
    map: dict[str, Any] | None = Field(
        default=None,
        description="Friendly value -> wire value, e.g. {'newest': 'sort_date_desc'}. The model sees the keys.",
    )
    minimum: float | None = None
    maximum: float | None = None
    to: str = Field(
        "",
        description="Request target: path.<n> | query.<n> | header.<n> | cookie.<n> | body | body.<a.b> | sql.<n>; "
        "empty when only steps use the arg ($arg.<name>).",
    )
    transform: str | None = Field(
        default=None, description="Input transform applied to the value before sending, e.g. 'jalali->unix'."
    )

    @field_validator("name")
    @classmethod
    def _name(cls, v: str) -> str:
        return check_arg_name(v)

    @field_validator("transform")
    @classmethod
    def _transform(cls, v: str | None) -> str | None:
        return v if v is None else check_transform(v, transforms.INPUT_NAMES)

    @field_validator("to")
    @classmethod
    def _to(cls, v: str) -> str:
        return check_target(v) if v else v


class Output(BaseModel):
    model_config = STRICT

    root: str = Field(default="", description="Dotted path to the useful part of the response, e.g. 'data.products'.")
    fields: dict[str, str] = Field(
        default={},
        description="Output name -> dotted source path, applied to each item when root is a list. Empty keeps all.",
    )
    max_items: int | None = Field(
        default=None, ge=0, description="Cap on list length; the result also reports the count."
    )
    clip: int = Field(default=400, ge=1, description="Longest string kept, in characters.")
    transforms: dict[str, str] = Field(
        default={}, description="Output name (a key of fields) -> output transform, e.g. {'day': 'unix->date'}."
    )

    @field_validator("transforms")
    @classmethod
    def _transforms(cls, v: dict[str, str]) -> dict[str, str]:
        for name in v.values():
            check_transform(name, transforms.OUTPUT_NAMES)
        return v


class Paginate(BaseModel):
    """Follow pages: the tool calls its endpoint up to max_pages times and joins the lists under output.root."""

    model_config = STRICT

    style: Literal["page", "offset", "cursor", "next_url"] = Field(
        "page", description="page: 1, 2, 3...; offset: 0, size, 2*size...; cursor/next_url: read from the response."
    )
    param: str = Field("", description="Target that carries the page, offset or cursor, e.g. 'query.page'.")
    next: str = Field("", description="cursor/next_url: dotted response path of the next cursor or URL.")
    start: int = Field(1, ge=0, description="First page number (page) or offset (offset).")
    size: int = Field(0, ge=0, description="offset: items per page.")
    max_pages: int = Field(3, ge=1, le=20, description="Most requests per tool call.")

    @field_validator("param")
    @classmethod
    def _param(cls, v: str) -> str:
        return check_target(v) if v else v


STEP_REF = re.compile(r"\$(arg|prev|item)(\.\S+)?")  # $arg.<name> | $prev.<path> | $item.<path>


class Step(BaseModel):
    """A further request after the tool's own: its values come from the args or from the previous step's result."""

    model_config = STRICT

    endpoint: str = Field(description="Endpoint.id this step calls.")
    args: dict[str, str] = Field(
        {}, description="Target -> '$arg.<name>', '$prev.<path>' or (with for_each) '$item.<path>'."
    )
    fixed: dict[str, Any] = Field({}, description="Constant request values, same target syntax as ToolArg.to.")
    for_each: str = Field("", description="Path of a list in the previous result: run once per item (max_items).")
    max_items: int = Field(5, ge=1, le=20, description="Most items for_each runs on.")

    @field_validator("args")
    @classmethod
    def _args(cls, v: dict[str, str]) -> dict[str, str]:
        for target, ref in v.items():
            check_target(target)
            if not STEP_REF.fullmatch(ref):
                raise ValueError(f"step value {ref!r} must be '$arg.<name>', '$prev.<path>' or '$item.<path>'.")
        return v

    @field_validator("fixed")
    @classmethod
    def _fixed(cls, v: dict[str, Any]) -> dict[str, Any]:
        for key in v:
            check_target(key)
        return v


class Tool(BaseModel):
    model_config = STRICT

    name: str = Field(description="snake_case, starts with the plan prefix, e.g. 'pet_find_by_status'.")
    title: str = Field(description="Short human title, e.g. 'Find pets by status'.")
    description: str = Field(
        description="First line: one complete sentence on what it returns. Then when to use it and what to call next."
    )
    endpoint: str = Field(description="Endpoint.id this tool calls.")
    args: list[ToolArg] = []
    fixed: dict[str, Any] = Field({}, description="Constant request values, same target syntax as ToolArg.to.")
    output: Output = Output()
    body_encoding: BodyEncoding = Field("json", description="How body.* values are sent: json, form or multipart.")
    paginate: Paginate | None = Field(None, description="Follow pages of the endpoint's list (see Paginate).")
    sql: str | None = Field(
        None, description="Database plans: one statement with :name placeholders, filled by args with to='sql.<name>'."
    )
    steps: list[Step] = Field(
        [], description="Requests after the tool's own, each fed by the one before; output shapes the last result."
    )
    read_only: bool = True
    example: dict[str, Any] = Field({}, description="Arguments for tests and `toolsmelt check`.")

    @field_validator("name")
    @classmethod
    def _name(cls, v: str) -> str:
        return check_tool_name(v)

    @field_validator("fixed")
    @classmethod
    def _fixed(cls, v: dict[str, Any]) -> dict[str, Any]:
        for key in v:
            check_target(key)
        return v


class Plan(BaseModel):
    model_config = STRICT

    schema_version: int = Field(SCHEMA_VERSION, description="plan.yaml format version.")
    name: str = Field(description="Distribution / command name, [a-z0-9][a-z0-9-]{0,62}, e.g. 'petstore-mcp'.")
    title: str
    description: str = Field(description="One line, at most 100 characters (MCP Registry limit).")
    instructions: str = Field(description="Server instructions: workflow across tools, units, conventions.")
    prefix: str = Field(description="Tool name prefix, [a-z][a-z0-9_]*, e.g. 'pet'.")
    version: str = "0.1.0"
    base_url: str
    env_prefix: str = Field(description="Upper-case env var prefix [A-Z][A-Z0-9_]*, e.g. 'PETSTORE_MCP'.")
    headers: dict[str, str] = {}
    auth: AuthScheme | None = None
    impersonate: bool = Field(False, description="Use a Chrome TLS fingerprint (sites that block plain clients).")
    concurrency: int = Field(2, ge=1)
    cache_ttl: int = Field(60, ge=0, description="Seconds to cache GET responses.")
    timezone: str = Field(
        "", description="IANA zone for date transforms, e.g. 'Asia/Tehran'; empty means system local."
    )
    author: str = ""
    repository: str = Field("", description="https://github.com/<owner>/<repo> if known.")
    tools: list[Tool] = []

    @field_validator("schema_version")
    @classmethod
    def _schema_version(cls, v: int) -> int:
        if not 1 <= v <= SCHEMA_VERSION:
            raise ValueError(
                f"schema_version {v} is not supported (this toolsmelt reads 1..{SCHEMA_VERSION}); upgrade."
            )
        return v

    @field_validator("name")
    @classmethod
    def _name(cls, v: str) -> str:
        return check_name(v)

    @field_validator("prefix")
    @classmethod
    def _prefix(cls, v: str) -> str:
        return check_prefix(v)

    @field_validator("env_prefix")
    @classmethod
    def _env_prefix(cls, v: str) -> str:
        return check_env(v)

    @field_validator("repository")
    @classmethod
    def _repository(cls, v: str) -> str:
        return check_repository(v)

    @field_validator("timezone")
    @classmethod
    def _timezone(cls, v: str) -> str:
        return check_timezone(v)

    @property
    def package(self) -> str:
        """Python import name: 'petstore-mcp' -> 'petstore_mcp'."""
        return package_of(self.name)


# ---------- files ----------


def load_source(path: str | Path) -> Source:
    return Source.model_validate_json(Path(path).read_text(encoding="utf-8"))


def save_source(source: Source, path: str | Path) -> None:
    Path(path).write_text(source.model_dump_json(indent=1, exclude_defaults=True), encoding="utf-8", newline="\n")


def project_plan(project: str | Path) -> Path:
    """A generated project's plan file: toolsmelt.yaml, or mcpsmith.yaml in projects generated before the rename."""
    new, old = Path(project) / "toolsmelt.yaml", Path(project) / "mcpsmith.yaml"
    return old if old.is_file() and not new.is_file() else new


def load_plan(path: str | Path) -> Plan:
    text = Path(path).read_text(encoding="utf-8")
    try:
        data = json.loads(text) if str(path).endswith(".json") else yaml.safe_load(text)
        data = json.loads(json.dumps(data, default=str))  # YAML dates become ISO strings
        return Plan.model_validate(data)
    except (yaml.YAMLError, ValueError, TypeError) as e:  # pydantic's ValidationError is a ValueError
        raise ValueError(f"{path}: {e}") from e


def save_plan(plan: Plan, path: str | Path) -> None:
    data = {"schema_version": plan.schema_version, **plan.model_dump(mode="json", exclude_defaults=True)}
    for tool, out in zip(plan.tools, data.get("tools", []), strict=True):
        out["read_only"] = tool.read_only  # always explicit: validate treats an unset read_only on a POST as an error
    text = yaml.safe_dump(data, allow_unicode=True, sort_keys=False, width=120)
    Path(path).write_text(text, encoding="utf-8", newline="\n")
