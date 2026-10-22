"""Read a WSDL 1.1 service description (SOAP 1.1 or 1.2 bindings, document/literal or rpc/literal) into a Source.

One endpoint per operation: POST to the port's soap:address with body_encoding "soap". The request element's
fields become body params named '<Root>.<field>'. Two params carry a default the draft makes fixed: the
SOAPAction header and the request element's namespace ('<Root>.@xmlns', or '<m:Root>.@xmlns:m' when the fields
are unqualified). Only inline xsd:schema is read; anything left out is listed in the description.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urljoin, urlsplit
from xml.etree.ElementTree import Element

from toolsmelt.importers.android import strip_userinfo
from toolsmelt.importers.har import cap_notes
from toolsmelt.model import Endpoint, Param, Source
from toolsmelt.runtime.http import xml_root

WSDL = "{http://schemas.xmlsoap.org/wsdl/}"
XS = "{http://www.w3.org/2001/XMLSchema}"
SOAP11 = "{http://schemas.xmlsoap.org/wsdl/soap/}"
SOAP12 = "{http://schemas.xmlsoap.org/wsdl/soap12/}"
INTEGER = {
    "int", "integer", "long", "short", "byte", "unsignedInt", "unsignedLong", "unsignedShort", "unsignedByte",
    "nonNegativeInteger", "positiveInteger", "negativeInteger", "nonPositiveInteger",
}  # fmt: skip
NUMBER = {"decimal", "float", "double"}
SAMPLE = {"integer": "0", "number": "0.0", "boolean": "false", "string": "text"}  # XML answers carry text only
MAX_DEPTH = 4  # nested complex types deeper than this (or recursive ones) show as an empty object

Shape = str | dict | list  # an ArgType, {field: Shape} for a complex type, [Shape] for a repeated element


def sniff(text: Any) -> bool:
    """A WSDL 1.1 document: a <definitions> element in the WSDL namespace."""
    return isinstance(text, str) and "<" in text and "definitions" in text and WSDL[1:-1] in text


def parse(text: str, origin: str | None = None) -> Source:
    """`origin` (the URL the WSDL came from) resolves a relative soap:address. No network access."""
    try:
        root = xml_root(text)
    except ValueError as e:
        raise ValueError(f"not a readable WSDL: {e}") from None
    if root.tag != f"{WSDL}definitions":
        what = "WSDL 2.0 is not supported" if _local(root.tag) == "description" else "no wsdl:definitions root"
        raise ValueError(f"not a WSDL 1.1 document ({what})")
    return _Reader(root, origin).source()


def _local(tag: str) -> str:
    return tag.rpartition("}")[2]


def _name(qname: str | None) -> str:
    # shortcut: QNames resolve by local name; a WSDL reusing one name in two namespaces would mix them up
    return (qname or "").rpartition(":")[2]


def _first(el: Element | None, *tags: str) -> Element | None:
    for tag in tags:
        if el is not None and (found := el.find(tag)) is not None:
            return found
    return None


def _doc(el: Element | None) -> str:
    d = el.find(f"{WSDL}documentation") if el is not None else None
    return " ".join("".join(d.itertext()).split())[:300] if d is not None else ""


def _arg_type(shape: Shape) -> str:
    return "object" if isinstance(shape, dict) else "array" if isinstance(shape, list) else shape


def _sample(shape: Shape) -> Any:
    if isinstance(shape, dict):
        return {k: _sample(v) for k, v in shape.items()}
    if isinstance(shape, list):
        return [_sample(shape[0])]
    return SAMPLE.get(shape, "text")


def _show(shape: Shape) -> str:
    if isinstance(shape, dict):
        return "{" + ", ".join(f"{k}: {_show(v)}" for k, v in shape.items()) + "}"
    return f"[{_show(shape[0])}]" if isinstance(shape, list) else shape


class _Reader:
    def __init__(self, root: Element, origin: str | None):
        self.root, self.origin, self.notes = root, origin, []
        self.elements: dict[str, tuple[Element, str, bool]] = {}  # global element -> (decl, namespace, qualified)
        self.types: dict[str, Element] = {}
        for schema in root.iter(f"{XS}schema"):
            tns, qualified = schema.get("targetNamespace", ""), schema.get("elementFormDefault") == "qualified"
            for child in schema:
                if child.tag == f"{XS}element" and child.get("name"):
                    self.elements[child.get("name")] = (child, tns, qualified)
                elif child.tag in (f"{XS}complexType", f"{XS}simpleType") and child.get("name"):
                    self.types[child.get("name")] = child
                elif _local(child.tag) in ("import", "include", "redefine") and (loc := child.get("schemaLocation")):
                    self.notes.append(f"external schema {strip_userinfo(loc)[:120]} not read: its types show as text")

    def named(self, tag: str) -> dict[str, Element]:
        return {e.get("name", ""): e for e in self.root.findall(f"{WSDL}{tag}")}

    def source(self) -> Source:
        messages, port_types, bindings = self.named("message"), self.named("portType"), self.named("binding")
        endpoints: dict[str, Endpoint] = {}
        base_url = ""
        for service in self.root.findall(f"{WSDL}service"):
            sname = service.get("name", "Service")
            ports = []
            for port in service.findall(f"{WSDL}port"):
                binding = bindings.get(_name(port.get("binding")))
                address = _first(port, f"{SOAP11}address", f"{SOAP12}address")
                if binding is None or address is None:
                    self.notes.append(f"port {sname}.{port.get('name')}: not a SOAP binding, left out")
                    continue
                ports.append((address.tag.startswith(SOAP12), port, binding, address.get("location", "")))
            done: set[str] = set()
            for soap12, port, binding, location in sorted(ports, key=lambda p: p[0]):  # SOAP 1.1 ports first
                port_type = port_types.get(_name(binding.get("type")))
                if port_type is None or port_type.get("name") in done:
                    continue  # the same operations through SOAP 1.2: the runtime sends SOAP 1.1
                done.add(port_type.get("name"))
                if soap12:
                    self.notes.append(
                        f"{sname}.{port.get('name')}: only a SOAP 1.2 binding; toolsmelt sends SOAP 1.1 envelopes, "
                        "which a SOAP 1.2-only service may refuse"
                    )
                url = urlsplit(strip_userinfo(urljoin(self.origin or "", location)))
                if not url.scheme:
                    self.notes.append(f"{sname}.{port.get('name')}: relative address {location!r}; set base_url")
                base = f"{url.scheme}://{url.netloc}" if url.scheme else ""
                base_url = base_url or base
                path = (url.path or "/") + (f"?{url.query}" if url.query else "")
                ops = {o.get("name"): o for o in port_type.findall(f"{WSDL}operation")}
                sb = _first(binding, f"{SOAP11}binding", f"{SOAP12}binding")
                style = (sb.get("style") if sb is not None else None) or "document"
                for bop in binding.findall(f"{WSDL}operation"):
                    ep = self.endpoint(sname, bop, ops.get(bop.get("name")), style, messages)
                    if ep is None:
                        continue
                    if ep.id in endpoints:
                        self.notes.append(f"{ep.id}: a second port offers the same operation, left out")
                        continue
                    ep.path, ep.base_url = path, base if base != base_url else ""
                    if soap12:
                        ep.description += " The service has only a SOAP 1.2 binding."
                    endpoints[ep.id] = ep
        if not endpoints:
            raise ValueError("no SOAP operation found" + (f" ({'; '.join(self.notes)})" if self.notes else ""))
        services = self.root.findall(f"{WSDL}service")
        title = (services[0].get("name") if services else "") or self.root.get("name") or "SOAP service"
        doc = _doc(services[0] if services else None) or _doc(self.root)
        return Source(
            kind="wsdl",
            title=title,
            description="\n".join(x for x in (doc, *cap_notes(list(dict.fromkeys(self.notes)))) if x),
            base_url=base_url,
            endpoints=list(endpoints.values()),
        )

    def endpoint(
        self, service: str, bop: Element, op: Element | None, style: str, messages: dict[str, Element]
    ) -> Endpoint | None:
        name = bop.get("name", "")
        where = f"SOAP {service}.{name}"
        sop = _first(bop, f"{SOAP11}operation", f"{SOAP12}operation")
        action = sop.get("soapAction", "") if sop is not None else ""
        style = (sop.get("style") if sop is not None else None) or style
        sbody = _first(bop.find(f"{WSDL}input"), f"{SOAP11}body", f"{SOAP12}body")
        if op is None or sbody is None:
            self.notes.append(f"{where}: no input body in the binding or portType, left out")
            return None
        if sbody.get("use", "literal") != "literal":
            self.notes.append(f"{where}: SOAP encoding (use=encoded) is not supported, left out")
            return None
        if _first(bop.find(f"{WSDL}input"), f"{SOAP11}header", f"{SOAP12}header") is not None:
            self.notes.append(f"{where}: its SOAP header blocks (soap:header) are not sent")
        wanted = sbody.get("parts")
        parts = self.parts(op, "input", messages, wanted)
        out_parts = self.parts(op, "output", messages, None)
        if style == "rpc":
            ns, qualified, root = sbody.get("namespace", ""), False, name
            fields = [(p.get("name", ""), p, "1", "1") for p in parts]
            response: Any = {p.get("name", ""): _sample(self.shape(p, 0)) for p in out_parts} or None
        else:
            decl = self.elements.get(_name(parts[0].get("element"))) if len(parts) == 1 else None
            if decl is None:
                self.notes.append(f"{where}: document style needs one input part naming a schema element, left out")
                return None
            el, ns, qualified = decl
            root = el.get("name", "")
            fields = self.fields(self.complex_of(el), 0)
            out = self.elements.get(_name(out_parts[0].get("element"))) if len(out_parts) == 1 else None
            response = _sample(self.shape(out[0], 0)) if out else None  # the runtime drops the wrapper element
        key = root if qualified or not ns else f"m:{root}"
        params = [
            Param(
                name="SOAPAction", location="header", required=True, default=action,
                description="SOAP action of this operation; send it as a fixed header.",
            )
        ]  # fmt: skip
        if ns:
            attr = "@xmlns" if key == root else "@xmlns:m"
            params.append(
                Param(
                    name=f"{key}.{attr}", location="body", required=True, default=ns,
                    description="XML namespace of the request element; a fixed value.",
                )
            )  # fmt: skip
        for fname, decl_el, lo, hi in fields:
            if "." in fname or not fname:
                self.notes.append(f"{where}: field {fname!r} has no body target (a dot in its name), left out")
                continue
            shape = self.shape(decl_el, 1)
            if hi == "unbounded" or (hi.isdigit() and int(hi) > 1):
                shape = [shape]
            desc = f"Fields in this order: {_show(shape)}." if isinstance(shape, dict | list) else ""
            params.append(
                Param(
                    name=f"{key}.{fname}", location="body", type=_arg_type(shape), required=lo != "0",
                    enum=self.enum(decl_el), description=desc,
                )
            )  # fmt: skip
        doc = _doc(op)
        return Endpoint(
            id=f"SOAP {service}.{name}",
            method="POST",
            path="/",
            summary=doc.split(". ")[0][:120] if doc else name,
            description=f"SOAP operation {name} ({style}/literal), SOAPAction {action!r}. {doc}".strip(),
            tags=[service],
            params=params,
            body_encoding="soap",
            response_example=response,
        )

    def parts(self, op: Element, kind: str, messages: dict[str, Element], wanted: str | None) -> list[Element]:
        io = op.find(f"{WSDL}{kind}")
        msg = messages.get(_name(io.get("message"))) if io is not None else None
        parts = msg.findall(f"{WSDL}part") if msg is not None else []
        return [p for p in parts if wanted is None or p.get("name") in wanted.split()]

    def complex_of(self, el: Element) -> Element | None:
        """The complexType of an element or part: inline, or named by its type."""
        inline = el.find(f"{XS}complexType")
        if inline is not None:
            return inline
        t = self.types.get(_name(el.get("type")))
        return t if t is not None and t.tag == f"{XS}complexType" else None

    def fields(self, ct: Element | None, depth: int) -> list[tuple[str, Element, str, str]]:
        """(name, declaration, minOccurs, maxOccurs) of a complexType's elements, base type first."""
        out: list[tuple[str, Element, str, str]] = []
        if ct is None or depth > MAX_DEPTH:
            return out

        def walk(node: Element) -> None:  # model groups only: a field's own inline type is not walked
            for child in node:
                if child.tag == f"{XS}element":
                    ref = child.get("ref")
                    decl = self.elements.get(_name(ref), (child,))[0] if ref else child
                    out.append((decl.get("name", ""), decl, child.get("minOccurs", "1"), child.get("maxOccurs", "1")))
                elif child.tag == f"{XS}extension":
                    base = self.types.get(_name(child.get("base")))
                    if base is not None and base.tag == f"{XS}complexType":
                        out.extend(self.fields(base, depth + 1))
                    walk(child)
                elif child.tag in (f"{XS}sequence", f"{XS}all", f"{XS}choice", f"{XS}complexContent"):
                    walk(child)

        walk(ct)
        return out

    def shape(self, el: Element, depth: int) -> Shape:
        """What an element or part holds: an ArgType for simple content, {field: Shape} for a complex type."""
        if (ref := el.get("element")) and (decl := self.elements.get(_name(ref))):  # an rpc part naming an element
            el = decl[0]
        if (ct := self.complex_of(el)) is not None:
            if depth > MAX_DEPTH:
                return {}
            shape: dict[str, Shape] = {}
            for name, decl, _, hi in self.fields(ct, depth):
                s = self.shape(decl, depth + 1)
                shape[name] = [s] if hi == "unbounded" or (hi.isdigit() and int(hi) > 1) else s
            return shape
        simple = el.find(f"{XS}simpleType")
        if simple is None:
            simple = self.types.get(_name(el.get("type")))
        if simple is not None:
            restriction = simple.find(f"{XS}restriction")
            base = restriction.get("base") if restriction is not None else None
            return self.builtin(_name(base)) if base else "string"
        return self.builtin(_name(el.get("type")))

    @staticmethod
    def builtin(name: str) -> str:
        if name in INTEGER:
            return "integer"
        if name in NUMBER:
            return "number"
        return "boolean" if name == "boolean" else "string"

    def enum(self, el: Element) -> list[str] | None:
        simple = el.find(f"{XS}simpleType")
        if simple is None:
            simple = self.types.get(_name(el.get("type")))
        values = [e.get("value") for e in simple.iter(f"{XS}enumeration")] if simple is not None else []
        return values or None
