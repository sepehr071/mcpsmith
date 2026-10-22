# toolsmelt design

## Why

Every tool that turns an API into an MCP server today either mirrors one endpoint per tool and passes raw
JSON through (FastMCP `from_openapi`, openapi-mcp-generator, Speakeasy), stops at an OpenAPI spec or API
client (mitmproxy2swagger, Integuru, reverse-api-engineer), or is a closed hosted service (Clarity,
Unbrowse, Supergood). FastMCP's own docs say curated servers beat auto-converted ones.

toolsmelt makes the curated server the default output:

1. **Two inputs, one pipeline.** An OpenAPI/Swagger file, or a website session recorded as HAR.
2. **An LLM designs the tools.** It reads a compact digest of the API (endpoints, parameters, trimmed real
   responses) and writes a `Plan`: few workflow-level tools, plain argument names, descriptions that say
   when to use each tool and what to call next, and output shaping that keeps only the useful fields.
   The LLM is either the developer's coding agent (skills + CLI + the toolsmelt MCP server; no API key)
   or a direct API call (`toolsmelt design --provider ...`).
3. **The output is ordinary code** the developer owns: a Python MCP server project with tests built from
   the captured responses, CI, a publish workflow (PyPI + MCP Registry), `server.json`, and a Claude Code
   plugin (MCP server + skill).
4. **A shared runtime** (`toolsmelt.runtime`) carries what hand-written servers copy into every repo:
   HTTP client with retries, Retry-After, jitter, concurrency cap, GET cache, host allowlist, body cap,
   proxy pool, optional Chrome TLS impersonation, actionable error messages, compact JSON output.
   A fix there reaches every generated server on upgrade.
5. **Drift check.** `toolsmelt check <project>` calls every tool live with its example arguments and reports
   errors, output size and output fields that came back empty: run it nightly to see when a site changes.

Non-goals: running a browser per call, solving JS challenges/captchas, non-Python output, a hosted service.
Multi-call tools are in scope since 0.6: `paginate` follows pages and `steps` chains requests (see the guide).

One request path (0.6): a generated tool keeps its typed signature but its body only calls
`toolsmelt.runtime.interpret.run_tool(api, spec, values, tz)` with a spec literal written by codegen, the same
function the hub runs. Argument mapping, pagination, steps and SQL execution live in the runtime once, so the hub
and exported servers cannot drift apart. Generated projects therefore depend on `toolsmelt>=0.7,<1` (0.7 is the first release under this name).

## Live hub

The toolsmelt MCP server doubles as a hub: `smith_connect` validates a plan, copies it with `source.json` to
`~/.toolsmelt/connections/<name>` (`TOOLSMELT_HOME`), and registers its tools on the running server through the
plan interpreter (`runtime/interpret.py`), which behaves like generated code. Saved connections reload on
start. Connections are personal; `generate` is the publish path and refuses `auth: browser` plans.

- Recording: `toolsmelt browser` starts a shared Chrome (own profile, remote debugging on localhost);
  `smith_record_start/stop` capture API traffic from it into a HAR with cookie/token headers dropped and bodies
  scrubbed. Agents drive pages with Chrome DevTools MCP pointed at the same Chrome, and must not call its
  `get_network_request` (it prints cookies).
- Browser auth: the runtime reads that Chrome's session cookies for the base host only; a 401 or a redirect to
  `auth.login_path` becomes a "log in again" error.
- Secrets: `toolsmelt secret set` stores values in the OS keychain (hidden prompt); they never pass through the
  agent or plan files.
- Transforms (`runtime/transforms.py`): input `ToolArg.transform` and output `Output.transforms`, using plan
  `timezone` (IANA name, empty = system local zone). Generic first (ISO dates, unix time, minutes, currency
  scale), Jalali as one optional calendar.

## Pipeline and files

```
openapi.yaml | site.har ──inspect──▶ .toolsmelt/source.json + digest.md
                                   ──draft───▶ .toolsmelt/plan.yaml        (heuristic starting point)
       LLM (agent edits plan.yaml, or `design`) ▶ .toolsmelt/plan.yaml
                                   ──validate─▶ issues
                                   ──generate─▶ <name>/  (server project, plan copied to <name>/toolsmelt.yaml)
                                   ──check────▶ live report per tool
```

`toolsmelt capture <url> -o site.har` opens a browser (Playwright, optional extra) and saves the session.
A HAR exported from browser DevTools works the same.
`toolsmelt app-desktop` is the same path for a desktop program: it prints the steps for the developer's own
mitmproxy (`mitmdump --set hardump=<file>`), then imports the HAR through `inspect_source`. It never installs,
starts or configures a proxy or certificate; certificate-pinned programs are out of scope.

## Data model

`src/toolsmelt/model.py` is the contract: `Source`, `Endpoint`, `Param`, `AuthScheme`, `Plan`, `Tool`,
`ToolArg`, `Output`, plus `load_source/save_source/load_plan/save_plan`. Read it; do not change field
names without the orchestrator.

Target syntax (`ToolArg.to`, keys of `Tool.fixed`): `path.<name>`, `query.<name>`, `header.<name>`,
`cookie.<name>`, `body` (the whole JSON body is the value), `body.<a.b.c>` (nested key in the JSON body).

Dotted paths (`Output.root`, `Output.fields` values): keys separated by `.`, a number indexes a list
(`images.0.url`), `[]` after a key maps over a list (`tags[].name`). A missing step gives `None`.

## Module contracts (function signatures)

```python
# importers  (src/toolsmelt/importers/)
toolsmelt.importers.load(path_or_url: str) -> Source        # detects OpenAPI 3.x / Swagger 2.0 (JSON or YAML) vs HAR
toolsmelt.importers.openapi.parse(doc: dict, origin: str | None = None) -> Source
toolsmelt.importers.har.parse(har: dict, hosts: list[str] | None = None) -> Source
toolsmelt.capture.record(url: str, out: Path, hosts: list[str] | None = None) -> Path   # blocking, headed browser

# design  (src/toolsmelt/design/)
toolsmelt.design.digest(source: Source, budget: int = 60_000) -> str      # markdown for an LLM, <= budget chars
toolsmelt.design.draft(source: Source, name: str | None = None) -> Plan  # deterministic, no LLM
toolsmelt.design.validate(plan: Plan, source: Source | None = None) -> list[Issue]
toolsmelt.design.Issue  # dataclass(level: "error" | "warning", where: str, message: str)
toolsmelt.design.guide() -> str                                           # tool-design rules, markdown
toolsmelt.design.llm.design(source: Source, goal: str = "", provider: str = "anthropic",
                           model: str | None = None, start: Plan | None = None) -> Plan

# runtime  (src/toolsmelt/runtime/)  — imported by generated servers, keep it small and stable
from toolsmelt.runtime import Api, ApiError, Registry, build_server, shape, run

# codegen + check
toolsmelt.codegen.generate(plan: Plan, out: Path, source: Source | None = None, force: bool = False) -> list[Path]
toolsmelt.check.run(project: Path) -> list[CheckResult]
toolsmelt.check.CheckResult  # dataclass(tool, ok, error: str | None, chars: int, empty_fields: list[str])
```

## Runtime API (what generated code calls)

```python
api = Api(
    base_url="https://petstore3.swagger.io/api/v3",
    env_prefix="PETSTORE_MCP",     # <P>_BASE_URL overrides base_url, <P>_PROXY = proxy URL or comma list
    hosts=["petstore3.swagger.io"],  # allowlist; defaults to the base_url host
    headers={"Accept": "application/json"},
    auth={"type": "bearer", "env": "PETSTORE_MCP_TOKEN"},   # or header/query/cookie with "param", basic ("user:pass")
    impersonate=False, concurrency=2, cache_ttl=60,
)
data = await api.call("GET", "/pet/{petId}", path={"petId": 1}, query={...}, body={"a.b": 1}, headers={...})
api.set_transport(httpx.MockTransport(handler))   # tests; resets client and cache

tool = Registry()
@tool("Find pets by status")            # literal string title: catalogs parse it
async def pet_find_by_status(...): ...

mcp = build_server("petstore-mcp", title="Petstore", instructions=INSTRUCTIONS, version=__version__,
                   registry=tool, website_url=None)
def main(): run(mcp)

shape(data, root="data.items", fields={"id": "id", "price": "price.value"}, max_items=20, clip=400)
# list after root -> {"items": [...], "count": <len before cap>}; dict -> dict. Nulls/empties dropped,
# strings clipped. fields={} keeps everything.
```

`Api.call` rules: the auth credential goes only to the base URL host (other allowlisted hosts get none);
path values are percent-encoded and `.`/`..` cannot climb out of the base path; cookie/header values
with `;` or line breaks are refused; `None` values are dropped from path/query/body/headers; `body` keys with dots nest;
absolute `path` (`https://...`) is allowed when its host is in `hosts`; GET responses cached `cache_ttl`
seconds; retries on network error/timeout/429/5xx (Retry-After honoured, capped); 4xx/5xx become
`ApiError` with a message the model can act on (403: blocked → proxy hint with `<P>_PROXY`; 401: set the
auth env var; 404; 429; 5xx); missing auth env var → `ApiError` naming the variable; JSON body parsed,
non-JSON returns `{"text": <clipped text>}`; responses over 5 MB refused; system proxy env ignored
(`trust_env=False`); `impersonate=True` uses curl_cffi Chrome when `toolsmelt[stealth]` is installed, else
plain httpx with one warning.

## Generated project

```
<name>/
  pyproject.toml  server.json  README.md  LICENSE  .gitignore  toolsmelt.yaml
  src/<package>/__init__.py  src/<package>/server.py
  tests/conftest.py  tests/test_tools.py  tests/fixtures/<tool>.json
  .github/workflows/ci.yml  .github/workflows/publish.yml
  .claude-plugin/plugin.json  skills/<name-without-mcp>/SKILL.md
```

Offline tests replay each tool's fixture (the endpoint's `response_example`) through `httpx.MockTransport`
and call the tool through the MCP client with `Tool.example`. One `-m live` test per tool hits the real API.

## Plan validation rules

Errors: tool name not matching `^[a-z][a-z0-9_]{0,63}$` or not starting with `prefix`; duplicate tool or
arg names; arg name not a Python identifier or a keyword; bad target syntax; unknown endpoint (when a
source is given); a `{param}` in the endpoint path with no arg or fixed value; required arg with a default;
plan `description` (not tool descriptions) > 100 chars; `env_prefix` not `^[A-Z][A-Z0-9_]*$`.
Warnings: description first line not ending with `.`; more than 20 tools; tool without example while it has
required args; output field path that resolves to nothing in the endpoint's response example; output
without `fields` on an endpoint whose example response is over 4 KB; non-GET tool marked read_only.
