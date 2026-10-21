# App Path, Task 4: desktop apps via a capture proxy (plan 3, part 4)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans.

**Goal:** Let a developer turn their own (or an authorized) desktop program's API into agent tools by recording the program's own traffic through a local proxy they run, then feeding it into the existing pipeline.

**Architecture:** Almost nothing new. mitmproxy (free, OSS, not bundled) writes a HAR with its built-in `hardump` option; mcpsmith's HAR importer already reads HAR. So this task is a guided command that prints the exact steps for the developer's platform, then imports the resulting HAR with the existing `inspect` path, plus the skill text that keeps an agent from doing anything the developer did not ask for.

**Tech Stack:** Existing `importers.load` (HAR), `cli`, `server`. No new dependency; mitmproxy is the developer's own tool, invoked by the developer.

**Spec:** `docs/superpowers/specs/2026-10-20-mcp-everything-design.md` (G9 native bullet; decision 13: native desktop apps use proxy capture because static extraction recovered ~4% of REST paths from a Go binary). User scope (2026-10-21): the developer's own or authorized software; nothing that defeats a protection.

## Global Constraints

- mcpsmith never installs, starts or configures a proxy, never installs a CA certificate, and never changes system or OS proxy settings. It prints the commands and the developer runs them. The guidance must say plainly that intercepting a program's traffic requires that the developer owns the program or is authorized to analyze it, and that certificate-pinned programs are out of scope (no bypass is offered or described).
- `hardump` is mitmproxy's documented option ("Save a HAR file with all flows on exit"), used as `mitmdump --set hardump=<file>` and available in mitmproxy/mitmdump/mitmweb; the printed instructions use it, plus `--set save_stream_filter=` guidance for narrowing hosts.
- The import path is the existing HAR importer: no second parser. `app-desktop` with a HAR delegates to the same `inspect_source` the CLI already uses, so scrubbing of secrets, cookies and personal data applies unchanged.
- Endpoints from a capture keep the HAR importer's existing semantics (`verified: True`, real response examples) — unlike the static importers, a capture is evidence of a real answer.
- Nothing is executed from the capture's directory; the HAR is read as untrusted data.
- Existing behaviour stays; full suite green; ruff clean; commits on `feat/app-path` without AI attribution.

## Review Focus

1. The printed steps must be correct per platform (Windows/macOS/Linux) for proxy address, the certificate step and the dump flag — a wrong flag wastes the developer's session. (Test asserts the exact `--set hardump=` form and that no step tells the user to disable certificate validation.)
2. A HAR that contains only the proxy's own CONNECT entries or no JSON → clear "nothing to import" message, not an empty `Source` written silently. (Test.)
3. A capture of a program that is not the target (other apps' traffic on the same proxy) → `--host` filtering is offered and applied. (Test.)
4. The guidance must not describe defeating pinning or any protection; it must say such programs are out of scope. (Test greps the skill text for the sentence.)
5. Secrets in a desktop capture (an app token in a header) are scrubbed by the existing importer before anything is written. (Test.)

---

### Task 4: `app-desktop` command + skill

**Files:**
- Modify: `src/mcpsmith/cli.py`, `src/mcpsmith/server.py`, `skills/extract-api/SKILL.md` (a "desktop app" section), `README.md` (one line in the app-path list), `docs/design.md`
- Create: `tests/test_desktop.py`

**Interfaces:**
- Produces:
  - CLI `mcpsmith app-desktop [HAR] [--port 8080] [--host H]... [-w DIR]`: with no HAR, prints the numbered steps (run `mitmdump --set hardump=<workdir>/capture.har -p <port>`, point the program at `http://127.0.0.1:<port>` or the OS proxy for that program only, install mitmproxy's CA for the program's trust store as mitmproxy's own docs describe, use the features you want as tools, stop mitmdump) and exits 0; with a HAR, imports it through the existing `inspect_source` and prints the endpoint count and the `mcpsmith draft` next step. Refuses to overwrite an existing `source.json` without `--force`.
  - MCP tool `smith_app_desktop(har=None, hosts=None, workdir=..., port=8080)` → either `{"steps": [...]}` or `{"workdir", "endpoints", "digest"}`; `read_only=False`; docstring states the developer runs the proxy themselves and that this is for their own or authorized software.
- Consumes: `cli.inspect_source`, `importers.load`.

- [ ] **Step 1: Write the failing tests** in `tests/test_desktop.py`: the steps text contains `--set hardump=` and the workdir path, names the port, and contains no instruction to disable TLS verification (assert the absence of "verify" / "--ssl-insecure" / "pinning bypass"); a hand-written HAR with one JSON API call and one CONNECT entry imports to one endpoint; a HAR with no JSON calls prints "nothing to import" and writes no `source.json`; `--host` keeps only the named host; an `Authorization` header value in the HAR is absent from the written `source.json`; the skill text contains the out-of-scope sentence about certificate-pinned programs.
- [ ] **Step 2: Run** `uv run pytest tests/test_desktop.py -q` → FAIL.
- [ ] **Step 3: Implement** the command, the tool, and the skill/README/design wording.
- [ ] **Step 4: Run** `uv run pytest -q` → PASS; ruff clean; `claude plugin validate .` passes.
- [ ] **Step 5: Commit** `feat: import a desktop app's own traffic from a developer-run proxy`.
