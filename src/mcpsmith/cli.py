"""The `mcpsmith` command line: capture, inspect, draft, design, validate, generate, check, guide, mcp."""

from __future__ import annotations

import argparse
import dataclasses
import json
import os
import sys
from pathlib import Path

import httpx
import yaml

from . import __version__
from .model import Plan, Source, load_plan, load_source, save_plan, save_source

DEFAULT_WORKDIR = ".mcpsmith"
EXPECTED = (OSError, ValueError, RuntimeError, ImportError, httpx.HTTPError, yaml.YAMLError)
# A missing file of the pipeline: the command that writes it.
WRITTEN_BY = {"source.json": "mcpsmith inspect", "plan.yaml": "mcpsmith draft", "mcpsmith.yaml": "mcpsmith generate"}


# ---------- steps shared with the MCP server ----------


def inspect_source(source: str, workdir: Path, hosts: list[str] | None, budget: int) -> tuple[Source, Path, Path, str]:
    """Load an OpenAPI file or HAR; write source.json and digest.md into workdir."""
    from .design import digest
    from .importers import load

    src = load(source, hosts=hosts or None)
    workdir.mkdir(parents=True, exist_ok=True)
    source_path, digest_path = workdir / "source.json", workdir / "digest.md"
    save_source(src, source_path)
    text = digest(src, budget=budget)
    digest_path.write_text(text, encoding="utf-8", newline="\n")
    return src, source_path, digest_path, text


def app_android(
    apk: Path | None,
    class_name: str | None,
    base_url: str,
    workdir: Path,
    jar: str | None = None,
    source_file: Path | None = None,
) -> tuple[list[str], Source | None, str]:
    """Without a class or source file: candidate API classes. Otherwise write source.json + digest.md.

    The interface text comes from `source_file` (decompiled by any tool) or from the APK via jadx. Returns
    (candidates, source, digest); when listing candidates, the third item is sign-in advice ("" if none).
    """
    from .design import digest
    from .importers import android, jadx

    if source_file:
        java = source_file.read_text(encoding="utf-8-sig")
    elif apk is None:
        raise ValueError("give an APK path, or --source FILE with an already decompiled class")
    elif not class_name:
        classes, signer = jadx.api_classes_and_signer(apk, jar)
        return classes, None, android.signer_note(signer) if signer else ""
    else:
        java = jadx.class_source(apk, class_name, jar)
    src = android.parse_interface(java, base_url)
    if apk and (not src.endpoints or any(e.method == "" for e in src.endpoints)):
        # release builds rename the annotations: the app's own Retrofit request builder names them again
        if mapping := jadx.builder_annotations(apk, jar):
            src = android.parse_interface(java, base_url, annotations=mapping)
    if not src.endpoints:
        raise ValueError(f"no endpoints found: {src.description}")
    workdir.mkdir(parents=True, exist_ok=True)
    save_source(src, workdir / "source.json")
    text = digest(src)
    (workdir / "digest.md").write_text(text, encoding="utf-8", newline="\n")
    return [], src, text


def app_js(path: Path, base_url: str, workdir: Path) -> tuple[Source, str]:
    """Read an app's own JavaScript (directory, .asar or file) into unverified endpoints; write source.json + digest.md."""
    from .design import digest
    from .importers import electron

    notes: list[str] = []
    src = electron.parse_sources(electron.read_app(path, notes), base_url)
    src.description = "\n".join(filter(None, [*notes, src.description]))
    if not src.endpoints:
        raise ValueError(f"no endpoints found: {src.description}")
    workdir.mkdir(parents=True, exist_ok=True)
    save_source(src, workdir / "source.json")
    text = digest(src)
    (workdir / "digest.md").write_text(text, encoding="utf-8", newline="\n")
    return src, text


def desktop_steps(workdir: Path, port: int) -> list[str]:
    """The commands the developer runs themselves; mcpsmith starts and changes nothing."""
    proxy = f"http://127.0.0.1:{port}"
    return [
        "Only for a program you own or are authorized to analyze. Programs that use certificate pinning are out of scope.",
        f"Install mitmproxy yourself (https://mitmproxy.org), then run: mitmdump --set hardump={workdir / 'capture.har'} -p {port}",
        f"Point that one program at {proxy}: its own proxy setting, or start it with HTTPS_PROXY={proxy} "
        f"(Windows cmd: set HTTPS_PROXY={proxy}; PowerShell: $env:HTTPS_PROXY='{proxy}'; macOS/Linux: HTTPS_PROXY={proxy} ./program). "
        "Do not change the system-wide proxy.",
        "For HTTPS to be captured, the program must trust mitmproxy's CA certificate: follow mitmproxy's own docs, https://docs.mitmproxy.org/stable/concepts-certificates/.",
        "Use each feature you want as a tool in the program. Anything you do not use is not in the capture. "
        "Narrow to the program's API host at import with --host; that is the reliable way.",
        "Stop mitmdump with Ctrl+C (in mitmweb, quit from the UI). Do not kill the process: hardump writes the HAR only on a clean exit, "
        "and a killed mitmdump leaves no file. Then run: mcpsmith app-desktop " + str(workdir / "capture.har"),
        "Safer alternative to the hardump run above: record with "
        f"mitmdump -p {port} -w {workdir / 'flows.mitm'} (flows are written as they arrive), stop it, then convert without any proxying: "
        f"mitmdump -nr {workdir / 'flows.mitm'} --set hardump={workdir / 'capture.har'} (it exits by itself and writes the HAR).",
    ]


def app_desktop(
    har: Path | None, workdir: Path, hosts: list[str] | None, port: int, force: bool
) -> tuple[list[str], Source | None, str]:
    """Without a HAR: the steps for recording. With one: import it through `inspect_source`."""
    from .importers import load

    if har is None:
        return desktop_steps(workdir, port), None, ""
    if (workdir / "source.json").exists() and not force:
        raise ValueError(f"{workdir / 'source.json'} already exists (use --force to overwrite it)")
    if not load(str(har), hosts=hosts or None).endpoints:
        raise ValueError(
            "nothing to import: the capture has no JSON API calls"
            + (
                f" to {', '.join(hosts)}"
                if hosts
                else " (only proxy CONNECT entries, static files or non-JSON traffic)"
            )
        )
    src, _, _, text = inspect_source(str(har), workdir, hosts, 60000)
    return [], src, text


def load_workdir_source(workdir: Path) -> Source | None:
    path = workdir / "source.json"
    return load_source(path) if path.exists() else None


def split_issues(issues) -> tuple[list[str], list[str]]:
    """Format design Issues as 'LEVEL where: message' lines: (errors, warnings)."""
    errors = [f"ERROR {i.where}: {i.message}" for i in issues if i.level == "error"]
    warnings = [f"WARNING {i.where}: {i.message}" for i in issues if i.level != "error"]
    return errors, warnings


def draft_plan(workdir: Path, name: str | None, force: bool, notes: list[str] | None = None) -> Path:
    """Write <workdir>/plan.yaml; endpoints the draft left out are explained in `notes`."""
    from .design import draft

    plan_path = workdir / "plan.yaml"
    if plan_path.exists() and not force:
        raise ValueError(f"{plan_path} already exists (use --force to overwrite it)")
    source = load_source(workdir / "source.json")
    save_plan(draft(source, name, notes), plan_path)
    return plan_path


# ---------- commands ----------


def _flags(args) -> str:
    return "" if args.workdir == DEFAULT_WORKDIR else f" -w {args.workdir}"


def cmd_capture(args) -> int:
    from .capture import record

    out = record(args.url, Path(args.output), args.host or None)
    print(f"saved {out}\nnext: mcpsmith inspect {out}")
    return 0


def cmd_inspect(args) -> int:
    src, source_path, digest_path, _ = inspect_source(args.source, Path(args.workdir), args.host, args.budget)
    auth = ", ".join(f"{a.name} ({a.type})" for a in src.auth) or "none"
    print(f"{src.title}: {len(src.endpoints)} endpoints, auth: {auth}")
    print(f"wrote {source_path}\nwrote {digest_path}")
    print(f"next: mcpsmith draft{_flags(args)}   (or `mcpsmith design{_flags(args)}` to let an LLM write the plan)")
    return 0


def cmd_draft(args) -> int:
    notes: list[str] = []
    plan_path = draft_plan(Path(args.workdir), args.name, args.force, notes)
    plan = load_plan(plan_path)
    for note in notes:
        print(f"note: {note}")
    print(f"wrote {plan_path} ({len(plan.tools)} tools)")
    print(f"next: edit it (see `mcpsmith guide`), then `mcpsmith validate{_flags(args)}`")
    return 0


def cmd_design(args) -> int:
    from .design.llm import design

    workdir = Path(args.workdir)
    plan_path = workdir / "plan.yaml"
    start = load_plan(plan_path) if plan_path.exists() else None
    plan = design(load_source(workdir / "source.json"), args.goal, args.provider, args.model, start)
    save_plan(plan, plan_path)
    print(f"wrote {plan_path}: " + ", ".join(t.name for t in plan.tools))
    print(f"next: mcpsmith validate{_flags(args)}")
    return 0


def _validated(args) -> tuple[Plan, Source | None, list[str]]:
    """Load the plan, validate it, print warnings; return (plan, source, errors)."""
    from .design import validate

    workdir = Path(args.workdir)
    plan = load_plan(args.plan or workdir / "plan.yaml")
    source = load_workdir_source(workdir)
    errors, warnings = split_issues(validate(plan, source))
    for line in warnings:
        print(line)
    return plan, source, errors


def cmd_validate(args) -> int:
    _, _, errors = _validated(args)
    for line in errors:
        print(line)
    if not errors:
        print("ok")
    return 1 if errors else 0


def cmd_generate(args) -> int:
    from .codegen import generate

    plan, source, errors = _validated(args)
    if errors:
        for line in errors:
            print(line)
        print("not generated: fix the errors above", file=sys.stderr)
        return 1
    out = Path(args.output or plan.name)
    files = generate(plan, out, source, force=args.force)
    print(f"generated {len(files)} files in {out}")
    print(f"next: cd {out} && uv run pytest\n      mcpsmith check {out}")
    return 0


def cmd_check(args) -> int:
    from .check import run

    results = run(Path(args.project), writes=args.writes)
    if args.json:
        print(json.dumps([dataclasses.asdict(r) for r in results], ensure_ascii=False, indent=1))
        return 0 if all(r.ok for r in results) else 1
    for r in results:
        if r.skipped:
            print(f"SKIP {r.tool}  {r.reason}")
        elif r.ok:
            empty = f"  empty: {', '.join(r.empty_fields)}" if r.empty_fields else ""
            print(f"OK   {r.tool}  {r.chars} chars{empty}")
        else:
            print(f"FAIL {r.tool}  {r.error}")
    return 0 if all(r.ok for r in results) else 1


def cmd_eval(args) -> int:
    from . import evals

    project = Path(args.project) if args.project else None
    plan, tools, path = evals.target(project, Path(args.workdir))
    again = f"mcpsmith eval {args.project}" if project else f"mcpsmith eval{_flags(args)}"
    if args.write:
        if path.exists() and not args.force:
            raise ValueError(f"{path} already exists (use --force to overwrite it)")
        cases, notes = evals.write_cases(plan, tools, args.provider, args.model)
        evals.save_cases(cases, path)
        for note in notes:
            print(f"note: {note}")
        print(f"wrote {path} ({len(cases)} cases)\nnext: review and edit it, then `{again}`")
        return 0
    if not path.exists():
        raise ValueError(f"{path} not found; write cases with `{again} --write` first")
    rep = evals.run(plan, tools, evals.load_cases(path, tools), args.provider, args.model, args.concurrency)
    print(json.dumps(rep, ensure_ascii=False, indent=1) if args.json else evals.table(rep))
    return 1 if rep["score"] < args.min else 0


def cmd_probe(args) -> int:
    from .probe import run

    workdir = Path(args.workdir)
    source = load_workdir_source(workdir)
    if source is None:
        raise ValueError(f"{workdir / 'source.json'} not found; run `mcpsmith inspect` first")
    results = run(source, writes=args.writes, limit=args.limit)
    save_source(source, workdir / "source.json")
    for r in results:
        if r.skipped_reason:
            print(f"SKIP {r.endpoint}  {r.skipped_reason}")
        elif r.ok:
            print(f"OK   {r.endpoint}  {r.chars} chars")
        else:
            print(f"FAIL {r.endpoint}  {r.error}")
    tried = [r for r in results if not r.skipped_reason]
    return 1 if tried and not any(r.ok for r in tried) else 0


def cmd_app_android(args) -> int:
    classes, src, text = app_android(
        Path(args.apk) if args.apk else None,
        args.cls,
        args.base_url,
        Path(args.workdir),
        args.jadx_jar,
        Path(args.source) if args.source else None,
    )
    if src is None:
        print("\n".join(classes))
        if text:
            print(f"note: {text}")
        print(f"next: mcpsmith app-android {args.apk} --class NAME --base-url URL{_flags(args)}")
        return 0
    print(f"{src.title}: {len(src.endpoints)} endpoints, all unverified")
    if src.description:
        print(src.description)
    print(f"wrote {Path(args.workdir) / 'source.json'}\nnext: mcpsmith probe{_flags(args)}")
    return 0


def cmd_app_js(args) -> int:
    src, _ = app_js(Path(args.path), args.base_url, Path(args.workdir))
    print(f"{src.title}: {len(src.endpoints)} endpoints, all unverified")
    if src.description:
        print(src.description)
    print(f"wrote {Path(args.workdir) / 'source.json'}\nnext: mcpsmith probe{_flags(args)}")
    return 0


def cmd_app_desktop(args) -> int:
    steps, src, _ = app_desktop(
        Path(args.har) if args.har else None, Path(args.workdir), args.host, args.port, args.force
    )
    if src is None:
        print("\n".join(f"{i}. {s}" for i, s in enumerate(steps, 1)))
        return 0
    print(f"{src.title}: {len(src.endpoints)} endpoints, from the recorded traffic")
    print(f"wrote {Path(args.workdir) / 'source.json'}\nnext: mcpsmith draft{_flags(args)}")
    return 0


def cmd_guide(args) -> int:
    from .design import guide

    print(guide())
    return 0


def cmd_schema(args) -> int:
    print(json.dumps(Plan.model_json_schema(), ensure_ascii=False, indent=1))
    return 0


def cmd_secret(args) -> int:
    from keyring.errors import KeyringError

    try:
        return _secret(args)
    except KeyringError as e:
        raise RuntimeError(
            f"no usable keychain ({e}); set the {args.name or 'secret'} environment variable instead"
        ) from e


def _secret(args) -> int:
    import getpass

    from . import secrets

    if args.action == "set":
        value = getpass.getpass(f"{args.name} for {args.connection} (hidden): ")
        if not value:
            raise ValueError("empty value; nothing stored")
        secrets.set(args.connection, args.name, value, once=args.once)
        print(f"stored {args.name} for {args.connection}" + (" (one use)" if args.once else ""))
    elif args.action == "delete":
        secrets.delete(args.connection, args.name)
        print(f"deleted {args.name} for {args.connection}")
    else:
        print(", ".join(secrets.names(args.connection)) or "(none)")
    return 0


def cmd_browser(args) -> int:
    from . import browser

    info = browser.launch(args.port, no_proxy=args.no_proxy)
    print(
        f"Chrome on port {info['port']} (proxy: {info['proxy'] or 'none'}), profile {browser.home() / 'chrome-profile'}"
    )
    print("next: open the site in this window and log in")
    return 0


def cmd_connections(args) -> int:
    from mcp.server import MCPServer

    from .hub import Hub

    hub = Hub(MCPServer("mcpsmith"))
    hub.load_all()
    for c in hub.connections():
        print(
            f"{c['name']}: "
            + (f"{len(c['tools'])} tools, auth {c['auth'] or 'none'}" if c["ok"] else f"NOT LOADED ({c['warning']})")
        )
    if not hub.connections():
        print("(none)")
    return 0


def cmd_setup(args) -> int:
    from .setup.run import cmd

    return cmd(args)


def cmd_install(args) -> int:
    from .setup.run import install_cmd

    return install_cmd(args)


def cmd_mcp(args) -> int:
    from .server import main as serve

    # runtime.run reads these; argv may not be sys.argv when main() is called directly
    for env, value in (("MCP_TRANSPORT", args.transport), ("MCP_HOST", args.host), ("MCP_PORT", args.port)):
        if value is not None:
            os.environ[env] = str(value)
    serve()
    return 0


# ---------- parser ----------


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="mcpsmith", description="Turn an OpenAPI file or a recorded website session into a curated MCP server."
    )
    p.add_argument("--version", action="version", version=f"mcpsmith {__version__}")
    sub = p.add_subparsers(dest="command", required=True, metavar="command")

    def command(name, fn, help, workdir=True):
        sp = sub.add_parser(name, help=help, description=help)
        sp.set_defaults(fn=fn)
        if workdir:
            sp.add_argument(
                "-w", "--workdir", default=DEFAULT_WORKDIR, help=f"working directory (default {DEFAULT_WORKDIR})"
            )
        return sp

    sp = command("capture", cmd_capture, "record a browsing session of a website to a HAR file", workdir=False)
    sp.add_argument("url")
    sp.add_argument("-o", "--output", default="site.har")
    sp.add_argument("--host", action="append", help="keep only traffic to this host (repeatable)")

    sp = command(
        "inspect",
        cmd_inspect,
        "read an OpenAPI file, HAR, Postman collection or cURL commands; write source.json and digest.md",
    )
    sp.add_argument("source", help="OpenAPI/Swagger file, HAR or Postman collection (path or URL), or a cURL file")
    sp.add_argument("--host", action="append", help="keep only traffic to this host (HAR; repeatable)")
    sp.add_argument("--budget", type=int, default=60000, help="digest size limit in characters (default 60000)")

    sp = command("draft", cmd_draft, "write a heuristic starting plan.yaml from source.json")
    sp.add_argument("--name", help="project name, e.g. petstore-mcp")
    sp.add_argument("--force", action="store_true", help="overwrite an existing plan.yaml")

    sp = command("design", cmd_design, "let an LLM (API key needed) design or refine plan.yaml")
    sp.add_argument("--goal", default="", help="what the server is for")
    sp.add_argument("--provider", choices=["anthropic", "openai", "openrouter", "ollama"], default="anthropic")
    sp.add_argument("--model")

    sp = command("validate", cmd_validate, "check plan.yaml; exit 1 on errors")
    sp.add_argument("--plan", help="plan file (default <workdir>/plan.yaml)")

    sp = command("generate", cmd_generate, "validate plan.yaml, then write the MCP server project")
    sp.add_argument("--plan", help="plan file (default <workdir>/plan.yaml)")
    sp.add_argument("-o", "--output", help="output directory (default ./<plan name>)")
    sp.add_argument("--force", action="store_true", help="write into an existing directory")

    sp = command("check", cmd_check, "call every tool of a generated project live; exit 1 on failures", workdir=False)
    sp.add_argument("project")
    sp.add_argument("--json", action="store_true")
    sp.add_argument("--writes", action="store_true", help="also call tools that change data (read_only: false)")

    sp = command(
        "eval",
        cmd_eval,
        "score how well an LLM (API key needed) picks the right tool and arguments for the questions in evals.yaml",
    )
    sp.add_argument("project", nargs="?", help="a generated project (default: the plan in the working directory)")
    sp.add_argument("--write", action="store_true", help="let the LLM write evals.yaml from the tools and examples")
    sp.add_argument("--force", action="store_true", help="with --write: overwrite an existing evals.yaml")
    sp.add_argument("--provider", choices=["anthropic", "openai", "openrouter", "ollama"], default="anthropic")
    sp.add_argument("--model")
    sp.add_argument("--json", action="store_true", help="print the full report as JSON (for CI)")
    sp.add_argument("--min", type=float, default=0, help="exit 1 when the score (0 to 1) is below this (default 0)")
    sp.add_argument("--concurrency", type=int, default=1, help="requests at once after the first (default 1)")

    sp = command(
        "probe", cmd_probe, "call unverified GET endpoints live to capture example responses; exit 1 if all fail"
    )
    sp.add_argument("--limit", type=int, default=20, help="most endpoints to probe (default 20)")
    sp.add_argument("--writes", action="store_true", help="also probe non-GET endpoints (may change data)")

    sp = command(
        "app-android",
        cmd_app_android,
        "read an Android app's HTTP interface (via REA's JADX jar) into unverified endpoints; your own or an authorized app",
    )
    sp.add_argument("apk", nargs="?", help="path of the APK (never downloaded for you)")
    sp.add_argument("--class", dest="cls", help="decompiled Retrofit interface class; omit to list candidates")
    sp.add_argument("--source", help="a class already decompiled by any tool (.java file); skips the jar")
    sp.add_argument("--jadx-jar", help="REA's JADX MCP jar (default: `jadx` on PATH, else $REA_JADX_MCP_JAR)")
    sp.add_argument("--base-url", default="", help="the app's API base URL, e.g. https://api.example.com")

    sp = command(
        "app-js",
        cmd_app_js,
        "read an Electron/JavaScript app's own HTTP calls (directory, .asar, .js or .map) into unverified endpoints; your own or an authorized app",
    )
    sp.add_argument("path", help="app directory, app.asar, or one .js/.ts/.map file (never downloaded for you)")
    sp.add_argument("--base-url", default="", help="the app's API base URL, e.g. https://api.example.com")

    sp = command(
        "app-desktop",
        cmd_app_desktop,
        "print the steps to record a desktop program's traffic with your own mitmproxy, or import the HAR it wrote; your own or an authorized program",
    )
    sp.add_argument("har", nargs="?", help="the HAR mitmdump wrote; omit to print the steps")
    sp.add_argument("--port", type=int, default=8080, help="proxy port in the printed steps (default 8080)")
    sp.add_argument("--host", action="append", help="keep only traffic to this host (repeatable)")
    sp.add_argument("--force", action="store_true", help="overwrite an existing source.json")

    sp = command("secret", cmd_secret, "store a connection's credentials in the OS keychain", workdir=False)
    sp.add_argument("action", choices=["set", "list", "delete"])
    sp.add_argument("connection")
    sp.add_argument("name", nargs="?", help="secret name, e.g. SHOP_MCP_TOKEN (set, delete)")
    sp.add_argument("--once", action="store_true", help="forget the value after its first use (set)")

    sp = command("browser", cmd_browser, "start (or reuse) the shared Chrome you log in with", workdir=False)
    sp.add_argument("--port", type=int, default=9222)
    sp.add_argument("--no-proxy", action="store_true", help="do not pass HTTPS_PROXY on to Chrome")

    sp = command("setup", cmd_setup, "register mcpsmith and Chrome DevTools MCP with your coding agents", workdir=False)
    sp.add_argument("--agents", help="comma-separated: claude,codex,cursor,gemini (default: the ones found)")
    sp.add_argument("--rea", action="store_true", help="also install REA (rea-agents; adds ~200 tools to each agent)")
    sp.add_argument("--no-rea", action="store_true", help=argparse.SUPPRESS)  # old default; now a no-op
    sp.add_argument("--no-skills", action="store_true", help="skip copying the skills")
    sp.add_argument("--dry-run", action="store_true", help="print the plan, write nothing")
    sp.add_argument("--yes", action="store_true", help="do not ask for confirmation")
    sp.add_argument("--remove", action="store_true", help="undo what setup did")

    sp = command(
        "install", cmd_install, "register a project made by `mcpsmith generate` with your coding agents", workdir=False
    )
    sp.add_argument("project", help="the generated project directory (has mcpsmith.yaml)")
    sp.add_argument("--agents", help="comma-separated: claude,codex,cursor,gemini (default: the ones found)")
    sp.add_argument("--pypi", action="store_true", help="run the released package (uvx name@version), not the folder")
    sp.add_argument("--force", action="store_true", help="replace an entry of the same name that runs something else")
    sp.add_argument("--dry-run", action="store_true", help="print the plan, write nothing")
    sp.add_argument("--yes", action="store_true", help="do not ask for confirmation")
    sp.add_argument("--remove", action="store_true", help="remove the project's entry")

    command("connections", cmd_connections, "list the connections saved for the mcpsmith server", workdir=False)
    command("guide", cmd_guide, "print the tool-design rules for plan.yaml", workdir=False)
    command("schema", cmd_schema, "print the JSON Schema of plan.yaml (for editor completion)", workdir=False)
    sp = command("mcp", cmd_mcp, "run the mcpsmith MCP server (stdio, or Streamable HTTP at /mcp)", workdir=False)
    sp.add_argument("--transport", choices=["stdio", "http"], help="default stdio, or $MCP_TRANSPORT")
    sp.add_argument("--host", help="HTTP bind address (default 127.0.0.1, or $MCP_HOST); there is no auth")
    sp.add_argument("--port", type=int, help="HTTP port (default 8000, or $MCP_PORT)")
    return p


def main(argv: list[str] | None = None) -> int:
    # Digests contain Persian text; the Windows console default would raise on it.
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass
    p = build_parser()
    args = p.parse_args(argv)
    if args.command == "secret" and (args.action != "list") != bool(args.name):
        p.error("secret set/delete need a NAME; secret list takes none")
    try:
        return args.fn(args)
    except EXPECTED as e:
        first = isinstance(e, FileNotFoundError) and WRITTEN_BY.get(Path(e.filename or "").name)
        print(f"mcpsmith: error: {e}" + (f"; run `{first}` first" if first else ""), file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
