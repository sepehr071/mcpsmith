"""toolsmelt (formerly mcpsmith): turn an API spec, a website, an app or a database into curated MCP tools."""

import os

__version__ = "0.8.0"


def getenv(name: str, default: str | None = None) -> str | None:
    """`TOOLSMELT_<name>`, else the pre-rename `MCPSMITH_<name>`, else `default`."""
    return os.environ.get(f"TOOLSMELT_{name}") or os.environ.get(f"MCPSMITH_{name}") or default
