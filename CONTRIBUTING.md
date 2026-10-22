# Contributing

## Dev setup

```bash
uv sync
uv run pytest -q          # live tests are skipped
uv run ruff check .
uv run ruff format .      # also formats Python blocks inside .md files
```

Python 3.10+, ruff line length 120.

## Layout

`src/toolsmelt/` holds the code; `model.py` is the data contract (`Source`, `Plan`), so check every stage that reads a field before renaming it. `toolsmelt.runtime` is imported by every generated server: keep it small and backward compatible. Design notes are in `docs/design.md`.

## Plans and specs

Larger changes are written down first: a design in `docs/superpowers/specs/` and step-by-step task plans in `docs/superpowers/plans/`, named `YYYY-MM-DD-<topic>.md`. Add or update one when your change spans several modules, and open the PR with the plan beside the code.

## Live tests

Tests marked `live` call real APIs, LLMs or need a real APK, and are skipped by default (`addopts = "-m 'not live'"`). Run them with `uv run pytest -m live`. They read their inputs from environment variables and skip when those are missing, for example `TOOLSMELT_TEST_APK` and `REA_JADX_MCP_JAR` for the Android test.

## Pull requests

- Keep the diff small and match the surrounding code.
- Add or update a test for new behavior.
- Do not add a dependency without discussing it in an issue first.
- Never commit tokens, cookies, HARs or app source; scrub fixtures.
