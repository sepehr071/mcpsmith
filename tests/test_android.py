import json
import subprocess
from pathlib import Path

import pytest

from mcpsmith.cli import main
from mcpsmith.importers import android

FIXTURE = Path(__file__).parent / "fixtures" / "android" / "interface.java"


@pytest.fixture(scope="module")
def src():
    return android.parse_interface(FIXTURE.read_text(encoding="utf-8"), "https://demo.test")


def ep(src, summary):
    return next(e for e in src.endpoints if e.summary == summary)


def locs(e):
    return {p.name: (p.location, p.required) for p in e.params}


def test_request_lines_and_param_kinds(src):
    e = ep(src, "homeTimeline")
    assert (e.method, e.path, e.id) == ("GET", "/api/v1/timelines/home", "GET /api/v1/timelines/home")
    assert locs(e) == {"max_id": ("query", False), "limit": ("query", False), "count": ("query", True)}
    assert locs(ep(src, "status")) == {"id": ("path", True)}
    assert locs(ep(src, "search")) == {
        "q": ("query", False),
        "filters": ("query", False),
        "X-Client": ("header", False),
    }
    assert locs(ep(src, "update")) == {"id": ("path", True), "update": ("body", True)}
    assert (ep(src, "ping").method, ep(src, "options").method) == ("HEAD", "OPTIONS")
    assert ep(src, "note").method == "PATCH"
    assert src.base_url == "https://demo.test"


def test_form_and_multipart_params_are_body(src):
    e = ep(src, "post")
    assert (e.method, locs(e)["status"], locs(e)["sensitive"], locs(e)["extra"][0]) == (
        "POST",
        ("body", False),
        ("body", True),
        "body",
    )
    assert "form-encoded" in e.description
    up = ep(src, "upload")
    assert {p.location for p in up.params} == {"body"} and "multipart" in up.description


def test_url_parameter_becomes_the_path(src):
    e = ep(src, "download")
    assert e.path == "{url}" and e.params[0].location == "path"


def test_http_annotation_delete_with_body_stays_unverified(src):
    e = ep(src, "removeFromList")
    assert (e.method, e.path) == ("DELETE", "/api/v1/lists/{id}/accounts")
    assert any(p.location == "body" for p in e.params)
    assert e.verified is False


def test_concatenated_or_constant_path_never_emitted(src):
    assert all("+" not in e.path and "Consts" not in e.path for e in src.endpoints)
    assert {"userById", "byConstant"}.isdisjoint(e.summary for e in src.endpoints)
    assert "skipped userById" in src.description and "skipped byConstant" in src.description


def test_credentials_never_stored(src):
    dumped = src.model_dump_json()
    assert "hunter2secret" not in dumped and "abcd1234efgh5678" not in dumped
    me = ep(src, "me")
    assert [s.param for s in src.auth if s.name in me.auth] == ["Authorization", "X-Api-Key"]
    assert {p.name for p in me.params} == {"Accept"}


def test_same_path_two_methods_get_distinct_ids(src):
    a, b = ep(src, "search"), ep(src, "searchByTag")
    assert a.path == b.path and a.id != b.id
    assert len({e.id for e in src.endpoints}) == len(src.endpoints)


def test_minified_names_resolve_by_structure(src):
    e = ep(src, "minified")
    assert e.method == "" and e.path == "/api/v1/minified/{id}"
    assert locs(e)["id"] == ("path", True) and "minified" in e.description
    h = ep(src, "minifiedHttp")
    assert (h.method, h.path, locs(h)["id"]) == ("PURGE", "/api/v1/things/{id}", ("path", True))


def test_every_endpoint_is_unverified(src):
    assert src.endpoints and all(
        e.verified is False and e.count == 0 and e.response_example is None for e in src.endpoints
    )


def test_not_an_interface_gives_empty_list_and_message():
    s = android.parse_interface("public class Util { int add(int a, int b) { return a + b; } }")
    assert s.endpoints == [] and "no Retrofit" in s.description


# ---------- REA wrappers ----------


class Runner:
    def __init__(self, out, code=0):
        self.out, self.code, self.calls = out, code, []

    def __call__(self, cmd, **kw):
        self.calls.append((cmd, kw))
        return subprocess.CompletedProcess(cmd, self.code, json.dumps(self.out), "boom")


@pytest.fixture
def apk(tmp_path):
    f = tmp_path / "app.apk"
    f.write_bytes(b"PK")
    return f


def test_class_source_runs_rea_outside_the_apk_directory(apk):
    r = Runner({"code": "interface A {}"})
    assert android.rea_class_source(apk, "com.x.Api", runner=r) == "interface A {}"
    cmd, kw = r.calls[0]
    assert "rea-agents@6.7.0" in cmd and "decompile" in cmd and str(apk.resolve()) in cmd
    assert Path(kw["cwd"]).resolve() != apk.parent.resolve()


def test_class_name_and_missing_apk_are_rejected(apk, tmp_path):
    with pytest.raises(ValueError):
        android.rea_class_source(apk, "--evil", runner=Runner({}))
    with pytest.raises(ValueError):
        android.rea_class_source(tmp_path / "no.apk", "a.B", runner=Runner({}))


def test_rea_failure_carries_the_install_hint(apk):
    with pytest.raises(RuntimeError, match="doctor"):
        android.rea_class_source(apk, "a.B", runner=Runner({}, code=1))


def test_find_api_classes(apk):
    out = {"matches": [{"className": "com.x.Api", "value": "api/v1/x"}, {"className": "com.x.Api"}, {"v": "a.b.Svc"}]}
    assert android.find_api_classes(apk, runner=Runner(out)) == ["a.b.Svc", "com.x.Api"]


# ---------- CLI ----------


def test_cli_lists_candidates_then_writes_source(apk, tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(android, "find_api_classes", lambda a: ["com.x.Api"])
    monkeypatch.setattr(android, "rea_class_source", lambda a, c: FIXTURE.read_text(encoding="utf-8"))
    wd = str(tmp_path / "wd")
    assert main(["app-android", str(apk), "-w", wd]) == 0
    assert "com.x.Api" in capsys.readouterr().out and not (tmp_path / "wd" / "source.json").exists()
    assert main(["app-android", str(apk), "--class", "com.x.Api", "--base-url", "https://demo.test", "-w", wd]) == 0
    assert "mcpsmith probe" in capsys.readouterr().out
    assert (tmp_path / "wd" / "source.json").exists() and (tmp_path / "wd" / "digest.md").exists()


def test_cli_empty_interface_exits_nonzero(apk, tmp_path, monkeypatch):
    monkeypatch.setattr(android, "rea_class_source", lambda a, c: "class X {}")
    assert main(["app-android", str(apk), "--class", "a.X", "-w", str(tmp_path / "wd")]) == 2
