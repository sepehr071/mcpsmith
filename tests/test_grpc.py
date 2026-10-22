import json
import os
import shutil
import socket
import subprocess
import sys
import types
from concurrent import futures

import pytest
from mcp import Client

from toolsmelt import codegen
from toolsmelt.check import _import_server
from toolsmelt.design import digest, draft
from toolsmelt.model import AuthScheme, Output, Plan, Tool, ToolArg
from toolsmelt.runtime import ApiError
from toolsmelt.runtime.interpret import api_kwargs, make_api, make_tool, run_tool, tool_spec

grpc = pytest.importorskip("grpc")
pytest.importorskip("grpc_reflection")
from google.protobuf import descriptor_pb2, descriptor_pool, message_factory  # noqa: E402
from grpc_reflection.v1alpha import reflection  # noqa: E402

from toolsmelt.importers import grpc as grpc_importer  # noqa: E402
from toolsmelt.runtime.grpc import Grpc, address, is_grpc  # noqa: E402

pytestmark = pytest.mark.anyio
F = descriptor_pb2.FieldDescriptorProto
PRODUCTS = {
    "p1": {"id": "p1", "name": "Green tea", "price": {"currency": "IRR", "units": 1250}, "tags": ["tea"], "status": 1},
    "p2": {"id": "p2", "name": "Tea pot", "price": {"currency": "IRR", "units": 9000}, "tags": [], "status": 2},
}


def _field(name, number, kind, type_name="", repeated=False):
    label = F.LABEL_REPEATED if repeated else F.LABEL_OPTIONAL
    return F(name=name, number=number, type=kind, label=label, **({"type_name": type_name} if type_name else {}))


def _file() -> descriptor_pb2.FileDescriptorProto:
    """shop.v1.Catalog: two unary methods and one server-streaming one, with comments as protoc keeps them."""
    f = descriptor_pb2.FileDescriptorProto(name="shop/v1/catalog.proto", package="shop.v1", syntax="proto3")
    status = f.enum_type.add(name="Status")
    for i, n in enumerate(["STATUS_UNSPECIFIED", "ACTIVE", "RETIRED"]):
        status.value.add(name=n, number=i)
    messages = {
        "Money": [_field("currency", 1, F.TYPE_STRING), _field("units", 2, F.TYPE_INT64)],
        "Product": [
            _field("id", 1, F.TYPE_STRING),
            _field("name", 2, F.TYPE_STRING),
            _field("price", 3, F.TYPE_MESSAGE, ".shop.v1.Money"),
            _field("tags", 4, F.TYPE_STRING, repeated=True),
            _field("status", 5, F.TYPE_ENUM, ".shop.v1.Status"),
        ],
        "GetProductRequest": [_field("id", 1, F.TYPE_STRING)],
        "ListProductsRequest": [
            _field("query", 1, F.TYPE_STRING),
            _field("page_size", 2, F.TYPE_INT32),
            _field("status", 3, F.TYPE_ENUM, ".shop.v1.Status"),
        ],
        "ListProductsResponse": [_field("products", 1, F.TYPE_MESSAGE, ".shop.v1.Product", repeated=True)],
    }
    for name, fields in messages.items():
        f.message_type.add(name=name, field=fields)
    service = f.service.add(name="Catalog")
    for name, req, resp, stream in [
        ("GetProduct", "GetProductRequest", "Product", False),
        ("ListProducts", "ListProductsRequest", "ListProductsResponse", False),
        ("WatchProducts", "ListProductsRequest", "Product", True),
    ]:
        service.method.add(
            name=name, input_type=f".shop.v1.{req}", output_type=f".shop.v1.{resp}", server_streaming=stream
        )
    where = f.source_code_info.location
    where.add(path=[6, 0, 2, 0], leading_comments=" Get one product by its id.\n")
    where.add(path=[4, 2, 2, 0], leading_comments=" The product id, e.g. p1.\n")  # GetProductRequest.id
    return f


class WithComments:
    """The pool, but serving the file with its source info: Python's reflection drops comments (CopyToProto),
    servers that keep them send them."""

    def __init__(self, pool):
        self.pool = pool

    def __getattr__(self, name):
        return getattr(self.pool, name)

    def _file(self, d):
        if d.name != _file().name:
            return d
        return types.SimpleNamespace(
            name=d.name, dependencies=d.dependencies, CopyToProto=lambda p: p.CopyFrom(_file())
        )

    def FindFileByName(self, name):
        return self._file(self.pool.FindFileByName(name))

    def FindFileContainingSymbol(self, symbol):
        return self._file(self.pool.FindFileContainingSymbol(symbol))


@pytest.fixture(scope="module")
def server():
    """A real gRPC server on a free localhost port, with reflection; `seen` collects each call's metadata."""
    pool = descriptor_pool.DescriptorPool()
    pool.AddSerializedFile(_file().SerializeToString())
    cls = {n: message_factory.GetMessageClass(pool.FindMessageTypeByName(f"shop.v1.{n}")) for n in (
        "Product", "GetProductRequest", "ListProductsRequest", "ListProductsResponse")}  # fmt: skip
    seen: list[dict[str, str]] = []

    def get(request, context):
        seen.append(dict(context.invocation_metadata()))
        if not request.id:
            context.abort(grpc.StatusCode.INVALID_ARGUMENT, "id is required")
        if request.id not in PRODUCTS:
            context.abort(grpc.StatusCode.NOT_FOUND, f"no product {request.id}")
        return cls["Product"](**PRODUCTS[request.id])

    def search(request):
        hits = [p for p in PRODUCTS.values() if request.query.lower() in p["name"].lower()]
        hits = [p for p in hits if not request.status or p["status"] == request.status]
        return [cls["Product"](**p) for p in hits[: request.page_size or None]]

    def handler(fn, req, resp, stream=False):
        make = grpc.unary_stream_rpc_method_handler if stream else grpc.unary_unary_rpc_method_handler
        return make(fn, request_deserializer=cls[req].FromString, response_serializer=cls[resp].SerializeToString)

    srv = grpc.server(futures.ThreadPoolExecutor(max_workers=4))
    methods = {
        "GetProduct": handler(get, "GetProductRequest", "Product"),
        "ListProducts": handler(
            lambda r, c: cls["ListProductsResponse"](products=search(r)), "ListProductsRequest", "ListProductsResponse"
        ),
        "WatchProducts": handler(lambda r, c: iter(search(r)), "ListProductsRequest", "Product", stream=True),
    }
    srv.add_generic_rpc_handlers([grpc.method_handlers_generic_handler("shop.v1.Catalog", methods)])
    reflection.enable_server_reflection(["shop.v1.Catalog", reflection.SERVICE_NAME], srv, pool=WithComments(pool))
    port = srv.add_insecure_port("127.0.0.1:0")
    srv.start()
    yield f"grpc://127.0.0.1:{port}", seen
    srv.stop(None)


@pytest.fixture(scope="module")
def source(server):
    return grpc_importer.parse(server[0])


def _plan(url: str) -> Plan:
    return Plan(
        name="shop-mcp",
        title="Shop",
        description="Shop catalog over gRPC.",
        instructions="Search first, then get one product by its id.",
        prefix="shop",
        base_url=url,
        env_prefix="SHOP_MCP",
        headers={"x-client": "toolsmelt"},
        auth=AuthScheme(name="bearer", type="bearer", env="SHOP_MCP_TOKEN"),
        repository="https://github.com/someone/shop-mcp",
        tools=[
            Tool(
                name="shop_product",
                title="One product",
                description="One product by id: name, price and tags.",
                endpoint="RPC shop.v1.Catalog/GetProduct",
                args=[
                    ToolArg(name="id", description="Product id, e.g. p1.", required=True, to="body.id"),
                    ToolArg(name="trace", description="Trace id.", to="header.x-trace"),
                ],
                read_only=True,
                example={"id": "p1"},
            ),
            Tool(
                name="shop_search",
                title="Search products",
                description="Products whose name contains the query.",
                endpoint="RPC shop.v1.Catalog/ListProducts",
                args=[
                    ToolArg(name="query", description="Words of the name.", required=True, to="body.query"),
                    ToolArg(name="limit", type="integer", description="At most this many.", to="body.page_size"),
                    ToolArg(
                        name="status", description="Only this status.", enum=["ACTIVE", "RETIRED"], to="body.status"
                    ),
                ],
                output=Output(root="products", fields={"id": "id", "name": "name", "price": "price.units"}),
                read_only=True,
                example={"query": "tea"},
            ),
        ],
    )


def test_urls():
    assert is_grpc("grpc://h:1") and is_grpc("GRPCS://h") and not is_grpc("https://h") and not is_grpc("sqlite:///x")
    assert address("grpcs://api.test") == ("api.test:443", True) and address("grpc://[::1]:50051") == (
        "[::1]:50051",
        False,
    )
    with pytest.raises(ValueError, match="not a gRPC URL"):
        address("grpc://:5")


def test_inspect_reads_unary_methods_fields_and_comments(source, server):
    assert source.kind == "grpc" and source.title == "shop" and source.base_url == server[0]
    assert "skipped shop.v1.Catalog/WatchProducts: server-streaming" in source.description
    eps = {e.id: e for e in source.endpoints}
    assert list(eps) == ["RPC shop.v1.Catalog/GetProduct", "RPC shop.v1.Catalog/ListProducts"]
    get = eps["RPC shop.v1.Catalog/GetProduct"]
    assert (
        get.method == "RPC" and get.path == "shop.v1.Catalog/GetProduct" and get.summary == "Get one product by its id."
    )
    assert [(p.name, p.location, p.type) for p in get.params] == [("id", "body", "string")]
    assert get.params[0].description == "string; The product id, e.g. p1."
    assert get.response_example["price"] == {"currency": "", "units": "0"} and get.response_example["tags"] == [""]
    params = {p.name: p for p in eps["RPC shop.v1.Catalog/ListProducts"].params}
    assert params["page_size"].type == "integer" and params["status"].type == "string"
    assert params["status"].enum == ["STATUS_UNSPECIFIED", "ACTIVE", "RETIRED"]
    d = digest(source)
    assert "## Methods (2 unary" in d and "### RPC shop.v1.Catalog/GetProduct\nGet one product" in d
    assert "- id (body, string) — string; The product id" in d and "`products` [1]" in d


def test_draft_maps_fields_to_body(source):
    plan = draft(source)
    assert plan.base_url == source.base_url and {t.endpoint for t in plan.tools} == {e.id for e in source.endpoints}
    assert all(a.to.startswith("body.") for t in plan.tools for a in t.args)


async def test_tools_round_trip_with_metadata(server, monkeypatch):
    url, seen = server
    plan = _plan(url)
    monkeypatch.setenv("SHOP_MCP_TOKEN", "s3cret")
    api = make_api(plan, None)
    assert isinstance(api, Grpc) and api_kwargs(plan, None)["auth"] == {"type": "bearer", "env": "SHOP_MCP_TOKEN"}
    product = make_tool(plan.tools[0], plan, None, api)
    result = await product(id="p1", trace="t-1")
    assert result["name"] == "Green tea" and result["price"] == {"currency": "IRR", "units": "1250"}
    assert result["status"] == "ACTIVE" and result["tags"] == ["tea"]
    md = seen[-1]
    assert md["authorization"] == "Bearer s3cret" and md["x-client"] == "toolsmelt" and md["x-trace"] == "t-1"
    search = make_tool(plan.tools[1], plan, None, api)
    assert (await search(query="tea"))["items"] == [
        {"id": "p1", "name": "Green tea", "price": "1250"},
        {"id": "p2", "name": "Tea pot", "price": "9000"},
    ]
    hits = await search(query="tea", status="RETIRED", limit=5)
    assert hits["items"] == [{"id": "p2", "name": "Tea pot", "price": "9000"}]


async def test_status_codes_become_clear_errors(server):
    plan = _plan(server[0]).model_copy(update={"auth": None})
    api = make_api(plan, None)
    product = make_tool(plan.tools[0], plan, None, api)
    with pytest.raises(ApiError, match=r"INVALID_ARGUMENT \(the server rejected the arguments\): id is required") as e:
        await product(id="")
    assert e.value.status == 400
    with pytest.raises(ApiError, match="NOT_FOUND .*no product p9") as e:
        await product(id="p9")
    assert e.value.status == 404
    spec = tool_spec(plan.tools[0], plan, None) | {"fixed": {"body.colour": "red"}}
    with pytest.raises(ApiError, match='invalid request: .*no field named "colour"'):
        await run_tool(api, spec, {"id": "p1", "trace": None})
    with pytest.raises(ApiError, match="not path/query/cookie"):
        await api.call("RPC", "shop.v1.Catalog/GetProduct", query={"id": "p1"})
    with pytest.raises(ApiError, match="streaming method"):
        await api.call("RPC", "shop.v1.Catalog/WatchProducts", body={})
    with pytest.raises(ApiError, match="does not list this method"):
        await api.call("RPC", "shop.v1.Catalog/Nope", body={})


async def test_unreachable_server():
    with socket.socket() as s:  # a port nothing listens on
        s.bind(("127.0.0.1", 0))
        url = f"grpc://127.0.0.1:{s.getsockname()[1]}"
    with pytest.raises(ApiError, match="UNAVAILABLE") as e:
        await Grpc(url, "SHOP_MCP").call("RPC", "shop.v1.Catalog/GetProduct", body={"id": "p1"})
    assert e.value.status == 503
    with pytest.raises(ValueError, match="no gRPC server answered"):
        grpc_importer.parse(url, timeout=1)


def test_auth_kinds():
    with pytest.raises(ValueError, match="gRPC auth"):
        Grpc("grpc://h:1", "X_MCP", auth={"type": "query", "env": "X_MCP_KEY", "param": "key"})
    api = Grpc("grpc://h:1", "X_MCP", auth={"type": "header", "env": "X_MCP_KEY", "param": "X-Api-Key"})
    api.secret = lambda env: "k" if env == "X_MCP_KEY" else None  # the keychain callback, like HTTP
    assert api._credential() == [("x-api-key", "k")]


async def test_generated_project_uses_grpc(server, source, tmp_path):
    plan = _plan(server[0])
    out = tmp_path / "shop-mcp"
    codegen.generate(plan, out, source)
    code = (out / "src/shop_mcp/server.py").read_text(encoding="utf-8")
    assert "from toolsmelt.runtime import Registry, build_server, run\nfrom toolsmelt.runtime.grpc import Grpc" in code
    assert "api = Grpc(" in code
    assert '"toolsmelt[grpc]>=0.8,<1"' in (out / "pyproject.toml").read_text(encoding="utf-8")
    assert "SHOP_MCP_PROXY" in (out / "server.json").read_text(encoding="utf-8")  # gRPC can go through a proxy
    env = {k: v for k, v in os.environ.items() if not k.startswith("SHOP_MCP")} | {"PYTHONPATH": str(out / "src")}
    r = subprocess.run(
        [sys.executable, "-m", "pytest", str(out / "tests"), "-q", "-p", "no:cacheprovider"],
        cwd=out, env=env, capture_output=True, text=True,
    )  # fmt: skip
    assert r.returncode == 0, r.stdout + r.stderr
    assert "2 passed, 2 deselected" in r.stdout
    if shutil.which("ruff"):
        lint = subprocess.run(["ruff", "check", str(out)], capture_output=True, text=True)
        assert lint.returncode == 0, lint.stdout
    gen = _import_server(out / "src", plan.package)
    async with Client(gen.mcp) as c:
        result = await c.call_tool("shop_product", {"id": "p2"})
    assert json.loads(result.content[0].text)["name"] == "Tea pot"


def test_proxy_from_env_and_in_the_generated_env_vars(monkeypatch):
    from toolsmelt.runtime.grpc import Grpc

    monkeypatch.setenv("SHOP_MCP_PROXY", "http://p.test:1, http://q.test:2")
    assert Grpc(base_url="grpc://localhost:1", env_prefix="SHOP_MCP").proxy == "http://p.test:1"  # first of the list
    assert (
        Grpc(base_url="grpc://localhost:1", env_prefix="SHOP_MCP", proxy="http://x.test:3").proxy == "http://x.test:3"
    )
    monkeypatch.delenv("SHOP_MCP_PROXY")
    assert Grpc(base_url="grpc://localhost:1", env_prefix="SHOP_MCP").proxy is None
    plan = Plan(
        name="shop-mcp", title="Shop", description="d.", instructions="i.", prefix="shop",
        base_url="grpc://localhost:1", env_prefix="SHOP_MCP",
    )  # fmt: skip
    assert "SHOP_MCP_PROXY" in [e["name"] for e in codegen._env_vars(plan)]
