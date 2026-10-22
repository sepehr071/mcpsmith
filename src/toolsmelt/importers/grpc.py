"""A gRPC server URL (grpc://host:port plaintext, grpcs://host:port TLS) -> Source, read through server reflection.

One endpoint per unary method ('RPC package.Service/Method'): the request message's fields as body params, a
response skeleton built from the response message, and the proto comments when the server keeps source info.
Streaming methods are skipped with a note. A token in TOOLSMELT_GRPC_TOKEN is sent as a bearer credential while
reading, and never stored.
"""

from __future__ import annotations

import urllib.request
from typing import Any
from urllib.parse import urlsplit

from toolsmelt import getenv
from toolsmelt.model import AuthScheme, Endpoint, Param, Source
from toolsmelt.runtime.grpc import GRPC_NEEDED, address, is_grpc, open_channel, reflection_pool, rpc_error

TIMEOUT = 10  # seconds to connect
SKIP = ("grpc.reflection.",)  # the reflection service itself
DEPTH = 3  # message nesting shown in response skeletons and field lists
# well-known message types and how their JSON looks
WKT = {
    "google.protobuf.Timestamp": "string",
    "google.protobuf.Duration": "string",
    "google.protobuf.FieldMask": "string",
    "google.protobuf.Struct": "object",
    "google.protobuf.ListValue": "array",
    "google.protobuf.Empty": "object",
    "google.protobuf.BoolValue": "boolean",
    "google.protobuf.StringValue": "string",
    "google.protobuf.BytesValue": "string",
    "google.protobuf.DoubleValue": "number",
    "google.protobuf.FloatValue": "number",
    "google.protobuf.Int32Value": "integer",
    "google.protobuf.UInt32Value": "integer",
    "google.protobuf.Int64Value": "integer",
    "google.protobuf.UInt64Value": "integer",
}


def sniff(text: str) -> bool:
    return is_grpc(text)


def parse(url: str, timeout: float = TIMEOUT) -> Source:
    try:
        import grpc
        import grpc_reflection  # noqa: F401  (reflection_pool needs it)
    except ImportError:
        raise ImportError(GRPC_NEEDED) from None
    addr, secure = address(url)
    public = f"{'grpcs' if secure else 'grpc'}://{addr}"  # no user info, path or query
    token = (getenv("GRPC_TOKEN") or "").strip()
    # like inspect's HTTP fetch, reading uses the system proxy; TOOLSMELT_PROXY overrides it
    # shortcut: NO_PROXY is not honoured here; unset the proxy variables when a gRPC host must be reached directly
    sys_proxies = urllib.request.getproxies()
    proxy = getenv("PROXY") or sys_proxies.get("https") or sys_proxies.get("http")
    channels: list[Any] = []

    def connect() -> tuple[Any, Any]:
        """A fresh reflection stream: a server that breaks the stream on one service must not cost the others."""
        if len(channels) > 1:  # close the previous service's channel: a hung stream on it can stall the next one
            channels.pop().close()
        meta = (lambda: [("authorization", f"Bearer {token}")]) if token else None
        channels.append(open_channel(public, meta, proxy))
        return reflection_pool(channels[-1])

    try:
        db, pool = connect()
        try:
            grpc.channel_ready_future(channels[0]).result(timeout=timeout)
        except grpc.FutureTimeoutError:
            raise ValueError(f"{public}: no gRPC server answered within {timeout}s") from None
        try:
            return _read(db, pool, connect, public, bool(token))
        except grpc.RpcError as e:
            if e.code() == grpc.StatusCode.UNIMPLEMENTED:
                raise ValueError(
                    f"{public}: the server has no reflection service; enable gRPC server reflection"
                ) from None
            raise ValueError(str(rpc_error(e, f"{public} reflection", "TOOLSMELT_GRPC_TOKEN"))) from None
    finally:
        for channel in channels:
            channel.close()


def _read(db: Any, pool: Any, connect: Any, public: str, has_token: bool) -> Source:
    import grpc

    names = sorted(s for s in db.get_services() if not s.startswith(SKIP))
    comments: dict[str, str] = {}
    files: set[str] = set()

    def load(file: Any) -> None:  # the comments of the service's own file, read once; they are optional
        if file.name in files:
            return
        files.add(file.name)
        # shortcut: imported files' comments are not read (some servers hang on them); fields defined there get none
        comments.update(_comments(db.FindFileByName(file.name)))

    endpoints, notes = [], []
    for name in names:
        db, pool = connect()  # one stream per service: a server that breaks it on one must not cost the others
        files.clear()
        try:
            service = pool.FindServiceByName(name)
        except (grpc.RpcError, KeyError, TypeError) as e:
            if isinstance(e, grpc.RpcError) and e.code() == grpc.StatusCode.UNIMPLEMENTED:
                raise
            notes.append(f"skipped service {name}: reflection could not read it ({type(e).__name__})")
            continue
        try:
            load(service.file)
        except (grpc.RpcError, KeyError, TypeError):
            notes.append(f"{name}: proto comments not readable by reflection; descriptions are empty")
        for m in service.methods:
            mid = f"{name}/{m.name}"
            if m.client_streaming or m.server_streaming:
                kind = (
                    "bidirectional"
                    if m.client_streaming and m.server_streaming
                    else "client"
                    if m.client_streaming
                    else "server"
                )
                notes.append(f"skipped {mid}: {kind}-streaming (only unary methods become tools)")
                continue
            text = comments.get(m.full_name, "")
            endpoints.append(
                Endpoint(
                    id=f"RPC {mid}",
                    method="RPC",
                    path=mid,
                    summary=text.split("\n")[0].strip() or f"{service.name}.{m.name}",
                    description=" ".join(text.split()),
                    tags=[name],
                    params=[_param(f, comments) for f in m.input_type.fields],
                    response_example=_skeleton(m.output_type, DEPTH),
                )
            )
    count = f"{len(names)} service{'s' * (len(names) != 1)}, {len(endpoints)} unary methods."
    return Source(
        kind="grpc",
        title=_title(names, public),
        description=" ".join([f"gRPC server read by reflection: {count}", *(f"{n}." for n in notes)]),
        base_url=public,
        # The token goes to <env_prefix>_TOKEN (env or keychain) as 'authorization: Bearer' metadata.
        auth=[AuthScheme(name="bearer", type="bearer")] if has_token else [],
        endpoints=endpoints,
    )


def _title(services: list[str], public: str) -> str:
    """The package of the services ('shop.v1' -> 'shop'), else the host."""
    packages = {s.rpartition(".")[0] for s in services}
    if len(packages) == 1 and (pkg := packages.pop()):
        return next((p for p in pkg.split(".") if not p.lower().lstrip("v").isdigit()), pkg)
    return urlsplit(public).hostname or "grpc"


def _comments(proto: Any) -> dict[str, str]:
    """Full name -> comment of each message, field, service and method of a FileDescriptorProto with source info."""
    where = {
        tuple(x.path): (x.leading_comments or x.trailing_comments).strip() for x in proto.source_code_info.location
    }
    out: dict[str, str] = {}
    pkg = f"{proto.package}." if proto.package else ""

    def messages(items: Any, prefix: str, path: tuple) -> None:  # 4 = message_type, 2 = field, 3 = nested_type
        for i, m in enumerate(items):
            name, p = prefix + m.name, (*path, i)
            out[name] = where.get(p, "")
            out.update({f"{name}.{f.name}": where.get((*p, 2, j), "") for j, f in enumerate(m.field)})
            messages(m.nested_type, f"{name}.", (*p, 3))

    messages(proto.message_type, pkg, (4,))
    for i, s in enumerate(proto.service):  # 6 = service, 2 = method
        out[pkg + s.name] = where.get((6, i), "")
        out |= {f"{pkg}{s.name}.{m.name}": where.get((6, i, 2, j), "") for j, m in enumerate(s.method)}
    return {k: v for k, v in out.items() if v}


def _repeated(f: Any) -> bool:
    flag = getattr(f, "is_repeated", None)  # protobuf 6+; `label` before
    return flag if isinstance(flag, bool) else f.label == f.LABEL_REPEATED


def _is_map(f: Any) -> bool:
    return f.message_type is not None and f.message_type.GetOptions().map_entry


def _scalar(f: Any) -> str:
    """The JSON type of one value of a field (repeated or not)."""
    if f.message_type is not None:
        return WKT.get(f.message_type.full_name, "object")
    kinds = {f.TYPE_BOOL: "boolean", f.TYPE_DOUBLE: "number", f.TYPE_FLOAT: "number"}
    ints = {f.TYPE_INT32, f.TYPE_INT64, f.TYPE_UINT32, f.TYPE_UINT64, f.TYPE_SINT32, f.TYPE_SINT64}
    ints |= {f.TYPE_FIXED32, f.TYPE_FIXED64, f.TYPE_SFIXED32, f.TYPE_SFIXED64}
    return kinds.get(f.type) or ("integer" if f.type in ints else "string")  # string, bytes (base64), enum


def _param(f: Any, comments: dict[str, str]) -> Param:
    kind = "object" if _is_map(f) else "array" if _repeated(f) else _scalar(f)
    bits = [_proto_type(f)]
    if f.message_type is not None and not _is_map(f) and f.message_type.full_name not in WKT:
        bits.append(_fields(f.message_type, DEPTH - 1))
    if comment := comments.get(f.full_name):
        bits.append(comment)
    required = bool(getattr(f, "is_required", False)) or getattr(f, "label", None) == f.LABEL_REQUIRED  # proto2 only
    return Param(
        name=f.name,  # the proto name; the request JSON accepts it as well as the lowerCamel name
        location="body",
        type=kind,
        required=required,
        description="; ".join(" ".join(b.split()) for b in bits if b),
        enum=[v.name for v in f.enum_type.values] if f.enum_type is not None and not _repeated(f) else None,
    )


def _proto_type(f: Any) -> str:
    if _is_map(f):
        key, value = f.message_type.fields_by_name["key"], f.message_type.fields_by_name["value"]
        return f"map<{_proto_type(key)}, {_proto_type(value)}>"
    if f.message_type is not None:
        base = f.message_type.full_name
    elif f.enum_type is not None:
        base = f"enum {f.enum_type.full_name}"
    else:
        base = next(n for n, v in vars(type(f)).items() if n.startswith("TYPE_") and v == f.type)[5:].lower()
    return f"repeated {base}" if _repeated(f) else base


def _fields(message: Any, depth: int) -> str:
    """'{name: string, tags: repeated string, ...}' of a message, nested messages to `depth`."""
    parts = []
    for f in message.fields:
        inner = f.message_type is not None and not _is_map(f) and f.message_type.full_name not in WKT
        parts.append(f"{f.name}: {_fields(f.message_type, depth - 1) if inner and depth > 0 else _proto_type(f)}")
    return "{" + ", ".join(parts) + "}"


def _example(f: Any, depth: int) -> Any:
    """A placeholder value of a field as the JSON mapping writes it (64-bit integers as strings)."""
    if _is_map(f):
        return {}
    if _repeated(f):
        return [_one(f, depth)]
    return _one(f, depth)


def _one(f: Any, depth: int) -> Any:
    if f.message_type is not None:
        if f.message_type.full_name in WKT:
            return {"string": "", "object": {}, "array": [], "boolean": False, "number": 0.0}.get(
                WKT[f.message_type.full_name], 0
            )
        return _skeleton(f.message_type, depth) if depth > 0 else {}
    if f.enum_type is not None:
        return f.enum_type.values[0].name if f.enum_type.values else ""
    if f.type in (f.TYPE_INT64, f.TYPE_UINT64, f.TYPE_SINT64, f.TYPE_FIXED64, f.TYPE_SFIXED64):
        return "0"  # the JSON mapping writes 64-bit integers as strings
    return {"boolean": False, "number": 0.0, "integer": 0}.get(_scalar(f), "")


def _skeleton(message: Any, depth: int) -> dict[str, Any]:
    """A response shaped like the message's JSON: every field with a placeholder value, nested to `depth`."""
    return {f.name: _example(f, depth - 1) for f in message.fields}
