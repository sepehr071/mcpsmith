"""Read an OpenAPI 3.x / Swagger 2.0 spec, a HAR recording, an REA browser-scenario capture, a Postman collection
(JSON or YAML, file or URL), a WSDL file, a file of cURL commands, a database URL or a gRPC URL into a Source."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Protocol

import httpx
import yaml

from toolsmelt import plugins
from toolsmelt.importers import android, curl, electron, har, openapi, postman, rea, sql, wsdl  # noqa: F401  (android, electron: see app-android, app-js)
from toolsmelt.importers import grpc as grpc_source
from toolsmelt.model import Source
from toolsmelt.runtime.http import tls_context


class Importer(Protocol):
    """A `toolsmelt.importers` plugin (see docs/extending.md). `data` is the file parsed as JSON/YAML, else None."""

    name: str

    def sniff(self, path: Path, data: Any | None) -> bool: ...

    def load(self, path: Path, data: Any | None, **opts: Any) -> Source: ...


def _parse(text: str | None) -> Any | None:
    if text is None:
        return None
    try:
        return json.loads(text)
    except ValueError:
        pass
    try:
        return yaml.safe_load(text)
    except yaml.YAMLError:
        return None


def _plugin(path: Path, data: Any | None) -> Importer | None:
    for ep_name, imp in plugins.load("toolsmelt.importers").items():
        try:
            if imp.sniff(path, data):
                return imp
        except Exception as e:  # a broken plugin must not block the built-in formats or other plugins
            plugins.skip("toolsmelt.importers", ep_name, f"sniff failed: {e}")
    return None


def load(path_or_url: str, hosts: list[str] | None = None) -> Source:
    """`hosts` filters HAR traffic to those hosts (exact or subdomain); ignored for OpenAPI.

    Built-in formats come first; any other local file is offered to `toolsmelt.importers` plugins.
    """
    origin, path = None, None
    if grpc_source.is_grpc(path_or_url):  # grpc://host:port, read through server reflection
        return grpc_source.parse(path_or_url)
    if sql.sniff(path_or_url):  # a database URL: sqlite:///shop.db, postgresql://user@host/db, ...
        return sql.parse(path_or_url)
    if path_or_url.startswith(("http://", "https://")):
        resp = httpx.get(path_or_url, follow_redirects=True, timeout=60, verify=tls_context())
        resp.raise_for_status()
        text, origin = resp.text, str(resp.url)
    else:
        path = Path(path_or_url)
        try:
            text = path.read_text(encoding="utf-8-sig")
        except UnicodeDecodeError:  # binary: only a plugin can read it
            text = None
    if wsdl.sniff(text):  # XML: before _parse, which would not give a dict for it
        try:
            return wsdl.parse(text, origin=origin)
        except ValueError as e:
            raise ValueError(f"{path_or_url}: {e}") from e
    doc = _parse(text)
    if isinstance(doc, dict):
        try:
            if isinstance(doc.get("log"), dict) and "entries" in doc["log"]:
                return har.parse(doc, hosts=hosts)
            # REA scenario capture: evidence wrapper with predicate_type "rea.browser-scenario-capture" and normalized_result.events.items
            if doc.get("predicate_type") == "rea.browser-scenario-capture" and "events" in (
                doc.get("normalized_result") or {}
            ):
                return har.parse(rea.to_har(doc), hosts=hosts)
            if postman.sniff(doc):
                return postman.parse(doc)
            if "openapi" in doc or "swagger" in doc:
                return openapi.parse(doc, origin=origin)
        except ValueError as e:  # the parsers never see the path; name the file in their "malformed ..." errors
            raise ValueError(f"{path_or_url}: {e}") from e
    if path and curl.sniff(text):
        try:
            return curl.parse(text, hosts=hosts)
        except ValueError as e:
            raise ValueError(f"{path_or_url}: {e}") from e
    # shortcut: plugins see local files only; pass a URL's text through a temp file if a plugin needs URLs
    if path and (imp := _plugin(path, doc)):
        return imp.load(path, doc, hosts=hosts)
    if not isinstance(doc, dict):
        raise ValueError(f"{path_or_url}: not a JSON/YAML object")
    raise ValueError(
        f"{path_or_url}: neither OpenAPI/Swagger (no 'openapi'/'swagger' key), HAR (no log.entries) "
        "nor a Postman collection (no info.schema)"
    )
