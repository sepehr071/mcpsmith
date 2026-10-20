---
name: design-mcp-tools
description: Design or review the tools of an MCP server (a mcpsmith plan.yaml, or any existing server's tool names, descriptions, arguments and output size). Use when the user asks to improve, audit or write tool definitions so agents call them correctly and cheaply.
---

# Design MCP tools

The source of truth is `mcpsmith guide` (prints the tool-design rules). Run it first and follow it where it differs from this list. This file is the checklist and the measuring loop.

```
mcpsmith guide
```

Without a shell, call the `smith_guide` MCP tool (`claude mcp add mcpsmith -- uvx mcpsmith mcp`).

## Checklist (per plan or server)

Tool set
- Few tools: aim under 10, hard stop 20 (validation warns above 20). One tool per user task, not per endpoint. Merge list + filter + sort variants into one tool with args.
- Drop endpoints no stated task needs (admin, health, duplicates, tracking calls).
- Names: `<prefix>_<verb_noun>`, snake_case, stable. Renaming later breaks callers.

Descriptions
- First line: one complete sentence on what the tool returns. It becomes the catalog summary, so it must stand alone and end with a period.
- Then: when to use it, what to call next (name the tool), what to do when the result is empty.
- Plan `description` (the whole server) is at most 100 characters.

Arguments
- Plain names a person would say: `query`, `max_price`, `city`. Not wire names like `q[price_lte]`.
- Each description has the unit and one example value ("price in toman, e.g. 500000").
- Add `enum`, `minimum`, `maximum`, `default` where known. Required only when the call cannot work without it (a required arg cannot have a default).
- Cut args the model never needs; pin constants with `fixed:`.
- Cryptic wire values (sort codes, index names): use `map: {newest: sort_date_desc}` so the model sees plain words.
- A public key the site ships to every visitor may go in `fixed:`; a user's own token stays in `auth` (env var).
- Each arg `to:` is one of `path.<n>`, `query.<n>`, `header.<n>`, `cookie.<n>`, `body`, `body.<a.b>`.

Output
- `output.root` points at the useful part; `output.fields` maps short names to dotted paths. Compact: id, title/name, price, url, one-line status. Drop images arrays, tracking, HTML, nulls.
- Keep every id or slug the next tool needs as input.
- `max_items` on lists (the result also reports the total count); lower `clip` for long strings.
- Paginated APIs: expose `page` (or cursor) as an arg and mention in the description how to get the next page.
- Do not return raw JSON dumps; an endpoint response over 4 KB without `fields` triggers a warning.

Safety and server
- `read_only: true` only for GET-like reads; writes get `read_only: false` and say what they change.
- `example` for every tool with required args; real values that return data.
- `instructions`: the cross-tool workflow ("search, then details by id"), units, currency, language, rate limits, limits of the data.
- Secrets only via env vars (`<P>_...`), never in args or descriptions.

## Measure

```
mcpsmith validate -w .mcpsmith
mcpsmith check <project> --json
```

`check` calls each tool live with its `example` and reports `ok`, `error`, `chars` (output size) and `empty_fields` (output fields that came back empty).
- Aim under about 4,000 chars per call. Over that: trim `fields`, lower `max_items`/`clip`, split the tool.
- `empty_fields` non-empty: wrong dotted path or an optional field; open `tests/fixtures/<tool>.json` for the real shape.
- Any `error`: fix the example, auth or mapping before polishing wording.
- Non-GET tools hit the real service; get the user's confirmation before running `check` on them.

Loop: edit `plan.yaml`, `mcpsmith validate`, `mcpsmith generate -o <project> --force`, `cd <project> && uv run pytest`, `mcpsmith check <project>`. Regenerating overwrites hand edits to `server.py`.

## Reviewing a server not made with mcpsmith

Read its tool list, apply the same checklist, and report findings as: tool, problem, concrete rewrite (new name, first-line sentence, trimmed fields). Measure real output sizes by calling the tools, not by guessing. Do not claim a size you did not measure.
