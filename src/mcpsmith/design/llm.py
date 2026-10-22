"""Design a Plan with an LLM over plain HTTP: Anthropic Messages API or any OpenAI-compatible chat API."""

from __future__ import annotations

import json
import logging
import os
import re
from typing import Any

import httpx
import yaml
from pydantic import ValidationError

from ..model import Plan, Source
from . import digest, draft, guide, validate

log = logging.getLogger(__name__)

# provider -> (base URL, API key env var, default model)
PROVIDERS: dict[str, tuple[str, str | None, str]] = {
    "anthropic": ("https://api.anthropic.com/v1", "ANTHROPIC_API_KEY", "claude-sonnet-5-5"),
    "openai": ("https://api.openai.com/v1", "OPENAI_API_KEY", "gpt-5-mini"),
    "openrouter": ("https://openrouter.ai/api/v1", "OPENROUTER_API_KEY", "anthropic/claude-sonnet-5.5"),
    "ollama": ("http://127.0.0.1:11434/v1", None, "qwen3"),
}
MAX_TOKENS = 32_000
_transport: httpx.BaseTransport | None = None  # tests put an httpx.MockTransport here

OUTPUT_RULES = """\
## Your answer

Reply with exactly one JSON object: the complete Plan, matching the JSON schema above. No prose, no markdown,
no code fences. Use only endpoint ids from the digest. Every tool needs `example` arguments with real values
from the digest. When asked to fix problems, reply with the whole corrected plan again."""


def system_prompt() -> str:
    """Static part of the prompt (cached): design rules, Plan schema, answer format."""
    schema = json.dumps(Plan.model_json_schema(), ensure_ascii=False, separators=(",", ":"))
    return f"{guide()}\n\n## Plan JSON schema\n\n{schema}\n\n{OUTPUT_RULES}"


def user_prompt(source: Source, start: Plan, goal: str) -> str:
    plan = yaml.safe_dump(
        start.model_dump(mode="json", exclude_defaults=True), allow_unicode=True, sort_keys=False, width=120
    )
    text = (
        f"## API digest\n\n{digest(source)}\n\n## Starting plan (heuristic draft, improve it)\n\n```yaml\n{plan}```\n"
    )
    return text + (f"\n## Goal\n\n{goal}\n" if goal else "")


def extract_json(text: str) -> Any:
    """First JSON object in a reply, ignoring ``` fences and surrounding prose."""
    text = re.sub(r"```(?:json)?", "", text)
    decoder = json.JSONDecoder()
    for m in re.finditer(r"\{", text):
        try:
            return decoder.raw_decode(text, m.start())[0]
        except json.JSONDecodeError:
            continue
    raise ValueError("the reply contained no JSON object")


def check(text: str, source: Source) -> tuple[Plan | None, list[str]]:
    """Parse and validate a reply; return the plan and the errors to send back."""
    try:
        plan = Plan.model_validate(extract_json(text))
    except ValidationError as e:
        return None, [f"{'.'.join(str(x) for x in err['loc'])}: {err['msg']}" for err in e.errors()]
    except ValueError as e:
        return None, [str(e)]
    return plan, [f"{i.where}: {i.message}" for i in validate(plan, source) if i.level == "error"]


def _post(client: httpx.Client, url: str, headers: dict[str, str], body: dict[str, Any]) -> dict[str, Any]:
    r = client.post(url, headers=headers, json=body)
    if r.status_code >= 400:
        raise RuntimeError(f"{url} returned HTTP {r.status_code}: {r.text[:500]}")
    return r.json()


def _anthropic(client, base, key, model, system, messages, max_tokens=MAX_TOKENS) -> tuple[str, Any, dict[str, int]]:
    data = _post(
        client,
        f"{base}/messages",
        {"x-api-key": key or "", "anthropic-version": "2023-06-01"},
        {
            "model": model,
            "max_tokens": max_tokens,
            "system": [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
            "messages": messages,
        },
    )
    if data.get("stop_reason") in ("refusal", "max_tokens"):
        raise RuntimeError(f"{model} stopped with stop_reason={data['stop_reason']}; try another model or a goal.")
    u = data.get("usage") or {}
    log.info(
        "anthropic usage: input=%s output=%s cache_read=%s cache_creation=%s",
        u.get("input_tokens"), u.get("output_tokens"),
        u.get("cache_read_input_tokens"), u.get("cache_creation_input_tokens"),
    )  # fmt: skip
    usage = {
        "input": u.get("input_tokens") or 0,
        "output": u.get("output_tokens") or 0,
        "cache_read": u.get("cache_read_input_tokens") or 0,
        "cache_write": u.get("cache_creation_input_tokens") or 0,
    }
    text = "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text")
    # Pass the whole content back (thinking blocks included) so later turns stay valid.
    return text, data.get("content", []), usage


def _openai(client, base, key, model, system, messages, provider) -> tuple[str, Any, dict[str, int]]:
    sys_content: Any = system
    if provider == "openrouter" and model.startswith("anthropic/"):
        sys_content = [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}]
    body: dict[str, Any] = {"model": model, "messages": [{"role": "system", "content": sys_content}, *messages]}
    if provider in ("openai", "ollama"):  # every caller wants one JSON object; JSON mode keeps small models to it
        body["response_format"] = {"type": "json_object"}
    data = _post(client, f"{base}/chat/completions", {"Authorization": f"Bearer {key}"} if key else {}, body)
    u = data.get("usage") or {}
    details = u.get("prompt_tokens_details") or {}
    log.info(
        "%s usage: prompt=%s completion=%s cached=%s cache_write=%s",
        provider, u.get("prompt_tokens"), u.get("completion_tokens"),
        details.get("cached_tokens"), details.get("cache_write_tokens"),
    )  # fmt: skip
    usage = {
        "input": u.get("prompt_tokens") or 0,
        "output": u.get("completion_tokens") or 0,
        "cache_read": details.get("cached_tokens") or 0,
        "cache_write": details.get("cache_write_tokens") or 0,
    }
    text = data["choices"][0]["message"].get("content") or ""
    return text, text, usage


def _resolve(provider: str, model: str | None) -> tuple[str, str | None, str]:
    """(base URL, API key, model) for a provider, from the environment."""
    if provider not in PROVIDERS:
        raise ValueError(f"unknown provider {provider!r}; use one of {', '.join(PROVIDERS)}")
    base, key_env, default_model = PROVIDERS[provider]
    key = os.environ.get(key_env) if key_env else None
    if key_env and not key:
        raise ValueError(f"set {key_env} to use provider {provider!r}")
    model = model or os.environ.get("MCPSMITH_MODEL") or default_model
    return os.environ.get("MCPSMITH_BASE_URL", base).rstrip("/"), key, model


def ask(
    provider: str, system: str, user: str, model: str | None = None, max_tokens: int = 4096
) -> tuple[str, dict[str, int]]:
    """One request: `system` is the cached static prefix, `user` the only dynamic part.

    Returns the reply text and the token usage (input, output, cache_read, cache_write; 0 when not reported)."""
    base, key, model = _resolve(provider, model)
    messages = [{"role": "user", "content": user}]
    with httpx.Client(transport=_transport, timeout=600) as client:
        if provider == "anthropic":
            text, _, usage = _anthropic(client, base, key, model, system, messages, max_tokens)
        else:
            text, _, usage = _openai(client, base, key, model, system, messages, provider)
    return text, usage


def design(
    source: Source,
    goal: str = "",
    provider: str = "anthropic",
    model: str | None = None,
    start: Plan | None = None,
    max_rounds: int = 3,
) -> Plan:
    """Ask an LLM for a Plan; send validation errors back until it is clean or max_rounds is spent."""
    base, key, model = _resolve(provider, model)
    system = system_prompt()
    messages: list[dict[str, Any]] = [{"role": "user", "content": user_prompt(source, start or draft(source), goal)}]
    errors: list[str] = []
    with httpx.Client(transport=_transport, timeout=600) as client:
        for round_ in range(1, max_rounds + 1):
            if provider == "anthropic":
                text, content, _ = _anthropic(client, base, key, model, system, messages)
            else:
                text, content, _ = _openai(client, base, key, model, system, messages, provider)
            plan, errors = check(text, source)
            log.info("design round %d: %d errors", round_, len(errors))
            if plan is not None and not errors:
                return plan
            messages.append({"role": "assistant", "content": content})
            problems = "\n".join(f"- {e}" for e in errors)
            messages.append(
                {
                    "role": "user",
                    "content": f"The plan has these errors:\n{problems}\n\nFix them and reply with the whole corrected plan as one JSON object.",
                }
            )
    raise ValueError(f"plan still invalid after {max_rounds} rounds:\n" + "\n".join(f"- {e}" for e in errors))
