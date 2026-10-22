"""`toolsmelt eval`: does an LLM pick the right tool, with the right arguments, from a plan's tool list?

Cases live in `evals.yaml` (next to plan.yaml, or in a generated project's root): a list of
`{question, tool, args}`, where `tool: null` means none of the tools fits and `args` is the subset that must match.
"""

from __future__ import annotations

import dataclasses
import json
import logging
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import yaml
from mcp.server import MCPServer
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .design import llm
from .model import Plan, load_plan, load_source, project_plan
from .runtime.interpret import make_tool
from .runtime.server import _strip_titles, compact

FILE = "evals.yaml"
NONE = "(none)"  # the expected or picked "tool" of a case no tool fits
NEGATIVES = 3

ASK_RULES = """\
You are an AI assistant connected to one MCP server; its instructions and tools follow. For the user's message, \
pick the single tool you would call first and the arguments you would pass, or null when none of these tools \
helps. Do not answer the message itself.
Reply with exactly one JSON object and nothing else: {"tool": "<tool name>" or null, "args": {"<argument>": <value>}}"""

WRITE_RULES = f"""\
Write evaluation cases for the MCP server below: messages a real user would send to an assistant that has these \
tools, each with the tool that should answer it.
- For every tool, one or two short, natural questions this tool answers. When the tool has an `example`, every \
example value must appear in the question in words a person would use, and `args` is exactly the example. Do not \
name the tool or its arguments, and vary the wording.
- Also {NEGATIVES} questions on the same subject that none of these tools can answer: `tool` null, `args` {{}}.
- Write the questions in the language the server's users would use.
Reply with exactly one JSON object and nothing else: \
{{"cases": [{{"question": "...", "tool": "<tool name>" or null, "args": {{...}}}}]}}"""


class Case(BaseModel):
    model_config = ConfigDict(extra="forbid")

    question: str = Field(min_length=1)
    tool: str | None  # required: a forgotten `tool:` must not turn a case into a negative
    args: dict[str, Any] = {}


@dataclasses.dataclass
class Result:
    question: str
    expected: str | None
    args: dict[str, Any]
    picked: str | None
    got: dict[str, Any]
    tool_ok: bool
    args_ok: bool
    error: str = ""


# ---------- the tool list ----------


def _listed(mcp: MCPServer) -> list[dict[str, Any]]:
    # shortcut: reads the SDK's tool registry like runtime.server does; recheck on an mcp upgrade.
    tools = []
    for t in mcp._tool_manager.list_tools():
        _strip_titles(t.parameters)
        tools.append({"name": t.name, "description": t.description, "input_schema": t.parameters})
    return tools


def target(project: Path | None, workdir: Path) -> tuple[Plan, list[dict[str, Any]], Path]:
    """(plan, the tool list its server exposes, the evals.yaml path) for a generated project or a workdir."""
    if project:
        from .check import _import_server

        plan = load_plan(project_plan(project))
        tools = _listed(_import_server(project / "src", plan.package).mcp)
        path = project / FILE
    else:
        plan = load_plan(workdir / "plan.yaml")
        source = load_source(workdir / "source.json") if (workdir / "source.json").exists() else None
        mcp = MCPServer(plan.name)
        for t in plan.tools:  # registered as the hub does; nothing is called, so no Api is needed
            mcp.add_tool(compact(make_tool(t, plan, source, None)), title=t.title, structured_output=False)
        tools = _listed(mcp)
        path = workdir / FILE
    # MCPServer turned on INFO logging: keep per-request lines out of the report
    for name in ("httpx", llm.__name__):
        logging.getLogger(name).setLevel(logging.WARNING)
    return plan, tools, path


def _prefix(plan: Plan, tools: list[dict[str, Any]], rules: str) -> str:
    listing = json.dumps(tools, ensure_ascii=False, indent=1)
    return f"{rules}\n\n## Server instructions\n\n{plan.instructions}\n\n## Tools\n\n{listing}"


def system_prompt(plan: Plan, tools: list[dict[str, Any]]) -> str:
    """The static, cached prefix of every eval request: answer rules, server instructions, tool list."""
    return _prefix(plan, tools, ASK_RULES)


# ---------- cases ----------


def _problems(cases: list[Case], tools: list[dict[str, Any]]) -> list[str]:
    known = {t["name"]: set(t["input_schema"].get("properties", {})) for t in tools}
    out = []
    for i, c in enumerate(cases, 1):
        if c.tool is not None and c.tool not in known:
            out.append(f"case {i}: unknown tool {c.tool!r}")
        elif c.tool is not None and (extra := sorted(set(c.args) - known[c.tool])):
            out.append(f"case {i}: {c.tool} has no argument(s) {extra}")
        elif c.tool is None and c.args:
            out.append(f"case {i}: a case with tool null takes no args")
    return out


def load_cases(path: Path, tools: list[dict[str, Any]]) -> list[Case]:
    """Read and check evals.yaml; its text may come from an LLM that read third-party pages: data only."""
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        data = json.loads(json.dumps(data, default=str))  # YAML dates become strings
        if not isinstance(data, list) or not data:
            raise ValueError("expected a non-empty list of {question, tool, args}")
        cases = [Case.model_validate(c) for c in data]
    except (yaml.YAMLError, ValueError, TypeError) as e:  # ValidationError is a ValueError
        raise ValueError(f"{path}: {e}") from e
    if problems := _problems(cases, tools):
        raise ValueError(f"{path}:\n" + "\n".join(f"- {p}" for p in problems))
    return cases


def write_cases(
    plan: Plan, tools: list[dict[str, Any]], provider: str, model: str | None = None
) -> tuple[list[Case], list[str]]:
    """Ask the LLM for cases (one request); expected args come from each tool's `example`. Returns (cases, notes)."""
    examples = {t.name: t.example for t in plan.tools}
    listing = [t | {"example": examples.get(t["name"], {})} for t in tools]
    text, _ = llm.ask(provider, _prefix(plan, listing, WRITE_RULES), "Write the cases now.", model, max_tokens=8000)
    try:
        raw = json.loads(text)  # a bare list: extract_json would return only its first object
    except ValueError:
        raw = llm.extract_json(text)
    raw = raw.get("cases") if isinstance(raw, dict) else raw  # small models often send the bare list
    if not isinstance(raw, list):
        raise ValueError(f'the reply has no "cases" list (it began: {text.strip()[:120]!r})')
    cases, notes = [], []
    for item in raw:
        try:
            case = Case.model_validate(item)
        except ValidationError as e:
            notes.append(f"dropped a malformed case: {e.errors()[0]['msg']}")
            continue
        if case.tool is not None and examples.get(case.tool):
            case.args = dict(examples[case.tool])
        if problems := _problems([case], tools):
            notes.append(f"dropped {case.question!r}: {problems[0].partition(': ')[2]}")
            continue
        cases.append(case)
    if not cases:
        raise ValueError("the reply had no usable cases")
    return cases, notes


def save_cases(cases: list[Case], path: Path) -> None:
    head = "# toolsmelt eval cases: question, expected tool (null: none fits) and the args that must match.\n"
    body = yaml.safe_dump([c.model_dump() for c in cases], allow_unicode=True, sort_keys=False, width=120)
    path.write_text(head + body, encoding="utf-8", newline="\n")


# ---------- running and scoring ----------


def same(a: Any, b: Any) -> bool:
    """Loose equality: numbers by value, strings without case or surrounding spaces, lists and dicts item by item."""
    if isinstance(a, dict) and isinstance(b, dict):
        return a.keys() == b.keys() and all(same(a[k], b[k]) for k in a)
    if isinstance(a, list) and isinstance(b, list):
        return len(a) == len(b) and all(map(same, a, b))
    if isinstance(a, (dict, list)) or isinstance(b, (dict, list)):
        return False
    x, y = str(a).strip().casefold(), str(b).strip().casefold()
    if x == y:
        return True
    try:
        return float(x) == float(y)
    except ValueError:
        return False


def score(case: Case, reply: str) -> Result:
    """Compare one reply with its case: the tool must match, and every expected arg loosely."""
    picked, got, error = None, {}, ""
    try:
        answer = llm.extract_json(reply)
        picked = answer.get("tool")
        got = answer.get("args") if isinstance(answer.get("args"), dict) else {}
    except ValueError as e:
        error = str(e)
    if not isinstance(picked, str) or picked.strip().casefold() in ("", "null", "none"):
        picked = None
    tool_ok = not error and picked == case.tool
    args_ok = tool_ok and all(same(got.get(k), v) for k, v in case.args.items())
    return Result(case.question, case.tool, case.args, picked, got, tool_ok, args_ok, error)


def run(
    plan: Plan,
    tools: list[dict[str, Any]],
    cases: list[Case],
    provider: str,
    model: str | None = None,
    concurrency: int = 1,
) -> dict[str, Any]:
    """One request per case, the tool list as the cached prefix; the first case runs alone to warm the cache."""
    system = system_prompt(plan, tools)

    def one(case: Case) -> tuple[Result, dict[str, int]]:
        text, usage = llm.ask(provider, system, case.question, model, max_tokens=1024)
        return score(case, text), usage

    done = [one(cases[0])]
    with ThreadPoolExecutor(max_workers=max(1, concurrency)) as pool:
        done += pool.map(one, cases[1:])
    usage: Counter[str] = Counter()
    for _, u in done:
        usage.update(u)
    return report([r for r, _ in done], dict(usage))


def report(results: list[Result], usage: dict[str, int]) -> dict[str, Any]:
    with_args = [r for r in results if r.tool_ok and r.expected is not None]
    passed = sum(r.args_ok for r in results)
    tools: dict[str, dict[str, Any]] = {}
    for r in results:
        row = tools.setdefault(r.expected or NONE, {"cases": 0, "tool_ok": 0, "args_ok": 0, "picked_instead": {}})
        row["cases"] += 1
        row["tool_ok"] += r.tool_ok
        row["args_ok"] += r.args_ok
        if not r.tool_ok:
            other = "(bad reply)" if r.error else r.picked or NONE
            row["picked_instead"][other] = row["picked_instead"].get(other, 0) + 1
    return {
        "score": round(passed / len(results), 3),
        "cases": len(results),
        "passed": passed,
        "tool_ok": sum(r.tool_ok for r in results),
        "args_checked": len(with_args),
        "args_ok": sum(r.args_ok for r in with_args),
        "tools": tools,
        "usage": usage,
        "results": [dataclasses.asdict(r) for r in results],
    }


def table(rep: dict[str, Any]) -> str:
    """The report for a terminal: failed cases, one row per expected tool, then the totals."""

    def call(tool: str | None, args: dict[str, Any]) -> str:
        return f"{tool} {json.dumps(args, ensure_ascii=False)}" if tool else "no tool"

    lines = []
    for r in rep["results"]:
        if not r["args_ok"]:
            got = f"bad reply ({r['error']})" if r["error"] else call(r["picked"], r["got"])
            lines += [f"FAIL  {r['question']}", f"      expected {call(r['expected'], r['args'])}, got {got}"]
    width = max(len("tool"), *(len(n) for n in rep["tools"]))
    lines += ["", f"{'tool':<{width}}  cases  tool ok  args ok  picked instead"]
    for name, row in rep["tools"].items():
        args = "-" if name == NONE else f"{row['args_ok']}/{row['tool_ok']}"
        instead = ", ".join(f"{k} x{v}" for k, v in row["picked_instead"].items())
        lines.append(f"{name:<{width}}  {row['cases']:>5}  {row['tool_ok']:>4}/{row['cases']:<2}  {args:>7}  {instead}")
    u = rep["usage"]
    lines.append(
        f"\nscore {rep['score']:.2f} ({rep['passed']}/{rep['cases']} cases right): tool {rep['tool_ok']}/{rep['cases']}, "
        f"args {rep['args_ok']}/{rep['args_checked']}; tokens: input {u.get('input', 0)}, "
        f"cache read {u.get('cache_read', 0)}, cache write {u.get('cache_write', 0)}, output {u.get('output', 0)}"
    )
    return "\n".join(lines).lstrip("\n")
