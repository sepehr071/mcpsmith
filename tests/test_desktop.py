"""app-desktop: guided proxy capture of a program's own traffic, imported through the HAR importer."""

import asyncio
import json
from pathlib import Path

from toolsmelt import server
from toolsmelt.cli import main
from toolsmelt.model import load_source

SKILL = Path(__file__).parent.parent / "skills" / "extract-api" / "SKILL.md"


def _entry(method, url, body=None, headers=None):
    return {
        "request": {"method": method, "url": url, "headers": headers or [], "queryString": []},
        "response": {
            "status": 200 if body is not None else 0,
            "headers": [],
            "content": {"mimeType": "application/json", "text": json.dumps(body)} if body is not None else {},
        },
    }


def _har(tmp_path, *entries):
    path = tmp_path / "capture.har"
    path.write_text(json.dumps({"log": {"entries": list(entries)}}), encoding="utf-8")
    return path


def test_steps(tmp_path, capsys):
    wd = tmp_path / "wd"
    assert main(["app-desktop", "--port", "9090", "-w", str(wd)]) == 0
    out = capsys.readouterr().out
    assert f"--set hardump={wd / 'capture.har'}" in out
    assert "9090" in out and "authorized" in out
    assert "Stop mitmdump with Ctrl+C" in out and "Do not kill the process" in out
    assert f"-nr {wd / 'flows.mitm'} --set hardump={wd / 'capture.har'}" in out
    assert "save_stream_filter" not in out
    assert not (wd / "source.json").exists()
    for bad in ("verify", "--ssl-insecure", "pinning bypass"):
        assert bad not in out.lower()


def test_import_ignores_connect(tmp_path, capsys):
    har = _har(
        tmp_path,
        _entry("CONNECT", "https://api.demo.test:443"),
        _entry("GET", "https://api.demo.test/items", [{"id": 1, "name": "a"}]),
    )
    wd = tmp_path / "wd"
    assert main(["app-desktop", str(har), "-w", str(wd)]) == 0
    out = capsys.readouterr().out
    assert "1 endpoints" in out and "toolsmelt draft" in out
    eps = load_source(wd / "source.json").endpoints
    assert len(eps) == 1 and eps[0].verified is True


def test_nothing_to_import(tmp_path, capsys):
    har = _har(tmp_path, _entry("CONNECT", "https://api.demo.test:443"))
    wd = tmp_path / "wd"
    assert main(["app-desktop", str(har), "-w", str(wd)]) == 2
    assert "nothing to import" in capsys.readouterr().err
    assert not (wd / "source.json").exists()


def test_host_filter(tmp_path):
    har = _har(
        tmp_path,
        _entry("GET", "https://api.demo.test/items", [{"id": 1}]),
        _entry("GET", "https://other.test/news", [{"id": 2}]),
    )
    wd = tmp_path / "wd"
    assert main(["app-desktop", str(har), "--host", "api.demo.test", "-w", str(wd)]) == 0
    text = (wd / "source.json").read_text(encoding="utf-8")
    assert "api.demo.test" in text and "other.test" not in text


def test_secret_scrubbed(tmp_path):
    har = _har(
        tmp_path,
        _entry(
            "GET",
            "https://api.demo.test/items",
            [{"id": 1}],
            headers=[{"name": "Authorization", "value": "Bearer s3cr3t-app-token-0123456789"}],
        ),
    )
    wd = tmp_path / "wd"
    assert main(["app-desktop", str(har), "-w", str(wd)]) == 0
    assert "s3cr3t-app-token" not in (wd / "source.json").read_text(encoding="utf-8")


def test_force(tmp_path):
    har = _har(tmp_path, _entry("GET", "https://api.demo.test/items", [{"id": 1}]))
    wd = tmp_path / "wd"
    assert main(["app-desktop", str(har), "-w", str(wd)]) == 0
    assert main(["app-desktop", str(har), "-w", str(wd)]) == 2
    assert main(["app-desktop", str(har), "-w", str(wd), "--force"]) == 0


def test_tool(tmp_path):
    out = asyncio.run(server.smith_app_desktop(None, None, str(tmp_path / "wd"), 9091))
    assert any("--set hardump=" in s for s in out["steps"])
    har = _har(tmp_path, _entry("GET", "https://api.demo.test/items", [{"id": 1}]))
    out = asyncio.run(server.smith_app_desktop(str(har), None, str(tmp_path / "wd2")))
    assert out["endpoints"] == 1 and "digest" in out


def test_skill_scope_sentence():
    text = SKILL.read_text(encoding="utf-8")
    assert "certificate-pinned programs are out of scope" in text
