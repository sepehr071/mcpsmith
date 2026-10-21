# mcpsmith

Python framework that turns an OpenAPI/Swagger file, a recorded website session (HAR), or an app you own (Android, Electron/JS, desktop) into curated tools: live through the hub, or a tested MCP server project. An LLM designs the tools: you (the coding agent) edit `plan.yaml`, or `mcpsmith design --provider ...` calls an LLM API. Generated projects use `mcpsmith.runtime` and include tests, CI, a PyPI/MCP Registry publish workflow, `server.json` and a Claude Code plugin.

## Workflow

Install for your agents with `uvx mcpsmith setup` (`--dry-run` first; `--agents`, `--no-rea`, `--no-skills`, `--yes`, `--remove`).

Ask the user which tasks the server should support, then:

1. Pick the source (apps and programs: only software the user owns or is authorized to analyze):
   - Website only: `mcpsmith capture <url> -o site.har --host <host>` (user browses, closes the browser), or use a DevTools HAR export. For a logged-in site use the hub (below).
   - Android: `mcpsmith app-android <apk> --base-url <url>` lists candidate Retrofit classes; add `--class NAME` to write `source.json` (or `--source FILE` for an already decompiled class). Needs REA's JADX jar (`REA_JADX_MCP_JAR`, set by `npx rea-agents setup`).
   - Electron/JS: `mcpsmith app-js <dir|app.asar|file> --base-url <url>`.
   - Desktop program: `mcpsmith app-desktop` prints the mitmproxy steps for the user to run; `mcpsmith app-desktop <capture.har> --host <host>` imports the HAR.
   - App endpoints are unverified: `mcpsmith probe` calls the GET ones live (`--writes` for the rest, only after the user agrees, `--limit N`). An endpoint with no method cannot be used until the plan sets it.
2. `mcpsmith inspect <openapi-or-har path|url>` writes `.mcpsmith/source.json` and `.mcpsmith/digest.md` (the app commands above write them already).
3. Read `.mcpsmith/digest.md` and the output of `mcpsmith guide` (tool-design rules).
4. `mcpsmith draft` writes a heuristic `.mcpsmith/plan.yaml`.
5. Redesign `plan.yaml` yourself: merge or drop endpoints, plain arg names, one-sentence first lines, compact `output.fields`, `example` per tool, `instructions`.
6. `mcpsmith validate` (exit 1 on errors).
7. `mcpsmith generate -o <dir>`, then `cd <dir> && uv run pytest`.
8. `mcpsmith check <dir>` calls every tool live: errors, output size, empty fields. Confirm with the user before it runs non-GET tools.
9. Edit the plan and `generate --force` to iterate. Hand edits to `server.py` are overwritten by regenerating.
10. Publish: PyPI trusted publisher (workflow `publish.yml`, environment `pypi`), bump `version` in `pyproject.toml` and `server.json`, tag `v*`.

Without a shell, use the MCP tools: `smith_guide`, `smith_inspect`, `smith_app_android`, `smith_app_js`, `smith_app_desktop`, `smith_probe`, `smith_draft`, `smith_validate`, `smith_generate`, `smith_check` (paths absolute).

Live hub (no project needed; connections are personal, stored under `~/.mcpsmith`, override with `MCPSMITH_HOME`):

- `mcpsmith browser [--port N] [--no-proxy]` starts the shared Chrome the developer logs in with; point Chrome DevTools MCP at it: `npx chrome-devtools-mcp@1.11.0 --browser-url=http://127.0.0.1:9222`. Never use DevTools `get_network_request` (it prints cookies).
- `smith_record_start(name, hosts)` / `smith_record_stop(name)` record API traffic from that Chrome into a HAR; then `smith_inspect`, edit the plan, `smith_validate`.
- `smith_connect(workdir, name)` makes the plan's tools live; `smith_connections`, `smith_tools(connection)`, `smith_call(connection, tool, args)` (read-only tools), `smith_disconnect(name)`. CLI: `mcpsmith connections`.
- Credentials come from the developer: `mcpsmith secret set|list|delete <connection> [NAME] [--once]`; never in chat or plans.
- `generate` is the publish path; it refuses `auth: browser` plans (switch to token auth).

## Skills

- `skills/build-mcp-server/SKILL.md`: the full workflow above.
- `skills/extract-api/SKILL.md`: record a logged-in site in the shared Chrome and connect it as live tools.
- `skills/design-mcp-tools/SKILL.md`: checklist for designing or reviewing tools, and how to measure output size.

Codex, Cursor, Gemini CLI and others: read these files directly; they are plain Markdown.

## MCP server config

```json
{"mcpServers": {"mcpsmith": {"command": "uvx", "args": ["mcpsmith", "mcp"]}}}
```

Claude Code: `claude mcp add mcpsmith -- uvx mcpsmith mcp`, or `/plugin marketplace add sepehr071/mcpsmith` then `/plugin install mcpsmith@mcpsmith`.

## Contributing

```
uv sync
uv run pytest            # live tests are skipped; run them with -m live
uv run ruff check .
uv run ruff format .
```

See `CONTRIBUTING.md`.

- Layout: `src/mcpsmith/` (`model.py` is the data contract: `Source`, `Plan`; `importers/` with `openapi.py`, `har.py`, `android.py`, `jadx.py`, `electron.py`, `design/`, `codegen.py`, `runtime/` with `transforms.py` and `interpret.py`, `hub.py`, `server.py`, `browser.py`, `record.py`, `probe.py`, `setup/`, `secrets.py`, `check.py`, `cli.py`); design notes in `docs/design.md`.
- Do not rename `model.py` fields without checking every stage that reads them.
- `mcpsmith.runtime` is imported by every generated server: keep it small and backward compatible.
- Ruff: line length 120, Python 3.10+.
