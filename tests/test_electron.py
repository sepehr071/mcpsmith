import asyncio
import json
import re
import struct
from pathlib import Path

import pytest

from mcpsmith.cli import main
from mcpsmith.importers import electron

FIXTURES = Path(__file__).parent / "fixtures" / "electron"


def make_asar(path: Path, files: dict[str, str]) -> Path:
    data, index = b"", {}
    for name, text in files.items():
        raw, node = text.encode(), index
        *dirs, leaf = name.split("/")
        for d in dirs:
            node = node.setdefault(d, {"files": {}})["files"]
        node[leaf] = {"size": len(raw), "offset": str(len(data))}
        data += raw
    body = json.dumps({"files": index}).encode()
    padded = body + b"\0" * (-len(body) % 4)
    header = struct.pack("<II", len(padded) + 4, len(body)) + padded
    path.write_bytes(struct.pack("<II", 4, len(header)) + header + data)
    return path


@pytest.fixture(scope="module")
def files():
    return electron.read_app(FIXTURES)


@pytest.fixture(scope="module")
def src(files):
    return electron.parse_sources(files, "https://api.demo.test")


def pairs(src):
    return {(e.method, e.path) for e in src.endpoints}


def test_read_app_prefers_map_sources_and_falls_back(files):
    assert "app/src/lib/api.ts" in files and "plain.js" in files
    assert "renderer.js" not in files  # its map carries the original sources
    assert not any("node_modules" in n for n in files)
    assert "broken.js" in files  # sourcesContent is null: the bundle is read


def test_templates_become_placeholders(src):
    got = pairs(src)
    assert ("GET", "/repos/{owner}/{name}") in got
    assert ("GET", "/repos/{owner}/{name}/forks") in got
    assert ("POST", "/repos/{owner}/{name}/issues") in got
    assert ("GET", "/user") in got
    assert ("DELETE", "/v1/items/{id}") in got
    assert ("PUT", "/v1/broken") in got
    assert ("GET", "/bundle-only/path") not in got


def test_query_in_path_becomes_param(src):
    e = next(e for e in src.endpoints if e.path.endswith("/forks"))
    assert [(p.name, p.location, p.example) for p in e.params if p.location == "query"] == [
        ("per_page", "query", "100")
    ]
    assert {p.name for p in e.params if p.location == "path"} == {"owner", "name"}


def test_summary_names_the_original_file(src):
    e = next(e for e in src.endpoints if e.path.endswith("/forks"))
    assert e.summary.startswith("app/src/lib/api.ts")


def test_unresolvable_path_is_skipped_with_a_note(src):
    assert not any("BASE" in e.path for e in src.endpoints)
    assert "plain.js" in src.description and "BASE + kind" in src.description


def test_no_endpoint_path_holds_expression_characters(src):
    for e in src.endpoints:
        assert not re.search(r"[$+'\"`\\]", e.path), e.path
        assert re.sub(r"\{\w+\}", "", e.path).count("{") == 0


def test_method_unreadable_is_empty_with_a_note(src):
    e = next(e for e in src.endpoints if e.path == "/v1/unknown-method")
    assert e.method == ""
    assert "unknown-method" in src.description


def test_secrets_and_userinfo_are_gone(src):
    dump = src.model_dump_json()
    assert "ghp_" not in dump and "hunter2" not in dump and "admin:" not in dump
    e = next(e for e in src.endpoints if e.path == "/v1/status")
    assert e.base_url == "https://api.demo.test" and e.method == "GET"
    me = next(e for e in src.endpoints if e.path == "/user")
    assert [a.param for a in src.auth] == ["Authorization"] and me.auth == ["authorization"]
    assert [(p.name, p.example) for p in me.params if p.location == "header"] == [("X-Client", "demo")]


def test_same_call_in_two_files_gets_two_ids(src):
    same = [e for e in src.endpoints if (e.method, e.path) == ("GET", "/repos/{owner}/{name}")]
    assert len(same) == 2 and len({e.id for e in same}) == 2
    assert len({e.id for e in src.endpoints}) == len(src.endpoints)


def test_everything_is_unverified(src):
    assert src.kind == "app" and src.base_url == "https://api.demo.test"
    assert all(e.verified is False and e.count == 0 and e.response_example is None for e in src.endpoints)


def test_single_file_and_map_inputs(tmp_path):
    assert set(electron.read_app(FIXTURES / "plain.js")) == {"plain.js"}
    assert "app/src/lib/api.ts" in electron.read_app(FIXTURES / "renderer.js.map")


def test_read_asar(tmp_path):
    asar = make_asar(
        tmp_path / "app.asar", {"main.js": "x.request('GET', 'ping')", "node_modules/a.js": "axios.get('/no')"}
    )
    files = electron.read_app(asar)
    assert files == {"main.js": "x.request('GET', 'ping')"}
    assert pairs(electron.parse_sources(files)) == {("GET", "/ping")}


def test_malformed_asar_names_the_file(tmp_path):
    good = make_asar(tmp_path / "good.asar", {"main.js": "axios.get('/ping')"})
    for name, blob in {
        "short.asar": good.read_bytes()[:10],
        "cut.asar": good.read_bytes()[:-5],
        "junk.asar": b"\x04\0\0\0" + b"\xff" * 40,
    }.items():
        bad = tmp_path / name
        bad.write_bytes(blob)
        with pytest.raises(ValueError, match=name):
            electron.read_app(bad)
    assert not (tmp_path / "wd").exists()


def test_caps_are_reported(tmp_path):
    for i in range(3):
        (tmp_path / f"f{i}.js").write_text(f"axios.get('/a{i}')", encoding="utf-8")
    (tmp_path / "big.js").write_text("x" * 100, encoding="utf-8")
    notes: list[str] = []
    files = electron.read_app(tmp_path, notes, max_files=3, max_bytes=50)
    assert len(files) == 2 and "big.js" not in files and len(notes) == 2
    many = {f"f{i}.js": f"axios.get('/a{i}')" for i in range(5)}
    assert "dropped 3" in electron.parse_sources(many, max_endpoints=2).description


# ---------- CLI and MCP tool ----------


def test_cli_writes_source_and_digest(tmp_path, capsys):
    wd = tmp_path / "wd"
    assert main(["app-js", str(FIXTURES), "--base-url", "https://api.demo.test", "-w", str(wd)]) == 0
    out = capsys.readouterr().out
    assert "all unverified" in out and "mcpsmith probe" in out
    saved = json.loads((wd / "source.json").read_text(encoding="utf-8"))
    assert saved["kind"] == "app" and (wd / "digest.md").exists()
    assert "ghp_" not in (wd / "digest.md").read_text(encoding="utf-8")


def test_cli_rejects_a_missing_path_and_an_empty_app(tmp_path):
    assert main(["app-js", str(tmp_path / "nope"), "-w", str(tmp_path / "wd")]) == 2
    (tmp_path / "empty.js").write_text("var a = 1", encoding="utf-8")
    assert main(["app-js", str(tmp_path / "empty.js"), "-w", str(tmp_path / "wd")]) == 2


def test_mcp_tool(tmp_path):
    from mcpsmith import server

    out = asyncio.run(server.smith_app_js(str(FIXTURES), "https://api.demo.test", str(tmp_path / "wd")))
    assert out["endpoints"] == out["unverified"] > 0 and "UNVERIFIED" in out["digest"]
    assert (tmp_path / "wd" / "source.json").exists()
