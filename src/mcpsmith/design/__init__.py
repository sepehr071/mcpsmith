"""Design stage: turn a Source into a Plan (digest for the LLM, heuristic draft, validation, design rules)."""

from __future__ import annotations

from functools import cache
from pathlib import Path

from ._digest import digest
from ._draft import draft
from ._validate import Issue, validate

__all__ = ["Issue", "digest", "draft", "guide", "validate"]


@cache
def guide() -> str:
    """The tool-design rules (markdown): shared by the LLM prompt, the agent skills and the MCP server."""
    return Path(__file__).with_name("guide.md").read_text(encoding="utf-8")
