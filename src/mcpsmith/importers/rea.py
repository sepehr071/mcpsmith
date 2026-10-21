"""REA `capture_browser_scenario` evidence -> HAR dict (events grouped by transaction_id), then parsed by har.parse."""

from __future__ import annotations

import base64


def to_har(capture: dict) -> dict:
    tx: dict[str, dict] = {}
    for ev in ((capture.get("normalized_result") or {}).get("events") or {}).get("items") or []:
        t = tx.setdefault(ev.get("transaction_id"), {})
        if ev.get("kind") in ("request", "response"):
            t[ev["kind"]] = ev
        elif ev.get("kind") == "network-content" and ev.get("phase") in ("request", "response"):
            t["c_" + ev["phase"]] = ev
    entries = []
    for t in tx.values():
        req, resp = t.get("request"), t.get("response") or {}
        if not req:
            continue
        rc, sc = t.get("c_request") or {}, t.get("c_response") or {}
        request = {"method": req["method"], "url": req["url"]["url"], "headers": _headers(rc)}
        body = rc.get("body") or {}
        if body.get("state") == "captured":
            request["postData"] = {"mimeType": body.get("media_type", ""), "text": _text(body)}
        content = {}
        sbody = sc.get("body") or {}
        if sbody.get("state") == "captured":
            content = {
                "mimeType": sbody.get("media_type", ""),
                "text": sbody["content"],
                "encoding": sbody.get("encoding"),
            }
        entries.append(
            {
                "_resourceType": req.get("resource_type"),
                "request": request,
                "response": {"status": resp.get("status") or 0, "headers": _headers(sc), "content": content},
            }
        )
    return {"log": {"entries": entries}}


def _headers(content_event: dict) -> list[dict]:
    items = (content_event.get("headers") or {}).get("items") or []
    return [{"name": h["name"], "value": h.get("value", "")} for h in items if not h.get("redacted")]


def _text(body: dict) -> str:
    raw = body.get("content", "")
    return base64.b64decode(raw).decode("utf-8", "replace") if body.get("encoding") == "base64" else raw
