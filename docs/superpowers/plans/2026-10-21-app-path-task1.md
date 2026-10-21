# App Path, Task 1: unverified endpoints and live probing (plan 3, part 1)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans.

**Goal:** Let a `Source` hold endpoints that were read from an app's own code (method, path, params, no response yet), and verify them by calling the read-only ones live, so later platform importers (Android, Electron, desktop capture) all share one verification step.

**Architecture:** One new `Endpoint.verified` flag, and a `probe` module that calls GET candidates through the existing `mcpsmith.runtime.Api`, stores scrubbed example responses, and marks them verified. No new transport, no new auth path.

**Tech Stack:** Existing: pydantic model, `runtime.Api`, `importers.har._scrub`, `mcp` server tools. No new dependency.

**Spec:** `docs/superpowers/specs/2026-10-20-mcp-everything-design.md` (G9; decisions 6, 7, 10). Scope confirmed by the user on 2026-10-21: apps the developer owns or is authorized to analyze; nothing that defeats a protection (no certificate-pinning bypass, no DRM or licensing circumvention).

## Global Constraints

- `verified: bool = True` on `Endpoint`, so every existing importer and saved `source.json` keeps its current meaning.
- Probing calls only `GET` endpoints by default; any other method requires an explicit opt-in argument and is reported as such. A probe that would send a write is skipped with a reason, never sent silently.
- Probe responses go through `importers.har._scrub` before being stored, like recordings do (spec anti-goal: no secrets or personal data on disk).
- Probing uses one `Api` built from the source's base URL plus the developer's own credentials (env var, keychain, or the shared Chrome session), exactly as hub tools do. No credential is created, guessed or brute-forced.
- Rate limit: at most 2 concurrent requests and a short delay between probes (reuse `Api`'s concurrency cap and jitter); a 429 or 403 stops probing that host with a clear message.
- Existing behaviour stays; full suite green; ruff clean; commits on `feat/app-path` without AI attribution.

## Review Focus

1. A probe target whose path still has unfilled `{placeholders}` → skipped with a reason, never requested with a literal brace. (Task tests.)
2. A 403/429 → probing stops for that host and says so, instead of hammering it. (Task tests.)
3. A probe response containing a token or an email → scrubbed before it reaches `source.json`. (Task tests.)
4. An endpoint with `verified: false` and no response example → `draft` still produces a usable plan, and `validate` warns rather than crashing. (Task tests.)
5. Non-GET candidates → never probed without the opt-in. (Task tests.)

---

### Task 1: `Endpoint.verified` + `smith_probe`

**Files:**
- Modify: `src/mcpsmith/model.py`, `src/mcpsmith/design/_digest.py` (mark unverified endpoints in the digest), `src/mcpsmith/design/_draft.py` (prefer verified endpoints; keep unverified ones usable), `src/mcpsmith/server.py` (new tool), `src/mcpsmith/cli.py` (new command)
- Create: `src/mcpsmith/probe.py`
- Test: `tests/test_probe.py`

**Interfaces:**
- Produces:
  - `Endpoint.verified: bool = True` — False means "read from the app's code, not yet seen answering".
  - `probe.run(source: Source, *, writes: bool = False, limit: int = 20, env: dict[str, str] | None = None) -> list[ProbeResult]`; `ProbeResult` dataclass: `endpoint` (id), `ok`, `status`, `error`, `chars`, `skipped_reason`.
  - Each successful probe sets that endpoint's `response_example` (scrubbed, trimmed like the HAR importer does) and `verified = True`; the caller saves the source.
  - CLI `mcpsmith probe [-w DIR] [--limit N] [--writes]` → one line per endpoint; exit 1 if every probe failed.
  - MCP tool `smith_probe(workdir, limit=20, writes=False)` → `{"verified": n, "failed": n, "skipped": n, "results": [...]}`, `read_only=False` (it calls a live API), docstring saying writes need the user's confirmation.

- [ ] **Step 1: Write the failing tests** in `tests/test_probe.py`, with a `Source` built in code (4 endpoints: a GET with a filled path, a GET whose path has an unfilled `{id}`, a POST, and a GET on a host that answers 429) and `Api.set_transport(httpx.MockTransport(...))`:
```python
async def test_probe_verifies_get_and_scrubs():
    # GET /items -> {"items":[{"id":1,"email":"a@b.co","token":"secret123"}]}
    results = probe.run(src)
    ep = src.endpoint("GET /items")
    assert ep.verified and "secret123" not in json.dumps(ep.response_example)
    assert "a@b.co" not in json.dumps(ep.response_example)

def test_probe_skips_unfillable_path_and_writes():
    r = {x.endpoint: x for x in probe.run(src)}
    assert "placeholder" in r["GET /items/{id}"].skipped_reason
    assert "write" in r["POST /items"].skipped_reason and src.endpoint("POST /items").verified is False

def test_probe_stops_host_on_429():
    # first GET -> 429; the second GET on the same host is skipped, not sent
    assert sent == 1 and "rate" in results[1].skipped_reason
```
- [ ] **Step 2: Run** `uv run pytest tests/test_probe.py -q` → FAIL (module missing).
- [ ] **Step 3: Implement** `Endpoint.verified`, `probe.run`, the digest/draft touches (digest marks `unverified`; draft keeps such endpoints but their `Output.fields` stay empty when there is no example), the CLI command and the MCP tool.
- [ ] **Step 4: Run** `uv run pytest -q` → PASS; ruff clean.
- [ ] **Step 5: Commit** `feat: verify code-read endpoints by probing them live`.
