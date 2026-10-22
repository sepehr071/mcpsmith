import json
import logging
from pathlib import Path
from types import SimpleNamespace

import pytest

from toolsmelt import importers, plugins
from toolsmelt.design import validate
from toolsmelt.model import Plan, Source
from toolsmelt.runtime import signin, transforms
from toolsmelt.runtime.shape import apply_transforms

FIXTURES = Path(__file__).parent / "fixtures"


class FakeEntryPoint:
    def __init__(self, name, obj, group):
        self.name, self.obj, self.group, self.value = name, obj, group, f"fake:{name}"

    def load(self):
        if isinstance(self.obj, Exception):
            raise self.obj
        return self.obj


def _clear():
    plugins.load.cache_clear()
    transforms._tables.cache_clear()
    signin._providers.cache_clear()


@pytest.fixture
def install(monkeypatch):
    """install(group, name, obj): pretend an installed package declares that entry point."""
    eps: list[FakeEntryPoint] = []
    monkeypatch.setattr(plugins, "entry_points", lambda group: [e for e in eps if e.group == group])
    _clear()
    yield lambda group, name, obj: eps.append(FakeEntryPoint(name, obj, group))
    _clear()  # later tests see the real (empty) plugin set


class Postman:
    name = "postman"

    def sniff(self, path, data):
        return isinstance(data, dict) and "postman" in str(data.get("info", {}).get("schema", ""))

    def load(self, path, data, **opts):
        return Source(kind="postman", title=data["info"]["name"], base_url="https://api.example.com")


def test_importer_claims_new_format(install, tmp_path):
    install("toolsmelt.importers", "postman", Postman())
    f = tmp_path / "c.json"
    f.write_text(json.dumps({"info": {"name": "Shop", "schema": "https://schema.getpostman.com/v2.1"}}), "utf-8")
    src = importers.load(str(f))
    assert (src.kind, src.title) == ("postman", "Shop")
    # built-in formats keep precedence even over a plugin that claims everything
    install("toolsmelt.importers", "greedy", SimpleNamespace(sniff=lambda p, d: True, load=None))
    plugins.load.cache_clear()
    assert importers.load(str(FIXTURES / "openapi" / "petstore2.yaml")).kind == "openapi"


def test_importer_sees_unparsed_files(install, tmp_path):
    seen = []
    curl = SimpleNamespace(
        sniff=lambda p, d: seen.append(d) or p.suffix == ".bin",
        load=lambda p, d, **o: Source(kind="bin", title=p.name, base_url="https://x.example"),
    )
    install("toolsmelt.importers", "bin", curl)
    f = tmp_path / "x.bin"
    f.write_bytes(b"\xff\xfe\x00binary")
    assert importers.load(str(f)).kind == "bin"
    assert seen == [None]
    g = tmp_path / "x.yaml"
    g.write_text("hello: world\n", encoding="utf-8")
    with pytest.raises(ValueError, match="neither"):  # unclaimed: the old error
        importers.load(str(g))


def test_transform_plugin_validates_and_runs(install, caplog):
    install(
        "toolsmelt.transforms",
        "money",
        SimpleNamespace(
            INPUT={"units->cents": lambda v, tz: round(float(v) * 100)},
            OUTPUT={"cents->units": lambda v, tz: v / 100, "unix->date": lambda v, tz: "hijacked"},
        ),
    )
    with caplog.at_level(logging.WARNING, logger="toolsmelt"):
        assert "cents->units" in transforms.OUTPUT_NAMES and "units->cents" in transforms.INPUT_NAMES
    assert "already taken" in caplog.text  # cannot override a built-in
    assert transforms.transform_out("unix->date", 1792454400, "UTC") == "2026-10-20"
    plan = Plan.model_validate(
        {
            "name": "shop-mcp",
            "title": "Shop",
            "description": "Search the shop.",
            "instructions": "Use shop_get.",
            "prefix": "shop",
            "base_url": "https://shop.example.com",
            "env_prefix": "SHOP_MCP",
            "tools": [
                {
                    "name": "shop_get",
                    "title": "Get",
                    "description": "Get products under a price.",
                    "endpoint": "getProduct",
                    "args": [{"name": "max", "description": "Euros.", "to": "query.max", "transform": "units->cents"}],
                    "output": {"fields": {"price": "price"}, "transforms": {"price": "cents->units"}},
                }
            ],
        }
    )
    assert not [i for i in validate(plan) if "transform" in i.message]
    assert transforms.transform_in("units->cents", "1.25") == 125
    assert apply_transforms({"price": 1250}, plan.tools[0].output.transforms) == {"price": 12.5}


def _provider(**overrides):
    attrs = {k: None for k in ("TITLE", "ACCOUNT", "USER_AGENT", "HEADERS", "TEST_SESSION", "LOGIN_MINUTES")}
    for fn in ("session_of", "cookies", "encrypt", "decrypt", "error", "fake_reply", "start_login", "finish_login"):
        attrs[fn] = lambda *a: None
    attrs["whoami"] = lambda *a: None
    return SimpleNamespace(**{**attrs, **overrides})


def test_signin_plugin_found(install, caplog):
    acme = _provider(TITLE="Acme")
    install("toolsmelt.signin", "acme", acme)
    install("toolsmelt.signin", "xiaomi", _provider(TITLE="fake"))  # cannot replace a built-in
    install("toolsmelt.signin", "half", SimpleNamespace(TITLE="Half"))  # misses most of the interface
    with caplog.at_level(logging.WARNING, logger="toolsmelt"):
        assert signin.provider("acme") is acme
        assert sorted(signin.PROVIDERS) == ["acme", "xiaomi"]
    assert signin.provider("xiaomi").TITLE == "Xiaomi"
    assert "half" in caplog.text and "Provider interface" in caplog.text


def test_broken_plugin_warns_and_is_skipped(install, caplog, tmp_path):
    install("toolsmelt.importers", "broken", ImportError("No module named 'missing_dep'"))
    install("toolsmelt.importers", "bad_sniff", SimpleNamespace(sniff=lambda p, d: 1 / 0))
    install("toolsmelt.importers", "postman", Postman())
    f = tmp_path / "c.json"
    f.write_text(json.dumps({"info": {"name": "Shop", "schema": "postman"}}), "utf-8")
    with caplog.at_level(logging.WARNING, logger="toolsmelt"):
        assert list(plugins.load("toolsmelt.importers")) == ["bad_sniff", "postman"]
        assert importers.load(str(f)).kind == "postman"  # a failing sniff does not block the next plugin
    assert "missing_dep" in caplog.text and "sniff failed" in caplog.text
