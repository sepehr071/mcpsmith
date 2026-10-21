# Setup Bundle Implementation Plan (plan 2 of 3)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `mcpsmith setup` registers the mcpsmith hub, Chrome DevTools MCP (attached to the shared Chrome) and REA with Claude Code, Codex, Cursor and Gemini CLI, and installs mcpsmith's skills — one command, previewed, backed up, reversible.

**Architecture:** A per-agent target module edits each agent's user-level MCP config (JSON for Claude Code/Cursor/Gemini, TOML via `tomlkit` for Codex) with read → merge → backup → write → verify, never touching unrelated entries. Skills ship inside the wheel and are copied to `~/.claude/skills` (Claude Code) and `~/.agents/skills` (Codex, Cursor, Gemini). REA is registered by delegating to REA's own non-interactive setup.

**Tech Stack:** Python ≥3.10 stdlib (json, shutil, importlib.resources, subprocess), `tomlkit` (new dependency, comment-preserving TOML). Tests: pytest with a fake HOME.

**Spec:** `docs/superpowers/specs/2026-10-20-mcp-everything-design.md` (G6; decisions 8, 9). Research facts (2026-10-21) are recorded in Global Constraints below.

## Global Constraints

- Config locations (user level): Claude Code `~/.claude.json` (`$CLAUDE_CONFIG_DIR/.claude.json` if set), key `mcpServers.<name>` = `{"type":"stdio","command","args","env"}`; Codex `~/.codex/config.toml` (`$CODEX_HOME` if set), table `[mcp_servers.<name>]` with `command`, `args`, `startup_timeout_sec = 30`, sub-table `env`; Cursor `~/.cursor/mcp.json`, `mcpServers.<name>` = `{"type":"stdio","command","args","env"}`; Gemini `~/.gemini/settings.json` (`$GEMINI_CLI_HOME/.gemini/settings.json` if set), `mcpServers.<name>` = `{"command","args","env","timeout":30000}`.
- Skills: Claude Code `~/.claude/skills/<name>/`; Codex, Cursor, Gemini `~/.agents/skills/<name>/`. Skill folder name equals its frontmatter `name`.
- Server entries (names exactly): `mcpsmith` → `uvx mcpsmith@<package version> mcp`; `chrome-devtools` → `npx -y chrome-devtools-mcp@1.11.0 --browser-url=http://127.0.0.1:9222`; REA → delegated: `npx -y rea-agents@6.7.0 setup --client <id> --yes` with ids `claude_code`, `codex`, `cursor`, `gemini_cli`.
- On Windows, `npx` commands are written as `command: "cmd"`, `args: ["/d","/c","npx",...]`; `uvx` stays as is.
- `env` written for the `mcpsmith` entry: `HTTPS_PROXY`/`HTTP_PROXY`/`NO_PROXY` from setup's environment when set, with userinfo (`user:pass@`) stripped (reuse `browser.clean_proxy`); never write any other secret.
- Never modify or remove entries mcpsmith did not create; preserve TOML comments and unrelated tables; JSON written with 2-space indent and UTF-8, original key order kept.
- Before the first write to a file: copy it to `<file>.mcpsmith.bak` (do not overwrite an existing backup). After writing, re-read and verify the entry; on failure restore the backup and report.
- Node.js missing (`npx` not on PATH) → skip `chrome-devtools` and REA with one clear line each; still do the rest.
- Default asks for confirmation after printing the plan; `--yes` skips it; `--dry-run` prints the plan and writes nothing; non-interactive stdin without `--yes` → error, exit 2.
- New dependency only: `tomlkit>=0.12`. Existing behaviour stays; full suite green; ruff clean; commits on branch `feat/setup` without AI attribution.

## Review Focus

1. Existing user configs with unrelated servers, comments (TOML) and other top-level keys → untouched after setup and after `--remove`. (Task 2 tests.)
2. Running setup twice → idempotent: no duplicate entries, no second backup overwrite. (Task 3 test.)
3. A config file that is malformed JSON/TOML → that agent is skipped with a clear message; nothing written to it. (Task 2 test.)
4. Proxy env with credentials → credentials never written. (Task 2 test.)
5. Agent not installed → not touched unless named with `--agents`. (Task 3 test.)

---

### Task 1: Ship skills in the package

**Files:** Modify `pyproject.toml`; Create `src/mcpsmith/skills_path.py`; Test `tests/test_setup.py`

**Interfaces:**
- Produces: `skills_path.skills_dir() -> Path` — the packaged skills directory (`importlib.resources.files("mcpsmith") / "skills"` when present, else the repo's `skills/` for editable installs); contains `build-mcp-server`, `design-mcp-tools`, `extract-api`.

- [ ] **Step 1:** Test `test_skills_dir_has_all_skills`: the three folders exist, each with a `SKILL.md` whose frontmatter `name` equals the folder name. Test `test_wheel_contains_skills`: `uv build --wheel` into `tmp_path`, the wheel's namelist contains `mcpsmith/skills/extract-api/SKILL.md` (mark the test `slow` only if it exceeds 20 s).
- [ ] **Step 2:** Run → FAIL. **Step 3:** Add `[tool.hatch.build.targets.wheel.force-include] "skills" = "mcpsmith/skills"`; implement `skills_dir`. **Step 4:** Run full suite → PASS. **Step 5:** Commit `build: ship skills inside the package`.

### Task 2: Agent targets

**Files:** Create `src/mcpsmith/setup/__init__.py`, `src/mcpsmith/setup/agents.py`; Modify `pyproject.toml` (`tomlkit`); Test `tests/test_setup.py`

**Interfaces:**
- Produces:
  - `SERVERS: dict[str, list[str]]` — base commands per server name (from Global Constraints), and `server_entry(name: str, windows: bool, env: dict[str,str]) -> dict` (command/args, Windows `cmd /d /c` wrapping for npx).
  - `class Agent` (one per agent: `claude`, `codex`, `cursor`, `gemini`) with `id`, `rea_client`, `config_path(home: Path, environ) -> Path`, `skills_root(home) -> Path`, `detected(home, environ, which) -> bool` (config dir exists or CLI on PATH), `add(doc, name, entry) -> doc`, `remove(doc, name) -> doc`, `has(doc, name) -> bool`, `load(path) -> doc` (raises `ValueError` on malformed), `dump(doc) -> str`.
  - `AGENTS: dict[str, Agent]`.
- [ ] **Step 1:** Tests on in-memory docs: each agent adds `mcpsmith` and `chrome-devtools` with exact shapes from Global Constraints; unrelated servers/keys preserved; Codex TOML comment and an unrelated `[mcp_servers.other]` survive add+remove; `remove` deletes only our names; malformed JSON/TOML → `ValueError`; `server_entry("chrome-devtools", windows=True, …)` → `command == "cmd"`, args start `["/d","/c","npx","-y","chrome-devtools-mcp@1.11.0"]`; mcpsmith env from `HTTPS_PROXY=http://u:p@h:1` → `"http://h:1"`; `CLAUDE_CONFIG_DIR`/`CODEX_HOME`/`GEMINI_CLI_HOME` honored.
- [ ] **Step 2:** Run → FAIL. **Step 3:** Implement. **Step 4:** Full suite → PASS. **Step 5:** Commit `feat(setup): agent config targets`.

### Task 3: `mcpsmith setup` command

**Files:** Create `src/mcpsmith/setup/run.py`; Modify `src/mcpsmith/cli.py`; Test `tests/test_setup.py`

**Interfaces:**
- Consumes: `AGENTS`, `server_entry`, `skills_dir`.
- Produces: CLI `mcpsmith setup [--agents claude,codex,cursor,gemini] [--no-rea] [--no-skills] [--dry-run] [--yes] [--remove]`; `plan(home, environ, agents, rea, skills, remove) -> list[Change]` (`Change`: agent, kind `server|skill|rea`, target path or command, action `add|update|remove|skip`, reason); `apply(changes, runner=subprocess.run) -> list[str]` (result lines).
- Behaviour: detected agents by default (or `--agents`); prints the plan (paths, entries, skill folders, REA command), asks `Apply these changes? [y/N]` unless `--yes`/`--dry-run`; per file backup→write→verify (restore on verify failure); skills copied with `shutil.copytree(dirs_exist_ok=True)` (only mcpsmith's three folders; `--remove` deletes only those); REA via `runner([npx..., "setup", "--client", id, "--yes"])` (Windows-wrapped), `--remove` → `rea-agents@6.7.0 uninstall --client id --yes`; final line tells the user to restart their agents and to run `mcpsmith browser` before using Chrome DevTools.
- [ ] **Step 1:** Tests with fake HOME and stub `which`/runner: `--dry-run` writes nothing and lists every change; a real run writes entries + skills + backups and calls REA once per agent with the right client id; second run → no changes reported as `add`, backups unchanged; `--remove` restores a config equal to the original except formatting (JSON) / byte-identical comments (TOML), skills gone; undetected agent untouched; malformed config → that agent skipped, others done; no `npx` → chrome-devtools and REA skipped with messages; stdin not a TTY and no `--yes` → exit 2.
- [ ] **Step 2:** Run → FAIL. **Step 3:** Implement. **Step 4:** Full suite → PASS. **Step 5:** Commit `feat: mcpsmith setup registers the bundle with coding agents`.

### Task 4: Plugin, docs, release 0.3.0

**Files:** Modify `.claude-plugin/plugin.json` (add `chrome-devtools` and `rea` servers, pinned, same args as setup on non-Windows), `README.md` (Install section: `uvx mcpsmith setup`; plugin users don't also run setup), `AGENTS.md`, `skills/build-mcp-server/SKILL.md` (setup line), version 0.3.0 in all version spots; `uv lock`.

- [ ] **Step 1:** `claude plugin validate .` → "Validation passed"; `uv run pytest -q`; ruff.
- [ ] **Step 2: Acceptance** — run `mcpsmith setup --dry-run` on the real machine and show the plan; run the real setup with `HOME`/`USERPROFILE` pointed at a scratch dir seeded with copies of the four real config files (no real config touched) and check: entries added, comments/unrelated servers preserved, `--remove` reverts. Real-machine install only with the user's yes.
- [ ] **Step 3:** Commit `chore: release 0.3.0`; PR `feat/setup` → main; CI green; merge; tag `v0.3.0`; publish workflow green.
