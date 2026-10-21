# mcpsmith: MCP everything (live hub + Chrome extraction + REA bundle)

Status: draft for approval · 2026-10-20 · builds on mcpsmith 0.1.1 (`docs/design.md`)

## Intent

Today mcpsmith turns an OpenAPI file or a HAR into a *generated server project* you then install and
publish. The goal now is the "REA, but for MCP" experience: **one install, then your agent can turn
almost anything it can reach (a website, a logged-in dashboard, an API spec) into working tools in the
same session.** Publishing a standalone server becomes an optional export.

Motivating case (the acceptance target): the user's company HR/attendance dashboard, a classic
ASP.NET MVC app behind a username/password form and a cookie session. The user wants to ask their
agent "when did I arrive yesterday", "am I short on hours this month", "how much leave did I use",
"which days are missing a punch". Nobody will ever publish an MCP server for this system; it must stay
on the user's machine.

Recon on that dashboard (2026-10-20) found what a design must handle: the data comes from JSON XHR
calls (`POST /Home/GetAttendance` with Unix-timestamp range fields, `POST /Home/GetMonthlyReport` with
transliterated field names like `kasrekar`, `ezafekaradi`), authenticated only by the session cookie;
one page (leave ledger) loads nothing until UI events fire; SignalR traffic is noise.

## Decisions already made (by the user)

1. **Compose, don't fork REA.** The bundle installs REA unchanged from npm (`rea-agents`) next to
   mcpsmith. Needed REA changes go upstream as PRs.
2. **The hub is the product.** mcpsmith runs as one MCP server; connections become live tools in the
   current session and persist across sessions.
3. **Chrome DevTools MCP is part of the bundle,** with a best-practice skill, so the agent can drive a
   real browser and extract APIs deeply (including behind a login).
4. **First target:** the company dashboard above (read-only questions).

## Decisions from the second round (2026-10-21, user)

5. **Global product, developers first.** Audience is developers anywhere. No locale-specific defaults:
   timezone defaults to the system's local zone; transforms are generic (ISO date, Unix time, time
   zones, currency/unit scaling) with Jalali as one optional calendar among them. RTL/Persian support
   stays as it is.
6. **Apps go REA-first.** For Android apps, Electron apps and native desktop apps, REA runs first and
   statically extracts the API from the binary (HTTP client code, route declarations such as Retrofit
   annotations, URL/format strings, the app's JavaScript). v1 is **static extraction first**: no
   emulator, proxy certificate or Frida required.
7. **Live probing, GET only.** Because static extraction yields endpoints without responses, the agent
   may call discovered GET endpoints live (with the developer's own credentials) during design to get
   example responses for shaping and tests. Non-GET endpoints are probed only after the developer
   confirms.
8. **Name stays mcpsmith.** REA and Chrome DevTools MCP are components it installs.
9. **Setup supports Claude Code, Codex, Cursor and Gemini CLI on day one.**
10. **Guardrails:** the bundle works with any target the user has access to (their own accounts and
    data). Docs and skills state that respecting each service's terms is the user's responsibility;
    no extra authorization gates.
11. **App acceptance targets are open-source apps chosen by the builder**, so results can be shown in
    the repo.
12. **Credentials come from the developer.** The hub never automates an app's login. Static secrets
    (token, API key, cookie) are entered with `mcpsmith secret set <connection> <NAME>` (hidden
    terminal prompt, stored in the OS keychain; env vars remain a fallback); they never pass through
    the agent or into plan files. Browser logins, including OTP/2FA, are done by the developer in the
    shared Chrome. A one-time code needed at call time makes the tool return an error naming it; the
    developer supplies it with `mcpsmith secret set ... --once` and it is used for the next call only.
13. **Native desktop apps use proxy capture in v1** (after the spike showed ~4% static REST recall);
    Android and Electron stay static-first.
14. **Connections are personal** (one machine, no sharing/import format). Publishing is the existing
    export path: `mcpsmith generate` from a connection's plan, then the publish workflow; secrets are
    never exported, and a `browser`-auth connection must be switched to token/API-key auth first
    (`generate` says so).

## Goals

G1. **Live hub.** New MCP tools on the mcpsmith server:
  - `smith_connect(...)`: from a workdir with a validated plan, register the connection's tools in
    the running server and persist it under `~/.mcpsmith/connections/<name>/` (plan.yaml, source.json,
    no secrets).
  - Connected tools are exposed two ways: (a) as real MCP tools, announced with
    `notifications/tools/list_changed`; (b) through two always-present meta tools,
    `smith_tools(connection)` (names, descriptions, input schemas) and `smith_call(connection, tool,
    args)`, for clients that do not refresh their tool list.
  - On start the hub loads every saved connection. `smith_connections()` lists them;
    `smith_disconnect(name)` removes one.
  - Tool behaviour is identical to a generated server: the same `mcpsmith.runtime` `Api` + `shape`.
    A plan executes without codegen (an interpreter over `Plan`); `generate` stays as the export path.
G2. **Shared Chrome.** `mcpsmith browser` (and the setup command) launches one Chrome with
  `--remote-debugging-port` (default 9222) and a dedicated persistent profile
  (`~/.mcpsmith/chrome-profile`; Chrome requires a non-default profile for remote debugging), passing
  the user's `HTTPS_PROXY` as `--proxy-server` when set. Chrome DevTools MCP is registered with
  `--browser-url=http://127.0.0.1:<port>` so the agent drives this same browser.
G3. **Recording from the shared Chrome.** `smith_record_start(workdir, hosts)` /
  `smith_record_stop(workdir)` attach to the shared Chrome over CDP, record request + response
  (method, URL, headers, JSON bodies) for the listed hosts while the agent browses with DevTools MCP,
  and write `capture.har`; `smith_inspect` then works unchanged. Recording never returns header values
  to the agent (cookies stay out of the conversation; recon showed DevTools MCP's
  `get_network_request` prints the session cookie).
G4. **Browser-session auth.** A plan can declare `auth: {type: browser}`. At call time the hub reads
  the cookies for the base host from the shared Chrome over CDP and sends them; no password or cookie
  is stored on disk. A 401, or a redirect to a login page (pattern in the plan, e.g. `/Account/Login`),
  becomes a ToolError: "session expired: log in again in the mcpsmith Chrome window". Chrome not
  running is a ToolError with the command to start it.
G5. **Value transforms.** `ToolArg.transform` converts what the model passes into the wire format:
  at least `jalali_date->unix`, `date->unix` (ISO date, start of day; `_end` variant for end of day in
  a stated timezone), `jalali_date->gregorian`, `toman->rial`. And output transforms on fields:
  `unix->date`, `gregorian->jalali`, `rial->toman`, `minutes->hh:mm` as needed by the acceptance
  target. Small registry; unknown names are validation errors.
G6. **Bundle setup.** `mcpsmith setup` detects installed agents (Claude Code, Codex, Cursor, Gemini CLI
  at minimum), shows the planned config changes, backs up each config, and registers: the mcpsmith hub
  (`uvx mcpsmith mcp`), Chrome DevTools MCP (`npx chrome-devtools-mcp@<pinned> --browser-url=...`),
  and REA (`npx rea-agents@<pinned>`, optional, asked). It installs the skills. `--dry-run` prints only.
  The existing Claude Code plugin gains the two extra MCP servers.
G7. **Skills.** A new `extract-api` skill (deep extraction playbook, below); `build-mcp-server` updated
  to the hub flow (connect first, export optional); `design-mcp-tools` gains transforms and browser auth.
G8. **REA import.** `mcpsmith inspect` accepts a REA `capture_browser_scenario` result (JSON) by
  converting it to HAR internally (group events by transaction id).
G9. **App path (REA-first).** REA is the front end (inventory, decompilation, source maps, Go/PE
  metadata); mcpsmith adds a per-platform extractor on REA's output, because REA's own endpoint
  findings were not usable in the spike (see "Spike results"). Every extracted endpoint lands in a
  `Source` marked `verified: false` (method, path template, params, base URL where recovered; no
  response examples). A `smith_probe` step calls GET candidates live and stores scrubbed example
  responses, turning them verified; non-GET only after the developer confirms.
  - **Android:** REA's JADX engine → decompiled source of the HTTP interfaces → Retrofit annotation
    parser (GET/POST/PUT/DELETE/PATCH/HTTP + Path/Query/Field/Body/Part/Header), with resolution of
    R8-minified annotation class names. Locate interfaces by code search for path-like strings.
  - **Electron / JS apps:** prefer original sources from shipped source maps (`sourcesContent`) when
    REA's inventory finds them; otherwise an AST pass over the bundle (acorn, run via Node, which REA
    already requires) that finds request-wrapper call sites `(method, pathExpr, opts)` and resolves
    template literals and local constants. The agent renames minified parameters.
  - **Native desktop apps:** runtime proxy capture (user decision 13) — the program runs through a
    local intercepting proxy (mitmproxy, free) with its CA installed, the developer uses the
    features, and the flows become a HAR for the existing importer. Static extraction is limited to
    GraphQL operation bodies found as whole strings, plus REA's Go/PE metadata for fingerprinting.
    Certificate-pinned programs are out of scope.

## The extract-api playbook (skill content, summarized)

1. Ask the user for the goal as concrete questions/tasks; list the features they imply.
2. `mcpsmith browser` (or check it runs); open the site in DevTools MCP; the user logs in themselves
   in that window. Never ask for or type passwords.
3. `smith_record_start` with the target hosts. Walk each feature at least twice with different inputs
   (dates, filters, pages, a detail item) so the importer can tell parameters from constants. Trigger
   lazy UI (dropdowns, tabs, "show more"). Never click save/submit/approve/delete without the user's
   yes.
4. Use DevTools MCP to *navigate and read the page*, not to read request headers or bodies
   (`get_network_request` prints cookies into the conversation); the recording has the bodies.
5. `smith_record_stop` → `smith_inspect` → design the plan (rename cryptic fields, add transforms,
   `auth: browser`) → `smith_validate` → `smith_connect` → call each tool once and compare with what
   the page shows.
6. Personal data stays local: connections hold no captured response bodies after connect except
   scrubbed examples; tell the user that tool results are sent to the model like any conversation.

## Non-goals (v1)

- Forking or vendoring REA; a hosted service; captcha/JS-challenge solving.
- HTML-table extraction for server-rendered pages (the acceptance target is JSON; planned next).
- Runtime capture for Android/Electron (emulators, Frida hooks) — static extraction + live GET probing
  in v1; proxy capture only for native desktop apps. Certificate-pinned programs. iOS apps.
- Multi-call workflow tools in the plan (derived answers such as "missing punches" are computed by the
  agent from tool output, or by a hand-edited exported server).
- Writing tools for the acceptance target (read-only).

## Anti-goals (failure even if it "works")

- A password, cookie or token written to disk, to a plan, to a fixture, or printed to the agent by
  mcpsmith.
- Any write request sent without explicit user approval.
- Company or personal data in the mcpsmith repo, its tests or its docs.
- Breaking existing 0.1.x users: `generate`, CLI commands and generated servers keep working.

## Constraints

- Python ≥3.10, `mcp` SDK 2.x (current dependency), Windows + macOS + Linux. CDP access from Python
  via Playwright's `connect_over_cdp` (already the `capture` extra) or a small websocket client;
  builder's choice, but no new mandatory heavy dependency for users who never use the browser.
- Chrome DevTools MCP and REA are npm packages; setup must work when Node is missing (skip with a
  clear message) and must pin versions it writes into configs.
- The shell environment here exports `HTTPS_PROXY`; MCP clients start servers with a minimal env, so
  the hub's own proxy settings come from its config/env entries written by setup.
- Existing security rules stay: plan validation before any execution, auth only to the base host,
  write tools skipped by `check` unless `--writes`, scrubbing of captures.

## Acceptance

1. Fresh machine flow: `uvx mcpsmith setup` registers hub + DevTools MCP (+ REA if chosen) for
   Claude Code; restart; the agent sees `smith_*` tools and DevTools MCP tools.
2. Company dashboard, one session, following the `extract-api` skill: the user logs in in the mcpsmith
   Chrome; the agent records, designs and connects; then answers the four questions through hub tools
   (attendance for a Jalali date range; monthly worked/shortfall/overtime; leave used; days with a
   missing punch derived from attendance rows). No cookie value appears in the transcript after
   `smith_record_start`. After logging out in Chrome, the next call returns the "session expired" error.
3. New session: the saved connection's tools work without re-recording (while the browser session is
   valid).
4. A public API (hn.algolia.com) connected from scratch through the same flow, no login.
5. App path, one connection per spike target, each with read tools working live against the public
   API and recall reported in a repo showcase: Tusky APK (static Retrofit extraction + GET probing
   against a public Mastodon instance, ≥90% of the interface's endpoints recovered), GitHub Desktop
   (static JS/source-map extraction + GET probing against api.github.com with the developer's token,
   ≥80% recovered), gh CLI (proxy capture of a scripted session of read commands → working tools).
6. Existing test suite green; new tests cover the interpreter (parity with codegen on the existing
   fixtures), transforms, browser-auth cookie injection (fake CDP), record→HAR, setup dry-run output.

## Settled after design review

Facts from recon:
- The acceptance dashboard answers both the direct line and the proxy exit (login page 200 on both);
  recon logged in through Chrome without issue.
- "Leave used" comes from the monthly report JSON (per-month leave fields for entitled, hourly, sick,
  unpaid leave). The leave-ledger page is not needed for acceptance; leave *balance* is out of scope.
- hn.algolia.com acceptance uses the web UI's Algolia calls (already proven with a HAR on 0.1.x);
  its public search key goes in `fixed` per the guide.
- REA `capture_browser_scenario` output samples exist from recon; the builder runs REA to produce the
  test fixture.

Calls made by the designer (not the user; review these):
- **One egress per connection.** The shared Chrome uses `HTTPS_PROXY` when set (`mcpsmith browser
  --no-proxy` to go direct). The connection records which proxy (or none) Chrome used, and the hub's
  `browser`-auth calls use the same one, so cookie and calls share an exit.
- **Recorder output.** The recorder drops the values of `Cookie`, `Set-Cookie`, `Authorization` and
  anti-forgery/CSRF headers before writing. Hub recordings default to
  `~/.mcpsmith/work/<name>/` (the existing `.mcpsmith` CLI default is unchanged). `smith_connect`
  deletes `capture.har` and keeps only the scrubbed `source.json` + `plan.yaml`.
- **Export.** `validate` warns and `generate` refuses `auth: browser` plans in v1 (they need the hub's
  Chrome). Transforms live in `mcpsmith.runtime` and therefore work in exported servers too. Jalali
  conversion is implemented in-house (pure Python, full calendar arithmetic, tested against known
  dates); no new dependency.
- **Write safety in the hub.** `smith_call` refuses `read_only: false` tools; those are only reachable
  as real tools carrying destructive annotations, after the user confirms. `smith_connect` warns when a
  POST tool marked `read_only: true` has a mutation-looking path (save, delete, update, approve,
  create, submit, cancel, remove).
- **Time.** Plans get `timezone` (IANA name, default: the system's local zone, from the stdlib
  `zoneinfo`; on Windows this needs `tzdata`, which becomes a dependency). Date transforms use it for
  start/end of day.

## Spike results (2026-10-21, throwaway code in the session scratchpad)

| Target (open source, documented API) | REA's own endpoint findings | Extractor on REA's output | Verdict |
|---|---|---|---|
| Tusky 29.0 APK (Retrofit, Mastodon API, R8-minified) | none usable: class/method list only, no annotations | 113/114 endpoints (99%); method, path and every param kind+name correct; 0 false | strong |
| GitHub Desktop 3.6.7 (Electron, GitHub REST) | 0/45; 161 noise rows; bundle exceeded REA's node ceiling | 42/45 call sites; method 42/42, path template 37/42; params minified | strong |
| gh 2.102.1 (Go, GitHub REST + GraphQL) | Ghidra 8.8 min (default MCP timeout 330 s); string search ~0 | REST 5/120 (4%); GraphQL operations 35/39 (90%) | REST not viable statically → proxy capture |

Setup facts: REA's Android provider needs Java and its bundled `jadx-headless-mcp` jar (configured
through REA's env, e.g. `REA_JADX_MCP_JAR`); native analysis needs Ghidra plus a raised
`REA_GHIDRA_STARTUP_TIMEOUT_MS`. The REA CLI does not pick up the MCP registration's env; setup must
write both. The Android annotation-name resolution assumed R8's alphabetical naming of
`retrofit2.http` classes; it held for Tusky and is unconfirmed for other apps (fallback: identify
annotations by their retained parameter structure, or ask the developer).

Upstream REA requests (file as issues/PRs): expose class source and annotations on Android class/method
tools; code/annotation search without the 5000-class cap; a Retrofit/OkHttp endpoint extractor;
follow fetch/request wrappers and map positions through source maps in JS analysis; raise the JS node
and transfer ceilings; Go string-table splitting and per-call-site string xrefs; configurable MCP
timeouts and working analysis snapshots; the CLI reading the same provider config as the MCP server.

## Risks

- Android: apps without Retrofit (raw OkHttp, Ktor, GraphQL clients) need further extractors; v1
  covers Retrofit and falls back to strings + probing.
- Electron: heavily obfuscated bundles without source maps reduce the AST extractor's recall.
- Native: proxy capture fails on certificate pinning and on programs that ignore proxy settings.
- REA changes fast; mcpsmith must pin the REA version it drives and test against it.

## Left to the builder

Module layout; CDP client choice; whether dynamic tools are registered through the SDK or a custom
tool list; exact setup config formats per agent (verify against each agent's docs); transform naming;
how login-redirect detection is configured; pinned versions.
