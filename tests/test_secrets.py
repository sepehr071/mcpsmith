import httpx
import keyring
import pytest
from keyring.backend import KeyringBackend
from keyring.errors import PasswordDeleteError

from toolsmelt import secrets
from toolsmelt.cli import main
from toolsmelt.runtime import Api, ApiError

pytestmark = pytest.mark.anyio


class MemoryKeyring(KeyringBackend):
    priority = 1

    def __init__(self):
        self.data = {}

    def get_password(self, service, username):
        return self.data.get((service, username))

    def set_password(self, service, username, password):
        self.data[(service, username)] = password

    def delete_password(self, service, username):
        try:
            del self.data[(service, username)]
        except KeyError:
            raise PasswordDeleteError(username) from None


@pytest.fixture(autouse=True)
def memory_keyring():
    old = keyring.get_keyring()
    backend = MemoryKeyring()
    keyring.set_keyring(backend)
    yield backend
    keyring.set_keyring(old)


@pytest.fixture
def anyio_backend():
    return "asyncio"


def test_set_get_delete(memory_keyring):
    secrets.set("shop", "TOKEN", "abc")
    assert secrets.get("shop", "TOKEN") == "abc"
    assert secrets.get("shop", "TOKEN") == "abc"
    assert ("mcpsmith/shop", "TOKEN") in memory_keyring.data  # pre-rename service name: stored secrets keep working
    secrets.delete("shop", "TOKEN")
    assert secrets.get("shop", "TOKEN") is None
    secrets.delete("shop", "TOKEN")  # deleting twice is fine
    assert secrets.get("other", "TOKEN") is None


def test_once_is_read_once():
    secrets.set("shop", "OTP", "123456", once=True)
    assert secrets.get("shop", "OTP") == "123456"
    assert secrets.get("shop", "OTP") is None
    assert secrets.names("shop") == []


def test_names_lists_without_values():
    secrets.set("shop", "B", "vb")
    secrets.set("shop", "A", "va")
    assert secrets.names("shop") == ["A", "B"]
    secrets.delete("shop", "B")
    assert secrets.names("shop") == ["A"]
    with pytest.raises(ValueError):
        secrets.set("shop", "__names__", "x")


def test_cli_secret_flow(monkeypatch, capsys):
    monkeypatch.setattr("getpass.getpass", lambda prompt="": "s3cret-value")
    assert main(["secret", "set", "shop", "TOKEN"]) == 0
    assert main(["secret", "list", "shop"]) == 0
    assert secrets.get("shop", "TOKEN") == "s3cret-value"
    out = capsys.readouterr()
    assert "TOKEN" in out.out and "s3cret-value" not in out.out + out.err
    assert main(["secret", "delete", "shop", "TOKEN"]) == 0
    assert secrets.names("shop") == []


async def test_api_uses_secret_when_env_unset(monkeypatch):
    monkeypatch.delenv("X_MCP_TOKEN", raising=False)
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, json={})

    api = Api(
        base_url="https://api.example.com",
        env_prefix="X_MCP",
        auth={"type": "bearer", "env": "X_MCP_TOKEN"},
        secret=lambda n: "s3",
    )
    api.set_transport(httpx.MockTransport(handler))
    await api.call("GET", "/x")
    assert seen[-1].headers["authorization"] == "Bearer s3"
    monkeypatch.setenv("X_MCP_TOKEN", "fromenv")
    await api.call("GET", "/y")
    assert seen[-1].headers["authorization"] == "Bearer fromenv"


async def test_once_secret_401_reports_rejected_and_is_not_reread(monkeypatch):
    monkeypatch.delenv("X_MCP_TOKEN", raising=False)
    secrets.set("shop", "X_MCP_TOKEN", "otp", once=True)
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(401)

    api = Api(
        base_url="https://api.example.com",
        env_prefix="X_MCP",
        auth={"type": "bearer", "env": "X_MCP_TOKEN"},
        secret=lambda n: secrets.get("shop", n),
    )
    api.RETRY_DELAY = 0
    api.set_transport(httpx.MockTransport(handler))
    with pytest.raises(ApiError, match="rejected the credentials"):
        await api.call("GET", "/x")
    assert seen[0].headers["authorization"] == "Bearer otp"
    assert secrets.get("shop", "X_MCP_TOKEN") is None


class NoBackend(KeyringBackend):
    priority = 1

    def get_password(self, service, username):
        raise keyring.errors.NoKeyringError("no backend")

    set_password = delete_password = get_password


def test_no_keychain_get_is_none_and_cli_explains(capsys):
    keyring.set_keyring(NoBackend())
    assert secrets.get("shop", "TOKEN") is None
    assert main(["secret", "list", "shop"]) == 2
    err = capsys.readouterr().err
    assert err.startswith("toolsmelt: error:") and "environment variable" in err
