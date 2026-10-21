---
name: build-mcp-server
description: Build a curated, tested MCP server (agent tools) with mcpsmith. Use when the user wants an MCP server or agent tools from an API, an OpenAPI/Swagger file, a backend, or a website that has no public API (via a recorded browsing session / HAR file).
---

# Build an MCP server with mcpsmith

Not set up yet? `uvx mcpsmith setup` registers mcpsmith, Chrome DevTools MCP and REA with the user's agents.

mcpsmith turns an OpenAPI file or a recorded website session (HAR) into a Python MCP server project with tests, CI, a publish workflow and a Claude Code plugin. You design the tools; mcpsmith generates and checks the code.

Run commands with `uvx mcpsmith <cmd>` (or `mcpsmith <cmd>` if installed). No shell? Use the mcpsmith MCP tools instead: `smith_guide`, `smith_inspect`, `smith_draft`, `smith_validate`, `smith_generate`, `smith_check`, `smith_install`, and the hub tools `smith_record_start`, `smith_record_stop`, `smith_connect`, `smith_disconnect`, `smith_connections`, `smith_login`, `smith_tools`, `smith_call` (paths absolute).

The end result users want is a normal MCP server their agent runs: `generate` the project, test it, then `mcpsmith install <project>` (`smith_install`) registers it with their agents (section 6). The hub (`smith_connect`, live tools with no project; for a logged-in site see the `extract-api` skill) is for trying tools fast before that, not the finish. If the mcpsmith tools are missing: `claude mcp add mcpsmith -- uvx mcpsmith mcp`.

App that signs its own requests (no readable HTTP interface, `app-android` says so): use `auth: {type: signin, provider: ...}` when mcpsmith has a provider for it. The user signs in through a link from the login tool; never ask for passwords or tokens in chat.

## 0. Ask first

Ask the user, then wait:
- Goal: which tasks should an agent be able to do with this API or site? (3-8 workflows, not a list of endpoints)
- Input: an OpenAPI/Swagger file or URL, or a website with no public API?
- Auth: does it need a token or login? Which account is safe to test with?

Done means: `uv run pytest` passes in the exported project, `mcpsmith check` shows no errors and sane output sizes, and the server is installed in the user's agents (or the user said they only want to try it in the hub).

## 1. Get the source

OpenAPI/Swagger: have a path or URL (`.json`/`.yaml`).

Website without an API: record the session as HAR. Each feature the user wants as a tool must appear in the recording, so tell them to:
- search for something, then open a result's detail page
- paginate (next page / load more) and change sort order
- apply each filter they care about once
- do any other action they want (reviews, availability, price history)
- skip logins and payments unless the tool needs them

```
mcpsmith capture https://example.com -o site.har --host example.com --host api.example.com
```

It opens a browser; the user browses, then closes the tab (closing the whole window can lose the recording). `--host` (repeatable) keeps only those hosts; use it to drop analytics and ads. Needs `pip install "mcpsmith[capture]" && playwright install chromium`. Alternative: DevTools > Network > "Save all as HAR with content", then pass that file.
The HAR holds the user's cookies and personal data: never commit or upload it, and delete it after `inspect`. Prefer recording logged out unless a tool needs the account. Before publishing, read the generated `tests/fixtures/*.json` for anything personal the scrubber missed.

## 2. Inspect, then read

```
mcpsmith inspect <openapi-or-har path|url> [-w .mcpsmith] [--host H]... [--budget N]
mcpsmith guide
```

`inspect` writes `.mcpsmith/source.json` and `.mcpsmith/digest.md` (endpoints, parameters, trimmed real responses). Read `digest.md` fully and the output of `guide` (the tool-design rules; they win over this file).

## 3. Draft, then redesign it yourself

```
mcpsmith draft [-w .mcpsmith] [--name site-mcp] [--force]
```

`draft` is heuristic (roughly one tool per endpoint, no LLM). Treat it as a starting point and edit `.mcpsmith/plan.yaml`:
- Merge or drop endpoints until each tool is one task the user named. Keep few tools (aim under 10).
- Name tools `<prefix>_<verb_noun>`; write argument names the way a person would say them (`query`, `max_price`), `to:` keeps the wire target (`query.q`, `path.id`, `body.filter.min`).
- Description first line: one complete sentence saying what it returns (max 100 chars for the whole plan `description`). Then when to use it and which tool to call next.
- Every arg: description with units and one example value; `enum`/`minimum`/`maximum` where known.
- `output`: set `root` and `fields` (output name -> dotted source path) so only useful fields come back; keep ids needed by the next tool; set `max_items`.
- Fill `example` for every tool (used by tests and `check`). Mark `read_only: false` only for writes.
- Write `instructions`: the workflow across tools, units, currency, language, conventions.
- Set `env_prefix`, `auth`, `impersonate: true` for sites that block plain clients (needs `mcpsmith[stealth]`). Logged-in site in the hub: `auth: {type: browser, login_path: /login}`.
- Dates, unix times, minutes, currency: `transform` on args and `output.transforms` (names in `mcpsmith guide`), and `timezone` (IANA name; empty = local zone).

For field-level rules and the review checklist use the `design-mcp-tools` skill. If the user has an API key and wants no manual step: `mcpsmith design --goal "..." --provider anthropic|openai|openrouter|ollama` writes the plan instead (keys: `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `OPENROUTER_API_KEY`; ollama needs none).

## 4. Validate, connect, generate, test

```
mcpsmith validate [-w .mcpsmith]            # exit 1 on errors; fix warnings too
```

Hub: `smith_connect(workdir, name)` adds the tools now and saves the connection (`mcpsmith connections` lists them; `smith_disconnect` removes one). Personal credentials: the developer runs `mcpsmith secret set <connection> <ENV_NAME>` (hidden prompt, OS keychain); never ask for them in chat. Export:

```
mcpsmith generate [-w .mcpsmith] -o ../site-mcp [--force]
cd ../site-mcp && uv run pytest
```

Offline tests replay captured responses. A plan error or failing test means fix `plan.yaml` and regenerate. `generate` refuses `auth: {type: browser}` plans (they only work in the hub): switch the plan to bearer/header/cookie auth first. Secrets are never exported.

## 5. Check live

```
mcpsmith check ../site-mcp [--json] [--writes]
```

Calls every tool with its `example` args and reports errors, output size (chars) and fields that came back empty. Tools marked `read_only: false` are skipped (`SKIP`) because they change data on the real service. Pass `--writes` (MCP: `writes=true`) only after listing those tools to the user and getting a yes.
On HTTP 403 from a site that works in the browser, the egress IP may be blocked: set `<ENV_PREFIX>_PROXY` (generated servers ignore system proxy settings).

Then iterate:
- Errors (401/403/404, wrong example): fix `example`, `auth`, `impersonate`, or the endpoint mapping in `plan.yaml`.
- Empty fields: wrong `fields` path; look at `tests/fixtures/<tool>.json` for the real shape.
- Output too big: trim `fields`, lower `max_items`, `clip`.
- Re-run `generate --force` after plan edits.

Need one tool to call several endpoints? Hand-edit `src/<package>/server.py`. Regenerating overwrites those edits, so do it last and tell the user.

## 6. Install and publish

Local, with every agent found (Claude Code, Codex, Cursor, Gemini CLI); ask the user first, it edits their agent configs:

```
mcpsmith install /abs/path/site-mcp        # --dry-run first; --pypi once it is released
```

The user restarts their agent. Needed env vars of the generated server: `<P>_PROXY` (proxy URL or comma list), `<P>_BASE_URL`, and the auth var named in the plan (signin auth needs none: its `<prefix>_login` tool signs the user in).

Publish (ask the user first; it is public):
1. Create the GitHub repo, push the project.
2. On PyPI add a pending trusted publisher: owner, repo, workflow `publish.yml`, environment `pypi`. Check the package name is free.
3. Bump `version` in `pyproject.toml` and `server.json` together, then `git tag v0.1.0 && git push --tags`. The workflow publishes to PyPI and the MCP Registry.

Nightly drift check: run `mcpsmith check <project>` on a schedule; new errors or empty fields mean the site changed.
