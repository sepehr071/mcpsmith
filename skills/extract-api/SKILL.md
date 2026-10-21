---
name: extract-api
description: Turn a logged-in website or web app with no public API into live agent tools through the mcpsmith hub. Use when the user wants an agent to read data from a site they log in to (dashboard, portal, internal tool), or asks to "record", "connect" or "extract the API" of a site.
---

# Extract an API from a website and connect it

You record the site's own API calls in a shared Chrome, design a plan, and connect it as live tools in the mcpsmith hub. Needs the mcpsmith MCP server (`claude mcp add mcpsmith -- uvx mcpsmith mcp`) and Chrome DevTools MCP pointed at the shared Chrome (step 2). Connections are personal: one machine, one user. Respecting the site's terms is the user's responsibility.

Done means: every connected tool was called once and its output matches what the page shows.

## 1. Goal

Ask the user, then wait:
- Which questions or tasks should the agent answer with this site? Write them as concrete examples ("attendance for last month", "open invoices over 500"). List the features (pages, filters, detail views) they imply.
- Read-only, or also actions? Default read-only.
- Site URL and the hosts its data comes from (usually the site and an `api.` host).

## 2. Shared Chrome and login

```
mcpsmith browser                 # starts or reuses Chrome on port 9222; --port N, --no-proxy
npx chrome-devtools-mcp@1.11.0 --browser-url=http://127.0.0.1:9222
```

The second line is the Chrome DevTools MCP config: point it at the port `mcpsmith browser` printed, then restart the MCP client if it was configured differently. Open the site with DevTools `navigate_page`.
The user logs in themselves in that window (password, OTP, 2FA). Never ask for, read or type a password, token or one-time code. Logged-in cookies live only in that Chrome profile under `~/.mcpsmith` (`MCPSMITH_HOME`).

## 3. Record

Call `smith_record_start(name, hosts)` (`name` e.g. `hr`, `hosts` e.g. `["hr.example.com"]`; subdomains included). Then drive the page with DevTools:
- Walk each feature at least twice with different inputs (dates, filters, page numbers, a different detail item) so the importer can tell parameters from constants.
- Trigger lazy UI: dropdowns, tabs, "show more", date pickers, sort headers.
- Use `navigate_page`, `take_snapshot`, `click`, `fill` only to move around and read the page.
- NEVER call `get_network_request`: it prints request headers, including session cookies, into the conversation. The recording already keeps bodies; headers with cookies and tokens are saved as `<dropped>`.
- Stop and ask before any click that saves, submits, approves, sends, pays or deletes. Proceed only on a clear yes for that action.

Then `smith_record_stop(name)`. It returns the HAR path and request count; zero requests means wrong `hosts` or the tab was not touched.

## 4. Inspect and design

```
smith_inspect(source=<har path>, workdir=<dir of the har>, hosts=[...])
smith_guide()
smith_draft(workdir=<same dir>)
```

Read the digest and the guide fully, then edit `<workdir>/plan.yaml` yourself (see `design-mcp-tools`):
- Keep only tools for the goals from step 1; drop analytics, menus, config, auth plumbing.
- Rename cryptic fields and args to what a person says (`emp_id` to `employee_id`, `Item_dev_sort_date` through `map: {newest: ...}`).
- Add `transform` on args and `output.transforms` for dates, unix times, minutes, currency. Set `timezone` (IANA name; empty means the machine's local zone).
- Set `auth: {type: browser, login_path: /Account/Login}`; `login_path` is the path the site redirects to when logged out. Also set `session_cookie` to the login cookie's name: compare the cookie NAMES Chrome holds logged in vs logged out (`mcpsmith.browser.cookies_for`, names only, never print values). Some sites answer an ended session with HTTP 200 and an empty body.
- Fill `example` for every tool with values seen in the recording; `read_only: false` for anything that writes.

```
smith_validate(workdir=<dir>)     # fix every error; read warnings
smith_connect(workdir=<dir>, name="hr")
```

`smith_connect` adds the tools right now and saves the connection. If the client does not show new tools, use `smith_tools(connection)` and `smith_call(connection, tool, args)` (read-only tools only).

## 5. Verify against the page

Call each connected tool once with a real input. Open the same view in Chrome (`take_snapshot`) and compare the numbers, dates and row counts. Fix wrong units, time zones, missing fields or paging in `plan.yaml`, run `smith_validate`, then `smith_connect` again (same `name` replaces it). Call write tools only after the user confirms each call.

If a call says the session expired, ask the user to log in again in the shared Chrome window, then retry.

## Desktop program (own or authorized)

For a desktop program's API, the developer records the program's own traffic with their own proxy; mcpsmith imports the HAR. Use this only for software the user owns or is authorized to analyze; certificate-pinned programs are out of scope, and you offer no way around that.

1. Call `smith_app_desktop(workdir=<dir>)` and show the user the steps. The user installs and runs mitmproxy (`mitmdump --set hardump=<file>`), points that one program at it, uses the features they want as tools, and stops mitmdump. You never install, start or configure the proxy, install a certificate or change system proxy settings yourself.
2. Call `smith_app_desktop(har=<file>, hosts=[<the program's API host>], workdir=<dir>)`. Other programs' traffic on the same proxy is dropped by `hosts`. If it says nothing to import, the capture holds no JSON API calls: ask the user to record again.
3. Continue at step 4 (`smith_draft`). The endpoints came from real responses, so they are verified. Delete the HAR afterwards (step 6).

## 6. Privacy and publishing

- Tool results go to the model like any conversation text. Tell the user which personal fields the tools return and trim `fields` they do not need.
- Connections keep only scrubbed examples, not captured response bodies. Delete the HAR once `smith_connect` works.
- Static secrets (API key, token) are entered by the developer: `mcpsmith secret set <connection> <ENV_NAME>` (hidden prompt, OS keychain; `--once` for a one-time code). Never ask for them in chat or write them into a plan.
- `mcpsmith connections` / `smith_connections` lists what is saved; `smith_disconnect(name)` removes one.
- To share a server publicly use `generate` (see `build-mcp-server`). It refuses `auth: browser` plans: switch the plan to token/API-key auth first. Secrets are never exported.
