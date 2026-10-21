# App Path, Task 3: Electron / JavaScript app extractor (plan 3, part 3)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans.

**Goal:** Read the HTTP calls an Electron (or any bundled JavaScript) app makes out of the app's own shipped code, as unverified endpoints, so a developer can give their agent tools for their own account in their own or an authorized app.

**Architecture:** The app ships its JavaScript, and usually its original sources inside `.map` files (`sourcesContent`). mcpsmith reads those files, prefers the original sources when present, and extracts call sites of the app's own request helper (`request(method, path, …)`, `fetch(url, {method})`, `axios.get(url)`) into `Endpoint`s with `verified: false`. Task 1's `probe` verifies the read-only ones. Pure Python, no Node, no new dependency.

**Tech Stack:** stdlib `json`, `re`, `struct` (asar index), `pathlib`. No parser dependency: extraction is regex over readable source; accuracy comes from Task 1's probe plus the agent's own review of the digest.

**Spec:** `docs/superpowers/specs/2026-10-20-mcp-everything-design.md` (G9 Electron bullet; "Spike results": REA's own findings 0/45 on GitHub Desktop, a small AST pass 42/45 with every method correct, and the app's `.map` files carrying the original TypeScript). User scope (2026-10-21): the developer's own or authorized app; nothing that defeats a protection.

## Global Constraints

- Input is a path the caller gives: an app directory, an `app.asar`, or a single `.js`/`.ts`/`.map` file. mcpsmith never downloads an app and never unpacks an installer; an `.asar` is read (header + JSON index + offsets), not executed.
- Treat every input file as untrusted data: read it, never import or run it, never resolve `require`/`import` targets, and keep output under the caller's workdir.
- Prefer original sources: for every `.map` with `sourcesContent`, extract from those texts and record the original file name (e.g. `app/src/lib/api.ts`) in `Endpoint.summary`; fall back to the bundle text only for files with no map.
- Minified identifiers stay as they are: an extracted param keeps its code name (`e`, `t`) and the digest marks the endpoint `UNVERIFIED`; the plan's author (the agent) renames them. Never invent a friendly name.
- An endpoint is emitted only when its path is a literal or a template whose non-expression part is a literal (`repos/${owner}/${name}` → `repos/{owner}/{name}`); a path built by an unresolvable expression is skipped with a note. No `Endpoint.path` may contain `$`, `{` without a matching `}`, `+`, backticks or quotes.
- Method comes from the call site only (the helper's first argument, `{method: …}`, or `axios.<verb>`); when it cannot be read, the endpoint keeps `method: ""` and a note — never guessed.
- Any string literal that becomes a header value, query value or base URL goes through `importers.har._scrub`; userinfo is stripped from any absolute URL.
- Every produced `Endpoint` has `verified: false`, `count: 0`, `response_example: None`; `Source.kind` is `"app"`.
- Caps so a 200 MB app cannot hang the tool: at most 400 files, 8 MB per file read for extraction, 2000 endpoints; whatever is dropped is counted in a note.
- Existing behaviour stays; full suite green; ruff clean; commits on `feat/app-path` without AI attribution.

## Review Focus

1. A `.map` whose `sourcesContent` is null or missing → fall back to the bundle, no crash. (Tests.)
2. A path from a template literal with an expression in the middle → `{param}` placeholders, never `${…}` in the path. (Tests.)
3. A hardcoded token in a header literal → scrubbed; an absolute URL with `user:pass@` → userinfo stripped. (Tests.)
4. A malformed `.asar` (truncated, bad index) → clear error naming the file, nothing written. (Tests.)
5. Two call sites with the same method and path in different files → two endpoints with distinct ids. (Tests.)

---

### Task 3: `importers/electron.py` + `smith_app_js`

**Files:**
- Create: `src/mcpsmith/importers/electron.py`, `tests/test_electron.py`, `tests/fixtures/electron/` (hand-written: `renderer.js` with a request-helper class, a `${}` template path, an unresolvable path, a header token, an absolute URL with userinfo; `renderer.js.map` with `sourcesContent` for one of them; a tiny valid `app.asar` built in the test, not committed as a binary)
- Modify: `src/mcpsmith/importers/__init__.py` (exported as its own function, not auto-detected), `src/mcpsmith/cli.py`, `src/mcpsmith/server.py`

**Interfaces:**
- Produces:
  - `electron.read_app(path: Path) -> dict[str, str]` — `{logical file name: text}` for `.js`/`.mjs`/`.cjs`/`.ts` plus the `sourcesContent` of their `.map` files, from a directory, an `.asar` or one file; raises `ValueError` naming the file on a malformed `.asar`.
  - `electron.parse_sources(files: dict[str, str], base_url: str = "", title: str = "") -> Source`.
  - CLI `mcpsmith app-js <path> [--base-url URL] [-w DIR]` → writes `source.json` + `digest.md`, prints `N endpoints, all unverified` and the `mcpsmith probe` next step.
  - MCP tool `smith_app_js(path, base_url="", workdir=...)` → `{"workdir", "endpoints", "unverified", "digest"}`; `read_only=False`; docstring: reads the app's own shipped code, for the user's own or an authorized app, endpoints unverified until `smith_probe`.

- [ ] **Step 1: Write the failing tests** in `tests/test_electron.py` against the fixtures: helper call sites give `(method, path)` pairs with templates turned into `{param}` placeholders; a `.map` with `sourcesContent` yields `Endpoint.summary` naming the original file; a null `sourcesContent` falls back to the bundle; the unresolvable path is absent and named in a note; no endpoint path contains `$`, backtick, quote or `+`; a header token literal and `user:pass@` are absent from `Source.model_dump_json()`; two same-path call sites in different files get distinct ids; a truncated `.asar` raises `ValueError` naming it; every endpoint has `verified is False` and `Source.kind == "app"`.
- [ ] **Step 2: Run** `uv run pytest tests/test_electron.py -q` → FAIL.
- [ ] **Step 3: Implement** the reader (asar index parse: 8-byte pickle header, then JSON index, then offsets), the extractor and the CLI/tool wiring.
- [ ] **Step 4: Run** `uv run pytest -q` → PASS; ruff clean.
- [ ] **Step 5: Commit** `feat(importers): read a JavaScript app's own HTTP calls into a Source`.
