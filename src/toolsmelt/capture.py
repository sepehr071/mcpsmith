"""Record a browsing session to HAR with a headed Chromium (Playwright, optional extra `capture`)."""

from __future__ import annotations

import os
import re
from pathlib import Path

INSTALL_HINT = 'pip install "toolsmelt[capture]" && playwright install chromium'


def record(url: str, out: Path, hosts: list[str] | None = None) -> Path:
    """Open `url`, let the user browse, and write the session to `out` when the tab or browser is closed."""
    try:
        from playwright.sync_api import Error, sync_playwright
    except ImportError as e:
        raise RuntimeError(f"Recording needs Playwright: {INSTALL_HINT}") from e

    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    options: dict = {"record_har_path": str(out), "record_har_content": "embed", "record_har_mode": "full"}
    if hosts:
        names = "|".join(re.escape(h.lstrip(".")) for h in hosts)
        options["record_har_url_filter"] = re.compile(rf"^https?://([^/]*\.)?({names})(:\d+)?(/|$)")

    # Chromium ignores proxy env vars; pass the developer's proxy on so recording works behind one.
    proxy = os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy") or os.environ.get("HTTP_PROXY")
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False, proxy={"server": proxy} if proxy else None)
        context = browser.new_context(**options)
        page = context.new_page()
        page.goto(url)
        print("Recording. Use the site features you want as tools, then close the browser tab to save the HAR.")
        while browser.is_connected() and context.pages:
            try:
                context.pages[0].wait_for_timeout(500)
            except Error:
                pass  # the page closed while we waited; the loop condition decides
        try:
            context.close()
            browser.close()
        except Error:
            pass  # browser already gone
    if not out.exists():
        raise RuntimeError(f"No HAR written to {out}: close the tab rather than the whole browser window.")
    print(
        f"Warning: {out} holds cookies and personal data from this session. Keep it out of git and delete it after `inspect`."
    )
    return out
