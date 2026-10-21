"""The `mcpsmith` command line: capture, inspect, draft, design, validate, generate, check, guide, mcp."""

from __future__ import annotations

import argparse
import dataclasses
import json
import sys
from pathlib import Path

import httpx
import yaml

from .model import Plan, Source, load_plan, load_source, save_plan, save_source

DEFAULT_WORKDIR = ".mcpsmith"
EXPECTED = (OSError, ValueError, RuntimeError, ImportError, httpx.HTTPError, yaml.YAMLError)


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

    The interface text comes from `source_file` (decompiled by any tool) or from the APK via the JADX jar. Returns
    (candidates, source, digest).
    """
    from .design import digest
    from .importers import android, jadx

    if source_file:
        java = source_file.read_text(encoding="utf-8-sig")
    elif apk is None:
        raise ValueError("give an APK path, or --source FILE with an already decompiled class")
    elif not class_name:
        return jadx.find_api_classes(apk, jar), None, ""
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


def load_workdir_source(workdir: Path) -> Source | None:
    path = workdir / "source.json"
    return load_source(path) if path.exists() else None


def split_issues(issues) -> tuple[list[str], list[str]]:
    """Format design Issues as 'LEVEL where: message' lines: (errors, warnings)."""
    errors = [f"ERROR {i.where}: {i.message}" for i in issues if i.level == "error"]
    warnings = [f"WARNING {i.where}: {i.message}" for i in issues if i.level != "error"]
    return errors, warnings


def draft_plan(workdir: Path, name: str | None, force: bool) -> Path:
    from .design import draft

    plan_path = workdir / "plan.yaml"
    if plan_path.exists() and not force:
        raise ValueError(f"{plan_path} already exists (use --force to overwrite it)")
    source = load_source(workdir / "source.json")
    save_plan(draft(source, name), plan_path)
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
    plan_path = draft_plan(Path(args.workdir), args.name, args.force)
    plan = load_plan(plan_path)
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
            print(f"SKIP {r.tool}  changes data on the live API; add --writes to call it")
        elif r.ok:
            empty = f"  empty: {', '.join(r.empty_fields)}" if r.empty_fields else ""
            print(f"OK   {r.tool}  {r.chars} chars{empty}")
        else:
            print(f"FAIL {r.tool}  {r.error}")
    return 0 if all(r.ok for r in results) else 1


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
    classes, src, _ = app_android(
        Path(args.apk) if args.apk else None,
        args.cls,
        args.base_url,
        Path(args.workdir),
        args.jadx_jar,
        Path(args.source) if args.source else None,
    )
    if src is None:
        print("\n".join(classes) or "(no candidate classes found)")
        print(f"next: mcpsmith app-android {args.apk} --class NAME --base-url URL{_flags(args)}")
        return 0
    print(f"{src.title}: {len(src.endpoints)} endpoints, all unverified")
    if src.description:
        print(src.description)
    print(f"wrote {Path(args.workdir) / 'source.json'}\nnext: mcpsmith probe{_flags(args)}")
    return 0


def cmd_guide(args) -> int:
    from .design import guide

    print(guide())
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


def cmd_mcp(args) -> int:
    from .server import main as serve

    serve()
    return 0


# ---------- parser ----------


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="mcpsmith", description="Turn an OpenAPI file or a recorded website session into a curated MCP server."
    )
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

    sp = command("inspect", cmd_inspect, "read an OpenAPI file or HAR (path or URL); write source.json and digest.md")
    sp.add_argument("source", help="OpenAPI/Swagger file or HAR, path or URL")
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
    sp.add_argument("--jadx-jar", help="REA's JADX MCP jar (default: $REA_JADX_MCP_JAR, set by `npx rea-agents setup`)")
    sp.add_argument("--base-url", default="", help="the app's API base URL, e.g. https://api.example.com")

    sp = command("secret", cmd_secret, "store a connection's credentials in the OS keychain", workdir=False)
    sp.add_argument("action", choices=["set", "list", "delete"])
    sp.add_argument("connection")
    sp.add_argument("name", nargs="?", help="secret name, e.g. SHOP_MCP_TOKEN (set, delete)")
    sp.add_argument("--once", action="store_true", help="forget the value after its first use (set)")

    sp = command("browser", cmd_browser, "start (or reuse) the shared Chrome you log in with", workdir=False)
    sp.add_argument("--port", type=int, default=9222)
    sp.add_argument("--no-proxy", action="store_true", help="do not pass HTTPS_PROXY on to Chrome")

    sp = command(
        "setup", cmd_setup, "register mcpsmith, Chrome DevTools MCP and REA with your coding agents", workdir=False
    )
    sp.add_argument("--agents", help="comma-separated: claude,codex,cursor,gemini (default: the ones found)")
    sp.add_argument("--no-rea", action="store_true", help="skip the REA install")
    sp.add_argument("--no-skills", action="store_true", help="skip copying the skills")
    sp.add_argument("--dry-run", action="store_true", help="print the plan, write nothing")
    sp.add_argument("--yes", action="store_true", help="do not ask for confirmation")
    sp.add_argument("--remove", action="store_true", help="undo what setup did")

    command("connections", cmd_connections, "list the connections saved for the mcpsmith server", workdir=False)
    command("guide", cmd_guide, "print the tool-design rules for plan.yaml", workdir=False)
    command("mcp", cmd_mcp, "run the mcpsmith MCP server on stdio", workdir=False)
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
        print(f"mcpsmith: error: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
