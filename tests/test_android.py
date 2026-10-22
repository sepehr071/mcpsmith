import contextlib
import json
import os
import zipfile
from pathlib import Path

import pytest

from mcpsmith.cli import main
from mcpsmith.importers import android, jadx

FIXTURES = Path(__file__).parent / "fixtures" / "android"
FIXTURE = FIXTURES / "interface.java"
MAPPING = {"a": "GET", "t": "Path", "u": "Query"}


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
    assert "form-encoded" in e.description and e.body_encoding == "form"
    up = ep(src, "upload")
    assert {p.location for p in up.params} == {"body"} and "multipart" in up.description
    assert up.body_encoding == "multipart" and ep(src, "update").body_encoding == "json"
    # @Field alone (the class annotation renamed or missing) still means a form
    assert ep(src, "note").body_encoding == "form"


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


# ---------- minified names resolved from the app's request builder ----------


def test_resolve_annotations_reads_instanceof_blocks():
    got = android.resolve_annotations((FIXTURES / "builder.java").read_text(encoding="utf-8"))
    assert got == {
        "b": "DELETE",
        "f": "GET",
        "o": "POST",
        "p": "PUT",
        "k": "Headers",
        "y": "Url",
        "s": "Path",
        "t": "Query",
        "v": "QueryName",
        "u": "QueryMap",
        "i": "Header",
        "a": "Body",
        "c": "Field",
    }


def test_resolve_annotations_ignores_decoys_and_unclear_blocks():
    java = """
    String x = "GET";
    void m(Object o) {
        if (o instanceof q.a) { log("@Query and @Path"); }
        if (o instanceof java.util.List) { log("@Body"); }
        if (!(o instanceof q.b)) { log("@Body"); }
    }
    """
    assert android.resolve_annotations(java) == {}


def test_parse_with_mapping_gives_real_methods_and_no_minified_notes():
    src = android.parse_interface(FIXTURE.read_text(encoding="utf-8"), annotations=MAPPING)
    e = ep(src, "minified")
    assert (e.method, e.path, e.id) == ("GET", "/api/v1/minified/{id}", "GET /api/v1/minified/{id}")
    assert locs(e) == {"id": ("path", True), "limit": ("query", True)}
    assert "minified" not in e.description and "minified" not in src.description
    assert ep(src, "minifiedHttp").method == "PURGE"


def test_overloads_and_same_named_minified_methods_get_unique_ids():
    java = """
    interface Api {
        @GET("api/items") Call<A> list(@Query("a") String a);
        @GET("api/items") Call<A> list(@Query("b") String b);
        @q("api/x") Call<A> m();
        @q("api/y") Call<A> m();
    }
    """
    s = android.parse_interface(java)
    ids = [e.id for e in s.endpoints]
    assert len(ids) == 4 and len(set(ids)) == 4
    assert [s.endpoint(i).params[0].name for i in ids[:2]] == ["a", "b"]
    assert [s.endpoint(i).path for i in ids[2:]] == ["/api/x", "/api/y"]


def test_dropped_methods_leave_a_note():
    java = """
    interface Api {
        @GET("api/ok") Call<A> ok();
        @Foo("not a path!") Call<A> odd();
        @GET("api/broken" Call<A> broken(;
    }
    """
    s = android.parse_interface(java)
    assert [e.summary for e in s.endpoints] == ["ok"]
    assert "skipped odd" in s.description and "skipped" in s.description.splitlines()[-1]
    assert len(s.description.splitlines()) == 2


def test_absolute_url_userinfo_is_stripped():
    s = android.parse_interface('interface Api { @GET("https://u:p@h.test:8443/x?y=1") Call<A> f(); }')
    e = s.endpoints[0]
    assert e.base_url == "https://h.test:8443" and e.path == "/x" and "u:p" not in s.model_dump_json()


# ---------- JADX driver ----------


@pytest.fixture(autouse=True)
def no_jadx_cli(monkeypatch):
    monkeypatch.setattr(jadx, "_jadx_cli", lambda: None)  # the jar path, whatever this machine has on PATH


def fake_jadx(monkeypatch, handler):
    @contextlib.asynccontextmanager
    async def session(apk, jar):
        async def call(tool, args):
            return handler(tool, args)

        yield call

    monkeypatch.setattr(jadx, "_session", session)


@pytest.fixture
def apk(tmp_path):
    f = tmp_path / "app.apk"
    f.write_bytes(b"PK")
    return f


def test_search_pages_through_every_result(monkeypatch, apk):
    monkeypatch.setattr(jadx, "PAGE", 2)
    names = ["a.A", "a.B", "a.C", "a.D", "a.E"]
    seen = []

    def handler(tool, args):
        seen.append((args["offset"], args["max_scan"]))
        items = [{"fqn": n} for n in names[args["offset"] : args["offset"] + 2]]
        return json.dumps({"items": items, "max_scan_reached": False})

    fake_jadx(monkeypatch, handler)
    assert jadx.find_api_classes(apk) == names
    assert seen[:3] == [(0, jadx.MAX_SCAN), (2, jadx.MAX_SCAN), (4, jadx.MAX_SCAN)]


def test_no_candidates_is_a_dead_end(monkeypatch, apk):
    fake_jadx(monkeypatch, lambda t, a: json.dumps({"items": [], "max_scan_reached": False}))
    with pytest.raises(RuntimeError, match="mcpsmith browser") as e:
        jadx.find_api_classes(apk)
    assert "signin" not in str(e.value)


def zip_apk(path, files: dict[str, bytes]):
    with zipfile.ZipFile(path, "w") as z:
        for name, data in files.items():
            z.writestr(name, data)
    return path


RETROFIT_DEX = b"dex\n035\x00 Lretrofit2/http/GET; "


@pytest.mark.parametrize(
    ("files", "needle"),
    [
        ({"lib/arm64-v8a/libflutter.so": b"", "classes.dex": RETROFIT_DEX}, "Flutter"),
        ({"assets/index.android.bundle": android.HERMES_MAGIC + b"..", "classes.dex": b""}, "Hermes bytecode"),
        ({"assets/index.android.bundle": b"var a=1;", "classes.dex": b""}, "mcpsmith app-js bundle.js"),
        ({"classes.dex": b"dex Lcom/x/Util; "}, "record its traffic"),
        ({"classes.dex": b"x", "classes2.dex": b"Lcom/xiaomi/smarthome/Net; "}, "provider: xiaomi"),
    ],
)
def test_triage_answers_before_any_decompile(monkeypatch, tmp_path, files, needle):
    monkeypatch.setattr(jadx, "_tree", lambda *a: pytest.fail("decompiled"))
    monkeypatch.setattr(jadx, "_run", lambda *a: pytest.fail("decompiled"))
    with pytest.raises(RuntimeError, match=needle):
        jadx.find_api_classes(zip_apk(tmp_path / "app.apk", files))


def test_triage_reads_retrofit_even_when_renamed(tmp_path):
    renamed = {
        "classes.dex": b"... HTTP method annotation is required (e.g., @GET, @POST, etc.). Lcom/xiaomi/smarthome/A;"
    }
    t = android.triage(zip_apk(tmp_path / "a.apk", renamed))
    assert t == android.Triage("", True, ("Xiaomi Mi Home", "xiaomi"))
    assert android.triage(tmp_path / "missing.apk") == android.Triage()


def test_search_that_hit_the_scan_cap_is_an_error(monkeypatch, apk):
    fake_jadx(monkeypatch, lambda t, a: json.dumps({"items": [], "scanned": 5000, "max_scan_reached": True}))
    with pytest.raises(RuntimeError, match="5000"):
        jadx.find_api_classes(apk)


def test_class_source_and_builder_annotations(monkeypatch, apk):
    builder = (FIXTURES / "builder.java").read_text(encoding="utf-8")

    def handler(tool, args):
        if tool == "search_classes_by_keyword":
            return json.dumps({"items": [{"fqn": "q8.q"}], "max_scan_reached": False})
        return builder if args["class_name"] == "q8.q" else "interface Api {}"

    fake_jadx(monkeypatch, handler)
    assert jadx.class_source(apk, "com.x.Api") == "interface Api {}"
    assert jadx.builder_annotations(apk)["f"] == "GET"


def test_bad_class_name_and_missing_apk(monkeypatch, apk, tmp_path):
    with pytest.raises(ValueError):
        jadx.class_source(apk, "--evil")
    monkeypatch.setenv("REA_JADX_MCP_JAR", str(tmp_path / "jar.jar"))
    (tmp_path / "jar.jar").write_bytes(b"x")
    with pytest.raises(ValueError):
        jadx.class_source(tmp_path / "no.apk", "a.B")


def test_missing_jar_and_java_say_what_to_do(monkeypatch, apk, tmp_path):
    monkeypatch.delenv("REA_JADX_MCP_JAR", raising=False)
    with pytest.raises(RuntimeError, match="jadx/releases.*rea-agents setup.*--jadx-jar"):
        jadx.class_source(apk, "a.B")
    jar = tmp_path / "j.jar"
    jar.write_bytes(b"x")
    monkeypatch.delenv("JAVA_HOME", raising=False)
    monkeypatch.setattr(jadx.shutil, "which", lambda _: None)
    with pytest.raises(RuntimeError, match="Java"):
        jadx.class_source(apk, "a.B", jar=str(jar))


# ---------- CLI ----------


def test_cli_lists_candidates_then_writes_source(apk, tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(jadx, "api_classes_and_signer", lambda a, j=None: (["com.x.Api"], None))
    monkeypatch.setattr(jadx, "class_source", lambda a, c, j=None: FIXTURE.read_text(encoding="utf-8"))
    monkeypatch.setattr(jadx, "builder_annotations", lambda a, j=None: MAPPING)
    wd = str(tmp_path / "wd")
    assert main(["app-android", str(apk), "-w", wd]) == 0
    assert "com.x.Api" in capsys.readouterr().out and not (tmp_path / "wd" / "source.json").exists()
    assert main(["app-android", str(apk), "--class", "com.x.Api", "--base-url", "https://demo.test", "-w", wd]) == 0
    assert "mcpsmith probe" in capsys.readouterr().out
    saved = json.loads((tmp_path / "wd" / "source.json").read_text(encoding="utf-8"))
    assert saved["kind"] == "app" and (tmp_path / "wd" / "digest.md").exists()
    assert next(e for e in saved["endpoints"] if e["summary"] == "minified")["method"] == "GET"  # mapping applied


def test_cli_source_file_needs_no_jar(tmp_path, monkeypatch):
    monkeypatch.delenv("REA_JADX_MCP_JAR", raising=False)
    wd = tmp_path / "wd"
    assert main(["app-android", "--source", str(FIXTURE), "-w", str(wd)]) == 0
    saved = json.loads((wd / "source.json").read_text(encoding="utf-8"))
    assert saved["endpoints"] and any(e["method"] == "" for e in saved["endpoints"])  # no apk, no mapping


def test_cli_needs_an_apk_or_a_source_and_rejects_empty_interfaces(apk, tmp_path):
    assert main(["app-android", "-w", str(tmp_path / "wd")]) == 2
    plain = tmp_path / "X.java"
    plain.write_text("class X {}", encoding="utf-8")
    assert main(["app-android", "--source", str(plain), "-w", str(tmp_path / "wd")]) == 2


@pytest.mark.live
def test_live_real_apk():
    apk, jar = os.environ.get("MCPSMITH_TEST_APK"), os.environ.get("REA_JADX_MCP_JAR")
    if not (apk and jar):
        pytest.skip("set MCPSMITH_TEST_APK and REA_JADX_MCP_JAR")
    classes = jadx.find_api_classes(Path(apk))
    sources = {c: jadx.class_source(Path(apk), c) for c in classes}
    java = max(sources.values(), key=lambda s: s.count("@"))
    src = android.parse_interface(java, annotations=jadx.builder_annotations(Path(apk)))
    assert len(src.endpoints) >= 110
    assert all(e.method for e in src.endpoints)
    assert any(e.method != "GET" for e in src.endpoints)


def test_unresolved_parameter_keeps_the_method_and_stays_out_of_the_request():
    s = android.parse_interface('interface A { @GET("api/items") Call<X> list(@zz("limit") int limit); }')
    e = s.endpoints[0]
    assert e.method == "GET" and not e.params and "limit" in s.description


def test_notes_never_carry_source_secrets_and_are_capped():
    from mcpsmith.design import digest

    java = 'interface A { @GET(BASE + "/x?api_key=sk_live_123") Call<X> a(); }'
    s = android.parse_interface(
        java + "interface B { " + "".join(f"@GET(P{i}) Call<X> m{i}();" for i in range(300)) + "}"
    )
    assert "sk_live_123" not in s.model_dump_json() and "sk_live_123" not in digest(s)
    assert len(s.description.splitlines()) == 201 and "more" in s.description.splitlines()[-1]
