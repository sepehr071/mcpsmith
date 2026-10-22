# Extending mcpsmith

mcpsmith finds extensions in installed packages through Python entry points, so you can add an input format, a
value transform or a sign-in provider without forking. Install your package in the same environment as mcpsmith
(and add it to a generated server's dependencies when that server uses it). There are three groups:

| Group | Entry point name | Object it points to |
|---|---|---|
| `mcpsmith.importers` | any label | an object with `name`, `sniff` and `load` |
| `mcpsmith.transforms` | any label | a module or object with `INPUT` and/or `OUTPUT` dicts |
| `mcpsmith.signin` | the provider name used in `auth.provider` | a module or object implementing `Provider` |

Plugins load on first use, once per process. A plugin that fails to import, or does not fit its interface, is
skipped with a warning on the `mcpsmith` logger (stderr); mcpsmith keeps working without it. Built-in names always
win: a plugin cannot replace a built-in transform or provider.

```toml
# your package's pyproject.toml
[project.entry-points."mcpsmith.importers"]
insomnia = "mcpsmith_insomnia:IMPORTER"

[project.entry-points."mcpsmith.transforms"]
money = "mcpsmith_money.transforms"

[project.entry-points."mcpsmith.signin"]
acme = "mcpsmith_acme.signin"
```

## Importers

`mcpsmith inspect <file>` (and `smith_inspect`) call `mcpsmith.importers.load`. It reads the file, parses it as
JSON or YAML when it can, and tries the built-in formats first (HAR, REA capture, Postman collection,
OpenAPI/Swagger, cURL commands, database URLs). Any other local file is offered to each plugin's `sniff`; the first
that returns true gets `load`. URLs reach built-ins only.

The interface is `mcpsmith.importers.Importer`:

```python
from pathlib import Path
from typing import Any

from mcpsmith.model import Source


class InsomniaImporter:
    name = "insomnia"

    def sniff(self, path: Path, data: Any | None) -> bool:
        # data: the parsed JSON/YAML value, or None when the file is binary or not JSON/YAML
        return isinstance(data, dict) and data.get("_type") == "export" and "__export_format" in data

    def load(self, path: Path, data: Any | None, **opts: Any) -> Source:
        # opts: hosts (list of hosts to keep, or None); accept unknown options for later versions
        ...
        return Source(kind=self.name, title=path.stem, base_url="https://api.example.com", endpoints=[])


IMPORTER = InsomniaImporter()
```

`sniff` must be cheap and must not raise for files it does not know; a `sniff` that raises is skipped with a
warning. `load` errors reach the user, so raise `ValueError` with a message that says what is wrong with the file.

What the `Source` must contain:

- `kind`: your importer's name. `title`, and `base_url` (scheme and host, plus a path prefix if every endpoint
  shares it).
- One `Endpoint` per operation: a unique `id`, the HTTP `method` and the `path` relative to `base_url` with
  `{name}` placeholders, and its `params` (with `location`: path, query, header, cookie or body).
- `body_example` and `response_example` from real data when you have it: the designing agent and the generated
  tests rely on them. `auth` lists the `AuthScheme`s the API uses, by name, never their values.
- Never guess. If you cannot tell an endpoint's method, a parameter's kind or a path, leave it out and say why in
  the endpoint `description` or the Source `description`.

## Privacy rules for importers

A Source is written to disk, shown to the designing agent and copied into tests. Use `mcpsmith.scrub`, the same
functions the built-in importers use:

- `scrub(value, name="")` on every example value (bodies, responses, parameter examples). It redacts secrets and
  replaces personal values (emails, phone numbers, names, addresses, user ids) with placeholders, judged mostly by
  field name: inside dicts it uses the keys, so for a bare value pass its field, header or parameter name as `name`.
- `is_secret(name)` before keeping any header, cookie or query value: drop or redact it when true.
  `is_credential(name)` tells a login credential (token, API key, session) from a one-time value (CSRF).
- `strip_userinfo(url)` on every URL you keep, so no `user:password@` survives.
- `note_text(code)` when a note quotes source code or a config line: literals are blanked and secret
  `key=value` pairs masked.

Never copy a secret or an app's source literal (keys, tokens, signing salts) into a Source, a note or a fixture.

## Transforms

A transform plugin is a module or object with `INPUT` (applied to tool arguments before sending) and/or `OUTPUT`
(applied to response fields) dicts of name -> function. Each function takes `(value, tz)`, where `tz` is the plan's
`timezone` (an IANA name, or empty for the system zone), and returns the new value. It is never called with None.

```python
# mcpsmith_money/transforms.py
from mcpsmith.runtime import ApiError


def _cents(value, tz):
    try:
        return round(float(value) * 100)
    except ValueError:
        raise ApiError(f"Bad amount {value!r}: pass a number, e.g. 12.50.") from None


INPUT = {"units->cents": _cents}
OUTPUT = {"cents->units": lambda value, tz: value / 100}
```

Plans then name them like the built-ins: `ToolArg.transform: units->cents`, `Output.transforms: {price: cents->units}`.
Raise `ApiError` with a message the model can act on when an input is wrong. An output transform that raises keeps
the original value, so one odd item does not fail the call.

## Sign-in providers

For apps that sign their own requests. The entry point name is the value of `auth.provider` in a plan
(`auth: {type: signin, provider: acme}`). The object must implement `mcpsmith.runtime.signin.Provider`; the
built-in `mcpsmith/runtime/signin/xiaomi.py` is the reference. In short:

- Constants: `TITLE`, `ACCOUNT` (who should sign in, in plain words), `USER_AGENT`, `HEADERS`, `TEST_SESSION`,
  `LOGIN_MINUTES`.
- Requests: `session_of(raw)` parses the saved session (ValueError when invalid), `cookies(session)`,
  `encrypt(url, session, payload) -> (form, key)`, `decrypt(key, content)`, `error(data)`.
- Login: `start_login(client) -> (link, poll)` and `finish_login(client, poll) -> session` (async), and
  `whoami(client, session)` (async) returning who is signed in and `found` (where their data is), or None.
- Tests: `fake_reply(request, body)` answers like the real service would for `TEST_SESSION`.

The user always signs in on the service's own page through the login tool's link. A provider never asks for or
stores a password; the session goes to the OS keychain.
