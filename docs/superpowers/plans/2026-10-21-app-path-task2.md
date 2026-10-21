# App Path, Task 2: Android endpoint extractor (plan 3, part 2)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans.

**Goal:** Turn the HTTP interface declarations in a decompiled Android app into a `Source` of unverified endpoints, so a developer can give their agent tools for their own account in their own (or an authorized) app.

**Architecture:** REA (already installed by `mcpsmith setup`) decompiles the APK and returns Java source for a class; mcpsmith parses the Retrofit annotations in that text into `Endpoint`s with `verified: false`. Task 1's `probe` then verifies the read-only ones. No new transport and no bypass of any protection: this reads declarations the app itself ships.

**Tech Stack:** Python stdlib `re`; the REA CLI (`npx -y rea-agents@6.7.0 …`) invoked through `subprocess` with injected runner for tests. No new dependency.

**Spec:** `docs/superpowers/specs/2026-10-20-mcp-everything-design.md` (G9 Android bullet; "Spike results" table: 113/114 endpoints with method, path and parameter kinds correct on Tusky 29.0). User scope (2026-10-21): the developer's own or authorized app; nothing that defeats a protection.

## Global Constraints

- Input is a decompiled-class text (Java) plus the app's base URL, supplied by the caller. mcpsmith never downloads an app, and never ships or installs a decompiler itself; REA is the provider and must already be configured (its own `rea doctor` reports that).
- Parser supports Retrofit's declarations only: `@GET @POST @PUT @PATCH @DELETE @HEAD @OPTIONS @HTTP(method=…, path=…, hasBody=…)` for the request line, and `@Path @Query @QueryMap @Field @FieldMap @Body @Part @PartMap @Header @HeaderMap @Url` for parameters, plus `@FormUrlEncoded`/`@Multipart` as body-encoding markers.
- Minified annotation names (release builds rename `retrofit2.http.*`): resolve them by their retained **structure** (which annotation carries a `String value`, which marks a body, the `@HTTP`-shaped one with `method`/`path`/`hasBody`) and by cross-checking against a path-like string; when a name cannot be resolved with confidence, emit the endpoint with `method: ""` and a note rather than guessing. Never assume alphabetical ordering (the spike found that assumption held for one app and is unconfirmed elsewhere).
- Every produced `Endpoint` has `verified: false`, `count: 0`, `response_example: None`, and `summary` set to the interface method's name (e.g. `homeTimeline`) so the digest is readable.
- Base URL comes from the caller (apps often build it per account); if the caller gives none, `Source.base_url` is `""` and `validate` already errors until a plan sets one.
- No secret from the app (hardcoded keys, tokens) is ever copied into the `Source`: run every string literal that becomes a param example or header value through `importers.har._scrub`.
- Existing behaviour stays; full suite green; ruff clean; commits on `feat/app-path` without AI attribution.

## Review Focus

1. A method whose path is built by concatenation (`"users/" + id`) → emitted with the literal part and a note, or skipped with a reason — never a path containing `+` or a Java expression. (Tests.)
2. A hardcoded credential in an `@Header("Authorization") String` default or a literal → never stored. (Tests.)
3. An interface with two methods on the same path and different parameters → two endpoints, distinct ids. (Tests.)
4. A class text that is not a Retrofit interface (no annotations) → empty endpoint list and a clear message, not a crash. (Tests.)
5. `@HTTP(method="DELETE", hasBody=true)` → method DELETE, body marked, and it stays unverified and unprobed (write). (Tests.)

---

### Task 2: Retrofit parser + `smith_app_android`

**Files:**
- Create: `src/mcpsmith/importers/android.py`, `tests/test_android.py`, `tests/fixtures/android/interface.java` (hand-written, no real app code: a small interface covering every supported annotation, one concatenated path, one minified-name case, one `@HTTP`)
- Modify: `src/mcpsmith/importers/__init__.py` (not auto-detected — exposed as its own function), `src/mcpsmith/server.py` (tool), `src/mcpsmith/cli.py` (command)

**Interfaces:**
- Produces:
  - `android.parse_interface(java: str, base_url: str = "", title: str = "") -> Source` — endpoints from one decompiled interface.
  - `android.rea_class_source(apk: Path, class_name: str, runner=subprocess.run) -> str` — thin wrapper that asks REA for a class's source; raises `RuntimeError` with REA's install/config hint when REA or its decompiler provider is unavailable.
  - `android.find_api_classes(apk: Path, runner=subprocess.run) -> list[str]` — class names whose code contains a path-like string (`"api/`, `"/v1/`, …), via REA's code search.
  - CLI `mcpsmith app-android <apk> [--class NAME] [--base-url URL] [-w DIR]`: with no `--class`, lists candidate classes and exits 0; with one, writes `source.json` + `digest.md` into the workdir and prints the next step (`mcpsmith probe`).
  - MCP tool `smith_app_android(apk, class_name=None, base_url="", workdir=...)` → candidates, or `{"workdir", "endpoints", "unverified", "digest"}`; `read_only=False` (it runs REA); docstring: for the user's own or authorized app, and the endpoints are unverified until `smith_probe`.

- [ ] **Step 1: Write the failing tests** in `tests/test_android.py` against the fixture: every annotation kind maps to the right `Param.location` (`path`/`query`/`body`/`header`) and `required`; `@FormUrlEncoded` + `@Field` → body params; the concatenated path is skipped with a reason or emitted with its literal prefix and a note (assert no `+` in any `path`); a literal `Authorization` value is absent from the Source JSON; two same-path methods give two ids; `@HTTP(method="DELETE", hasBody=true)` → `method == "DELETE"`; a class with no annotations → `endpoints == []`; every endpoint has `verified is False`.
- [ ] **Step 2: Run** `uv run pytest tests/test_android.py -q` → FAIL.
- [ ] **Step 3: Implement** the parser, the two REA wrappers (runner injected; never execute anything from the APK's own directory), the CLI command and the MCP tool.
- [ ] **Step 4: Run** `uv run pytest -q` → PASS; ruff clean.
- [ ] **Step 5: Commit** `feat(importers): read Android HTTP interfaces into a Source`.
