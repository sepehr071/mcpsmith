"""Read an OpenAPI 3.x / Swagger 2.0 spec a HAR recording or an REA browser-scenario capture (JSON or YAML, file or URL) into a Source."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import yaml

from mcpsmith.importers import android, electron, har, openapi, rea  # noqa: F401  (android, electron: see app-android, app-js)
from mcpsmith.model import Source
from mcpsmith.runtime.http import tls_context


def load(path_or_url: str, hosts: list[str] | None = None) -> Source:
    """`hosts` filters HAR traffic to those hosts (exact or subdomain); ignored for OpenAPI."""
    origin = None
    if path_or_url.startswith(("http://", "https://")):
        resp = httpx.get(path_or_url, follow_redirects=True, timeout=60, verify=tls_context())
        resp.raise_for_status()
        text, origin = resp.text, str(resp.url)
    else:
        text = Path(path_or_url).read_text(encoding="utf-8-sig")
    try:
        doc = json.loads(text)
    except ValueError:
        doc = yaml.safe_load(text)
    if not isinstance(doc, dict):
        raise ValueError(f"{path_or_url}: not a JSON/YAML object")
    if isinstance(doc.get("log"), dict) and "entries" in doc["log"]:
        return har.parse(doc, hosts=hosts)
    # REA scenario capture: evidence wrapper with predicate_type "rea.browser-scenario-capture" and normalized_result.events.items
    if doc.get("predicate_type") == "rea.browser-scenario-capture" and "events" in (doc.get("normalized_result") or {}):
        return har.parse(rea.to_har(doc), hosts=hosts)
    if "openapi" in doc or "swagger" in doc:
        return openapi.parse(doc, origin=origin)
    raise ValueError(f"{path_or_url}: neither OpenAPI/Swagger (no 'openapi'/'swagger' key) nor HAR (no log.entries)")
