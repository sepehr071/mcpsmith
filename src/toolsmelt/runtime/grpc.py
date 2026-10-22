"""gRPC services as a source: `Grpc` calls a plan's unary methods (make_api builds it for a grpc:// or grpcs://
base_url). Message types come from the server's reflection service; requests and responses are plain JSON.

grpcio, grpcio-reflection and protobuf (the `toolsmelt[grpc]` extra) are imported on first use, so HTTP-only
installs never need them.
"""

from __future__ import annotations

import asyncio
import base64
import collections
import functools
import os
import ssl
import sys
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from .http import ApiError

GRPC_NEEDED = "gRPC sources need grpcio: pip install 'toolsmelt[grpc]'"
AUTH_TYPES = {"bearer", "basic", "header"}  # credentials gRPC can carry: metadata only
# status code name -> (the HTTP status it reads as, what it means for the caller)
STATUS = {
    "INVALID_ARGUMENT": (400, "the server rejected the arguments"),
    "FAILED_PRECONDITION": (400, "the server refused the call in its current state"),
    "UNAUTHENTICATED": (401, "missing or invalid credential"),
    "PERMISSION_DENIED": (403, "the credential may not call this method"),
    "NOT_FOUND": (404, "not found"),
    "ALREADY_EXISTS": (409, "already exists"),
    "RESOURCE_EXHAUSTED": (429, "rate limited or over quota; wait and retry"),
    "UNIMPLEMENTED": (501, "the server does not offer this method"),
    "UNAVAILABLE": (503, "the server is unreachable or down; check the address and that it runs"),
    "DEADLINE_EXCEEDED": (504, "no answer in time"),
}


def is_grpc(url: str) -> bool:
    """True for grpc://host:port (plaintext) and grpcs://host:port (TLS)."""
    return url.strip().lower().startswith(("grpc://", "grpcs://"))


def address(url: str) -> tuple[str, bool]:
    """(host:port, uses TLS) of a grpc:// or grpcs:// URL; the port defaults to 443 with TLS, else 80."""
    parts = urlsplit(url.strip())
    if not is_grpc(url) or not parts.hostname:
        raise ValueError(f"{url!r} is not a gRPC URL; write grpc://host:port or grpcs://host:port")
    secure = parts.scheme.lower() == "grpcs"
    host = f"[{parts.hostname}]" if ":" in parts.hostname else parts.hostname
    return f"{host}:{parts.port or (443 if secure else 80)}", secure


@functools.cache
def os_roots() -> bytes | None:
    """The OS trust store as PEM for gRPC's TLS (it bundles its own roots otherwise); None to use those."""
    try:
        if sys.platform == "win32":
            ders = [cert for cert, enc, _ in ssl.enum_certificates("ROOT") if enc == "x509_asn"]
            pem = "".join(ssl.DER_cert_to_PEM_cert(d) for d in ders)
        else:
            path = ssl.get_default_verify_paths().cafile
            pem = Path(path).read_text(encoding="ascii") if path else ""
    except (OSError, ValueError, ssl.SSLError):
        return None
    return pem.encode() or None


def _with_metadata(grpc: Any, metadata: Callable[[], list[tuple[str, str]]]) -> Any:
    """A client interceptor adding `metadata()` to streaming calls: reflection then carries the credential too."""

    class Details(
        collections.namedtuple("Details", "method timeout metadata credentials wait_for_ready compression"),
        grpc.ClientCallDetails,
    ):
        pass

    class Interceptor(grpc.StreamStreamClientInterceptor):
        def intercept_stream_stream(self, continuation, details, requests):
            extra = [*(details.metadata or []), *metadata()]
            keep = (getattr(details, f, None) for f in ("credentials", "wait_for_ready", "compression"))
            return continuation(Details(details.method, details.timeout, extra, *keep), requests)

    return Interceptor()


def open_channel(
    url: str, metadata: Callable[[], list[tuple[str, str]]] | None = None, proxy: str | None = None
) -> Any:
    """A channel to the server of `url`: TLS with the OS roots for grpcs://, plaintext for grpc://. Only an explicit
    `proxy` (HTTP CONNECT) is used; proxy environment variables are ignored, like the HTTP client does."""
    import grpc

    addr, secure = address(url)
    options = [("grpc.http_proxy", proxy)] if proxy else [("grpc.enable_http_proxy", 0)]
    if secure:
        channel = grpc.secure_channel(addr, grpc.ssl_channel_credentials(os_roots()), options)
    else:
        channel = grpc.insecure_channel(addr, options)
    return grpc.intercept_channel(channel, _with_metadata(grpc, metadata)) if metadata else channel


WELL_KNOWN = ("any", "api", "duration", "empty", "field_mask", "source_context", "struct", "timestamp", "type")


def reflection_pool(channel: Any) -> tuple[Any, Any]:
    """(reflection database, descriptor pool) for `channel`. The google/protobuf/*.proto files come from the local
    protobuf library: they are identical everywhere, and some servers never answer reflection requests for them."""
    import importlib

    from google.protobuf import descriptor_pb2, descriptor_pool
    from grpc_reflection.v1alpha.proto_reflection_descriptor_database import ProtoReflectionDescriptorDatabase

    for name in (*WELL_KNOWN, "wrappers"):
        importlib.import_module(f"google.protobuf.{name}_pb2")  # registers it in the default pool

    class Database(ProtoReflectionDescriptorDatabase):
        def FindFileByName(self, name):  # noqa: N802 (the protobuf DescriptorDatabase interface)
            if name.startswith("google/protobuf/"):
                try:
                    proto = descriptor_pb2.FileDescriptorProto()
                    descriptor_pool.Default().FindFileByName(name).CopyToProto(proto)
                    return proto
                except KeyError:
                    pass
            return super().FindFileByName(name)

    db = Database(channel)
    return db, descriptor_pool.DescriptorPool(db)


def rpc_error(e: Any, what: str, env: str = "") -> ApiError:
    """A clear ApiError for a failed call: the status name, what it means, and the server's own message."""
    code = e.code().name if callable(getattr(e, "code", None)) and e.code() else "UNKNOWN"
    status, hint = STATUS.get(code, (502, "the call failed"))
    if code == "UNAUTHENTICATED" and env:
        hint += f"; set {env}"
    details = (e.details() if callable(getattr(e, "details", None)) else "") or ""
    return ApiError(f"{what}: {code} ({hint}){': ' + details[:300] if details else ''}", status)


class Grpc:
    """One gRPC server for a plan's tools, the `Api` of a grpc:// or grpcs:// base_url. `<env_prefix>_BASE_URL`
    overrides the URL. Metadata is the plan's headers, the header.* values and the credential from `auth.env`
    (the environment, then the keychain-backed `secret` callback). The channel opens on the first call and the
    message types are read by reflection once."""

    TIMEOUT = 30  # seconds per call (the deadline)

    def __init__(
        self,
        base_url: str,
        env_prefix: str,
        headers: dict[str, str] | None = None,
        auth: dict[str, str] | None = None,
        secret: Callable[[str], str | None] | None = None,
        proxy: str | None = None,
    ):
        if auth and (auth.get("type") not in AUTH_TYPES or not auth.get("env")):
            raise ValueError(f"gRPC auth needs type (one of {sorted(AUTH_TYPES)}) and env, got {auth!r}")
        if auth and auth["type"] == "header" and not auth.get("param"):
            raise ValueError("gRPC header auth needs param: the metadata key that carries the credential")
        self.env_prefix = env_prefix
        self.base_url = os.environ.get(f"{env_prefix}_BASE_URL") or base_url
        address(self.base_url)  # fail at start on a bad URL, not on the first call
        self.headers = dict(headers or {})
        self.auth = auth
        self.secret = secret
        # an explicit proxy, else <P>_PROXY (the first of a comma list: a channel keeps one connection)
        self.proxy = proxy or next(
            (p.strip() for p in os.environ.get(f"{env_prefix}_PROXY", "").split(",") if p.strip()), None
        )
        self._channel: Any = None
        self._db: Any = None
        self._pool: Any = None
        self._lock = threading.Lock()

    def _credential(self) -> list[tuple[str, str]]:
        if not self.auth:
            return []
        env, kind = self.auth["env"], self.auth["type"]
        secret = (os.environ.get(env) or (self.secret and self.secret(env)) or "").strip()
        if not secret:
            return []  # a public method still works; UNAUTHENTICATED then names the variable to set
        if kind == "bearer":
            return [("authorization", f"Bearer {secret}")]
        if kind == "basic":  # env value is "user:password"
            return [("authorization", "Basic " + base64.b64encode(secret.encode()).decode())]
        return [(self.auth["param"].lower(), secret)]

    def method(self, name: str) -> Any:
        """The MethodDescriptor of 'package.Service/Method', read by reflection (cached by the pool)."""
        service, _, rpc = name.strip("/").partition("/")
        with self._lock:
            if self._channel is None:
                self._channel = open_channel(self.base_url, self._credential, self.proxy)
                self._db, self._pool = reflection_pool(self._channel)
            try:
                desc = self._pool.FindServiceByName(service).methods_by_name[rpc]
            except KeyError:
                raise ApiError(f"{name}: the server's reflection does not list this method.", 404) from None
        if desc.client_streaming or desc.server_streaming:
            raise ApiError(f"{name} is a streaming method; only unary methods can be tools.")
        return desc

    async def call(
        self,
        method: str,
        url: str,
        path: dict[str, Any] | None = None,
        query: dict[str, Any] | None = None,
        body: Any = None,
        headers: dict[str, Any] | None = None,
        cookies: dict[str, Any] | None = None,
        encoding: str = "json",
    ) -> Any:
        """Call the unary method `url` ('package.Service/Method') with the body as its request message; return the
        response message as a dict. The same signature as `Api.call`, so run_tool drives both."""
        if path or query or cookies:
            raise ApiError(f"{url}: gRPC takes body.<field> and header.<key> values only, not path/query/cookie.")
        if body is not None and not isinstance(body, dict):
            raise ApiError(f"{url}: the request must be an object of message fields, not {type(body).__name__}.")
        try:
            import grpc  # noqa: F401
        except ImportError:
            raise ApiError(GRPC_NEEDED) from None
        return await asyncio.to_thread(self.invoke, url, body or {}, headers or {})

    def invoke(self, name: str, body: dict[str, Any], headers: dict[str, Any]) -> Any:
        import grpc
        from google.protobuf import json_format, message_factory

        try:
            desc = self.method(name)
        except grpc.RpcError as e:
            raise rpc_error(e, f"{name} (reflection)", (self.auth or {}).get("env", "")) from None
        request_cls = message_factory.GetMessageClass(desc.input_type)
        response_cls = message_factory.GetMessageClass(desc.output_type)
        try:
            request = json_format.ParseDict(body, request_cls(), ignore_unknown_fields=False)
        except json_format.ParseError as e:
            raise ApiError(f"{name}: invalid request: {e}", 400) from None
        metadata = [
            *((k.lower(), str(v)) for k, v in {**self.headers, **headers}.items() if v is not None),
            *self._credential(),
        ]
        stub = self._channel.unary_unary(
            f"/{desc.containing_service.full_name}/{desc.name}",
            request_serializer=request_cls.SerializeToString,
            response_deserializer=response_cls.FromString,
        )
        try:
            response = stub(request, timeout=self.TIMEOUT, metadata=metadata)
        except grpc.RpcError as e:
            raise rpc_error(e, name, (self.auth or {}).get("env", "")) from None
        return json_format.MessageToDict(response, preserving_proto_field_name=True)
