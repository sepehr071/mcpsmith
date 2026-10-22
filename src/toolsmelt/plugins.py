"""Third-party extensions, found through installed packages' entry points (see docs/extending.md).

Groups: `toolsmelt.importers` (new input formats), `toolsmelt.transforms` (value transforms) and `toolsmelt.signin`
(sign-in providers). Each is loaded on first use, once per process.
"""

from __future__ import annotations

import logging
from functools import cache
from importlib.metadata import entry_points
from typing import Any

log = logging.getLogger("toolsmelt")
GROUPS = ("toolsmelt.importers", "toolsmelt.transforms", "toolsmelt.signin")


@cache
def load(group: str) -> dict[str, Any]:
    """Entry point name -> loaded object for `group`. A plugin that fails to import is skipped with a warning."""
    found: dict[str, Any] = {}
    for ep in entry_points(group=group):
        try:
            found[ep.name] = ep.load()
        except Exception as e:  # a broken third-party package must not take toolsmelt down
            log.warning("toolsmelt plugin %r (%s, %s) failed to load and is skipped: %s", ep.name, group, ep.value, e)
    return found


def skip(group: str, name: str, why: str) -> None:
    """Report a plugin that loaded but cannot be used."""
    log.warning("toolsmelt plugin %r (%s) is skipped: %s", name, group, why)
