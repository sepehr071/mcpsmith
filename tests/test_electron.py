import asyncio
import json
import re
import struct
import time
from pathlib import Path

import pytest

from mcpsmith.cli import main
from mcpsmith.importers import android, electron

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
    return write_asar(path, json.dumps({"files": index}).encode(), data)


def write_asar(path: Path, body: bytes, data: bytes = b"") -> Path:
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
        wd = tmp_path / "wd"
        assert main(["app-js", str(bad), "-w", str(wd)]) == 2
        assert not wd.exists()  # nothing written


def test_deeply_nested_asar_index_is_a_clear_error(tmp_path):
    deep = write_asar(tmp_path / "deep.asar", b'{"files":{"a":' * 5000 + b"{}" + b"}}" * 5000)
    with pytest.raises(ValueError, match="deep.asar"):
        electron.read_app(deep)


def test_caps_are_reported(tmp_path):
    for i in range(3):
        (tmp_path / f"f{i}.js").write_text(f"axios.get('/a{i}')", encoding="utf-8")
    (tmp_path / "big.js").write_text("x" * 100, encoding="utf-8")
    notes: list[str] = []
    files = electron.read_app(tmp_path, notes, max_files=3, max_bytes=50)
    assert len(files) == 2 and "big.js" not in files and len(notes) == 2
    many = {f"f{i}.js": f"axios.get('/a{i}')" for i in range(5)}
    capped = electron.parse_sources(many, max_endpoints=2)
    assert len(capped.endpoints) == 2 and "stopped at the 2-endpoint cap" in capped.description
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "dep.js").write_text("axios.get('/dep')", encoding="utf-8")
    notes = []
    files = electron.read_app(tmp_path, notes, max_bytes=10_000, max_total=40)
    assert "node_modules/dep.js" not in files and len(files) == 2 and any("total read budget" in n for n in notes)


def test_path_held_in_a_variable_is_resolved_once():
    code = """
function a(org) {
  const one = `orgs/${org}/repos`
  return this.request('GET', one)
}
function b() {
  let twice = 'x/first'
  twice = 'x/second'
  return this.request('GET', twice)
}
function c(org) {
  const tern = org ? 'a' : 'b'
  return this.request('GET', tern)
}
function d() {
  const plus = 'p/' + id
  return this.request('GET', plus)
}
function e(param) {
  return this.request('GET', param)
}
"""
    s = electron.parse_sources({"v.js": code})
    assert pairs(s) == {("GET", "/orgs/{org}/repos")}
    for name in ("twice", "tern", "plus", "param"):
        assert f"({name})" in s.description


def test_a_parameter_or_another_scope_never_binds_an_assignment():
    code = """
function a() { const path = '/admin/secret' }
function b(path) { return request('GET', path) }
function c() {
  let p = '/a'
  if (x) p += '/b'
  return request('GET', p)
}
const q = '/shadowed'
class K { m(q) { return request('GET', q) } }
const r = '/cb'
items.map(x => request('GET', r))
const [s] = [1]
let t = '/t'
;[t] = other
request('GET', t)
"""
    s = electron.parse_sources({"s.js": code})
    assert s.endpoints == [] and "/admin/secret" not in s.description
    for name in ("path", "p", "q", "r", "t"):
        assert f"({name})" in s.description


def test_resolved_path_is_noted_with_its_line():
    s = electron.parse_sources({"r.js": "const u = '/v1/x'\n\nrequest('GET', u)\n"})
    assert [(e.path, e.description) for e in s.endpoints] == [("/v1/x", "path resolved from r.js:1")]


def test_fetch_with_a_non_literal_method_is_unreadable():
    s = electron.parse_sources(
        {"f.js": "fetch(u, { method: m, body })\nfetch('/k', { method })\nfetch('/ok', { body })"}
    )
    assert {(e.path, e.method) for e in s.endpoints} == {("/k", ""), ("/ok", "GET")}
    assert "{u}" not in {e.path for e in s.endpoints}


def test_quoted_method_key_is_read_not_guessed():
    code = """fetch('/a', { "method": m })
fetch('/b', { 'method': 'DELETE' })
fetch('/c', { "method": "post", body })"""
    s = electron.parse_sources({"q.js": code})
    assert {(e.path, e.method) for e in s.endpoints} == {("/a", ""), ("/b", "DELETE"), ("/c", "POST")}


def test_dangling_symlink_is_skipped(tmp_path):
    (tmp_path / "ok.js").write_text("axios.get('/ok')", encoding="utf-8")
    try:
        (tmp_path / "gone.js").symlink_to(tmp_path / "missing.js")
    except (OSError, NotImplementedError):
        pytest.skip("cannot create symlinks here")
    assert set(electron.read_app(tmp_path)) == {"ok.js"}


def test_userinfo_with_an_at_in_the_password_is_stripped():
    assert electron.strip_userinfo("https://u:p@ss@api.demo.test/x") == "https://api.demo.test/x"
    assert electron.parse_sources({}, "https://u:p@ss@api.demo.test").base_url == "https://api.demo.test"
    assert android.parse_interface("class X {}", "https://u:p@ss@api.demo.test").base_url == "https://api.demo.test"


def test_comment_stripping_is_linear():
    start = time.perf_counter()
    for text, expected in ((" /*" * 200_000, 0), (" //" * 200_000, 1), ("/* " + "*" * 400_000, 0)):
        # an unterminated block comment runs to the end of the file; a line comment ends at its newline
        assert len(electron.parse_sources({"p.js": text + "\naxios.get('/x')"}).endpoints) == expected
    assert time.perf_counter() - start < 3


def test_base_url_userinfo_is_stripped():
    assert electron.parse_sources({}, "https://u:p@api.demo.test/v1").base_url == "https://api.demo.test/v1"
    assert android.parse_interface("class X {}", "https://u:p@api.demo.test").base_url == "https://api.demo.test"


def test_comments_and_apostrophes_are_not_calls():
    code = """
/**
 * we'll fetch (like a very active repo) in bulk
 */
// fetch ('also not a call
const real = () => fetch('/v1/real', { method: 'GET' }) // it's fine
"""
    s = electron.parse_sources({"c.js": code})
    assert pairs(s) == {("GET", "/v1/real")} and s.description == ""
    assert s.endpoints[0].summary == "c.js:6"


def test_oversize_note_says_whether_a_map_covered_it(tmp_path):
    (tmp_path / "big.js").write_text("x" * 100, encoding="utf-8")
    (tmp_path / "big.js.map").write_text(
        json.dumps({"sources": ["s.ts"], "sourcesContent": ["axios.get('/ok')"]}), encoding="utf-8"
    )
    (tmp_path / "lone.js").write_text("y" * 100, encoding="utf-8")
    notes: list[str] = []
    files = electron.read_app(tmp_path, notes, max_bytes=90)
    assert files == {"s.ts": "axios.get('/ok')"} and len(notes) == 2
    assert any("big.js:" in n and "its .map sources were read instead" in n for n in notes)
    assert any("lone.js:" in n and "no .map sources cover it" in n for n in notes)


def test_big_map_replaces_its_bundle_and_a_too_big_map_falls_back(tmp_path):
    bundle = "t.request('GET', `repos/${e}/${t}`)" + " " * 200  # over the 100-byte raw cap, as a real bundle is
    (tmp_path / "app.js").write_text(bundle, encoding="utf-8")
    (tmp_path / "app.js.map").write_text(
        json.dumps(
            {"sources": ["api.ts"], "sourcesContent": ["x.request('GET', `repos/${owner}/${name}`)" + " " * 200]}
        ),
        encoding="utf-8",
    )
    notes: list[str] = []
    src = electron.parse_sources(electron.read_app(tmp_path, notes, max_bytes=100, max_map_bytes=10_000))
    assert pairs(src) == {("GET", "/repos/{owner}/{name}")}  # the map, not the minified bundle
    assert [n for n in notes if "app.js:" in n] and "its .map sources were read instead" in notes[0]
    # the map is over its own cap: its bundle is read (small cap lifted for it) and the note names the map
    notes = []
    src = electron.parse_sources(electron.read_app(tmp_path, notes, max_bytes=10_000, max_map_bytes=100))
    assert pairs(src) == {("GET", "/repos/{e}/{t}")}
    assert any("skipped app.js.map:" in n and "100-byte cap" in n for n in notes)


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


def test_electron_notes_hide_secrets_and_are_capped():
    from mcpsmith.design import digest

    code = "fetch(BASE + 'x?api_key=sk_live_123');" + "".join(f"fetch(u{i});" for i in range(300))
    s = electron.parse_sources({"a.js": code})
    assert "sk_live_123" not in s.model_dump_json() and "sk_live_123" not in digest(s)
    assert len(s.description.splitlines()) == 201
