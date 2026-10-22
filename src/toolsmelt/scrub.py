"""The privacy rules every importer follows, public for plugin importers (see docs/extending.md).

Nothing an importer puts in a `Source` (examples, notes, base_url) may carry a secret or a personal value: the
Source is written to disk, shown to the designing agent and copied into tests.
"""

from __future__ import annotations

from typing import Any

from .importers.android import strip_userinfo as _strip_userinfo
from .importers.har import _is_credential, _is_secret, _scrub
from .importers.har import note_text as _note_text

__all__ = ["is_credential", "is_secret", "note_text", "scrub", "strip_userinfo"]


def scrub(value: Any, name: str = "") -> Any:
    """Copy of `value` (any JSON value) with secrets and personal data replaced; ids, titles and prices stay.

    `name` is the field, header or parameter the value came from: a secret name such as `token` redacts it.
    """
    return _scrub(value, name)


def is_secret(name: str) -> bool:
    """True when a field, header or parameter name looks like it holds a secret (token, password, key, ...)."""
    return _is_secret(name)


def is_credential(name: str) -> bool:
    """True when `name` is a secret that logs in (token, API key, session id), not a one-time value like CSRF."""
    return _is_credential(name)


def note_text(s: str, limit: int = 60) -> str:
    """Quote app source in a note safely: string literals blanked, secret `key=value` masked, cut to `limit`."""
    return _note_text(s, limit)


def strip_userinfo(url: str) -> str:
    """`url` without `user:password@` before the host."""
    return _strip_userinfo(url)
