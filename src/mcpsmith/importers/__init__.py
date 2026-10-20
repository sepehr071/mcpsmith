"""Read an OpenAPI 3.x / Swagger 2.0 spec or a HAR recording (JSON or YAML, file or URL) into a Source."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import yaml

from mcpsmith.importers import har, openapi
from mcpsmith.model import Source


def load(path_or_url: str, hosts: list[str] | None = None) -> Source:
    """`hosts` filters HAR traffic to those hosts (exact or subdomain); ignored for OpenAPI."""
    origin = None
    if path_or_url.startswith(("http://", "https://")):
        resp = httpx.get(path_or_url, follow_redirects=True, timeout=60)
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
    if "openapi" in doc or "swagger" in doc:
        return openapi.parse(doc, origin=origin)
    raise ValueError(f"{path_or_url}: neither OpenAPI/Swagger (no 'openapi'/'swagger' key) nor HAR (no log.entries)")
