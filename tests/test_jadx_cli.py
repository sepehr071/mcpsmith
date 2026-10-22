import hashlib
import subprocess
from pathlib import Path

import pytest

from toolsmelt.importers import jadx

FIXTURES = Path(__file__).parent / "fixtures" / "android"
TREE = {
    "com/x/Api.java": (FIXTURES / "interface.java").read_text(encoding="utf-8"),
    "q8/q.java": (FIXTURES / "builder.java").read_text(encoding="utf-8") + f"// {jadx.BUILDER_MARK}",
    "com/x/Util.java": "class Util {}",
}


@pytest.fixture
def apk(tmp_path):
    f = tmp_path / "app.apk"
    f.write_bytes(b"PK")
    return f


@pytest.fixture
def fake_cli(monkeypatch, tmp_path):
    """A `jadx` on PATH that writes `tree` under -d DIR/sources; returns the list of its runs."""
    runs = Runs()
    tree = dict(TREE)

    def run(args, cwd, **_):
        runs.append((args, cwd))
        runs.apk_bytes = (Path(cwd) / args[-1]).read_bytes()  # the copy exists while jadx runs
        out = Path(cwd) / args[args.index("-d") + 1] / "sources"
        for rel, text in tree.items():
            (out / rel).parent.mkdir(parents=True, exist_ok=True)
            (out / rel).write_text(text, encoding="utf-8")
        return subprocess.CompletedProcess(args, 1, "", "ERROR - finished with errors")

    monkeypatch.setattr(jadx, "_jadx_cli", lambda: "jadx")
    monkeypatch.setattr(jadx.subprocess, "run", run)
    monkeypatch.setattr(jadx, "CACHE", tmp_path / "cache")
    monkeypatch.delenv("REA_JADX_MCP_JAR", raising=False)
    runs.tree = tree
    return runs


class Runs(list):
    tree: dict
    apk_bytes: bytes


def test_cli_finds_reads_and_maps_with_one_decompile(fake_cli, apk, tmp_path):
    assert jadx.find_api_classes(apk) == ["com.x.Api"]
    assert "homeTimeline" in jadx.class_source(apk, "com.x.Api")
    assert jadx.class_source(apk, "com.x.Api$Inner") == jadx.class_source(apk, "com.x.Api")
    assert jadx.builder_annotations(apk)["f"] == "GET"
    assert len(fake_cli) == 1  # cached per APK sha256
    args, cwd = fake_cli[0]
    sha = hashlib.sha256(apk.read_bytes()).hexdigest()
    assert args[1:] == ["-d", args[2], "--show-bad-code", "--no-res", f"{sha}.apk"]
    assert (cwd / args[2]).parent == tmp_path / "cache" and cwd == tmp_path / "cache"  # never next to the APK
    with pytest.raises(RuntimeError, match="no class com.x.Missing"):
        jadx.class_source(apk, "com.x.Missing")


def test_cli_never_sees_the_apk_name(fake_cli, tmp_path):
    # jadx.bat runs through cmd.exe: an `&` in the file name would run the rest as a command
    apk = tmp_path / "a&ver&.apk"
    apk.write_bytes(b"PK")
    jadx.find_api_classes(apk)
    args, cwd = fake_cli[0]
    assert not any("&" in a for a in args) and fake_cli.apk_bytes == b"PK"
    assert not list(cwd.glob("*.apk"))  # the copy is gone afterwards


def test_cli_is_skipped_for_an_explicit_jar(fake_cli, apk):
    assert jadx._tree(apk, "some.jar") is None and not fake_cli


def test_without_the_cli_the_jar_is_used_and_missing_both_names_both(monkeypatch, apk):
    monkeypatch.setattr(jadx, "_jadx_cli", lambda: None)
    assert jadx._tree(apk, None) is None
    monkeypatch.delenv("REA_JADX_MCP_JAR", raising=False)
    with pytest.raises(RuntimeError, match="scoop install extras/jadx.*REA_JADX_MCP_JAR"):
        jadx.find_api_classes(apk)


def test_cli_with_no_output_is_an_error_and_leaves_nothing(monkeypatch, apk, tmp_path):
    monkeypatch.setattr(jadx, "_jadx_cli", lambda: "jadx")
    monkeypatch.setattr(jadx, "CACHE", tmp_path / "cache")
    monkeypatch.setattr(jadx.subprocess, "run", lambda args, **_: subprocess.CompletedProcess(args, 1, "", "bad apk"))
    with pytest.raises(RuntimeError, match="bad apk"):
        jadx.find_api_classes(apk)
    assert not any((tmp_path / "cache").iterdir())


def test_cli_dead_end_names_a_known_signer_from_class_names(fake_cli, apk):
    fake_cli.tree.clear()
    fake_cli.tree["com/xiaomi/smarthome/core/Net.java"] = 'class Net { String k = "0123secret"; }'
    with pytest.raises(RuntimeError, match="signs its requests itself.*Xiaomi Mi Home.*provider: xiaomi") as e:
        jadx.find_api_classes(apk)
    assert "0123secret" not in str(e.value)


def test_candidates_still_name_a_known_signer(fake_cli, apk, tmp_path, capsys):
    from toolsmelt.cli import main

    fake_cli.tree["com/xiaomi/smarthome/core/Net.java"] = "class Net {}"
    assert jadx.api_classes_and_signer(apk) == (["com.x.Api"], ("Xiaomi Mi Home", "xiaomi"))
    assert main(["app-android", str(apk), "-w", str(tmp_path / "wd")]) == 0
    out = capsys.readouterr().out
    assert (
        "com.x.Api" in out and "note: Found Xiaomi Mi Home's request-signing SDK" in out and "provider: xiaomi" in out
    )


def test_cli_dead_end_without_a_signer(fake_cli, apk):
    fake_cli.tree.clear()
    fake_cli.tree["com/x/Util.java"] = "class Util {}"
    with pytest.raises(RuntimeError, match="record its traffic") as e:
        jadx.find_api_classes(apk)
    assert "signin" not in str(e.value)
