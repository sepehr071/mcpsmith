# mcpsmith

Python framework that turns an OpenAPI/Swagger file or a recorded website session (HAR) into a curated, tested MCP server project. An LLM designs the tools: you (the coding agent) edit `plan.yaml`, or `mcpsmith design --provider ...` calls an LLM API. Generated projects use `mcpsmith.runtime` and include tests, CI, a PyPI/MCP Registry publish workflow, `server.json` and a Claude Code plugin.

## Workflow

Ask the user which tasks the server should support, then:

1. Website only: `mcpsmith capture <url> -o site.har --host <host>` (user browses, closes the browser), or use a DevTools HAR export.
2. `mcpsmith inspect <openapi-or-har path|url>` writes `.mcpsmith/source.json` and `.mcpsmith/digest.md`.
3. Read `.mcpsmith/digest.md` and the output of `mcpsmith guide` (tool-design rules).
4. `mcpsmith draft` writes a heuristic `.mcpsmith/plan.yaml`.
5. Redesign `plan.yaml` yourself: merge or drop endpoints, plain arg names, one-sentence first lines, compact `output.fields`, `example` per tool, `instructions`.
6. `mcpsmith validate` (exit 1 on errors).
7. `mcpsmith generate -o <dir>`, then `cd <dir> && uv run pytest`.
8. `mcpsmith check <dir>` calls every tool live: errors, output size, empty fields. Confirm with the user before it runs non-GET tools.
9. Edit the plan and `generate --force` to iterate. Hand edits to `server.py` are overwritten by regenerating.
10. Publish: PyPI trusted publisher (workflow `publish.yml`, environment `pypi`), bump `version` in `pyproject.toml` and `server.json`, tag `v*`.

Without a shell, use the MCP tools below (`smith_guide`, `smith_inspect`, `smith_draft`, `smith_validate`, `smith_generate`, `smith_check`; paths absolute).

## Skills

- `skills/build-mcp-server/SKILL.md`: the full workflow above.
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

- Layout: `src/mcpsmith/` (`model.py` is the data contract: `Source`, `Plan`; `importers/`, `design/`, `codegen/`, `runtime/`, `check.py`, `cli.py`); design notes in `docs/design.md`.
- Do not rename `model.py` fields without checking every stage that reads them.
- `mcpsmith.runtime` is imported by every generated server: keep it small and backward compatible.
- Ruff: line length 120, Python 3.10+.
