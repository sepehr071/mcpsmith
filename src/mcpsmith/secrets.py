"""Connection secrets in the OS keychain. Values never go through plan files, the CLI output or the agent."""

from __future__ import annotations

import json
import logging

import keyring
from keyring.errors import KeyringError, PasswordDeleteError

log = logging.getLogger(__name__)
INDEX = "__names__"  # keyring cannot list entries; this one holds {name: once} as JSON


def _service(connection: str) -> str:
    return f"mcpsmith/{connection}"


def _index(connection: str) -> dict[str, bool]:
    raw = keyring.get_password(_service(connection), INDEX)
    return json.loads(raw) if raw else {}


def _save_index(connection: str, index: dict[str, bool]) -> None:
    keyring.set_password(_service(connection), INDEX, json.dumps(index))


def _check(name: str) -> None:
    if name == INDEX:
        raise ValueError(f"{INDEX} is reserved")


def get(connection: str, name: str) -> str | None:
    """The stored value, or None. A `once` value is deleted as it is read."""
    _check(name)
    try:
        value = keyring.get_password(_service(connection), name)
        if value is not None and _index(connection).get(name):
            delete(connection, name)
    except KeyringError as e:
        log.debug("keychain unavailable for %s: %s", name, type(e).__name__)
        return None
    return value


def set(connection: str, name: str, value: str, once: bool = False) -> None:  # noqa: A001
    _check(name)
    keyring.set_password(_service(connection), name, value)
    _save_index(connection, {**_index(connection), name: once})


def delete(connection: str, name: str) -> None:
    _check(name)
    try:
        keyring.delete_password(_service(connection), name)
    except PasswordDeleteError:
        pass  # already gone
    index = _index(connection)
    if index.pop(name, None) is not None:
        _save_index(connection, index)


def names(connection: str) -> list[str]:
    return sorted(_index(connection))
