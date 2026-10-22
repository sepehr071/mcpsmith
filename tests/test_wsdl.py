from pathlib import Path

import httpx
import pytest

from toolsmelt.design import draft, validate
from toolsmelt.importers import wsdl
from toolsmelt.runtime import ApiError, interpret

pytestmark = pytest.mark.anyio

WEATHER = (Path(__file__).parent / "fixtures" / "wsdl" / "weather.wsdl").read_text(encoding="utf-8")
NS = "http://example.com/weather"

# rpc/literal, SOAP 1.2 only, relative address: the operation wrapper is namespaced, its parts are not
RPC = """<definitions xmlns="http://schemas.xmlsoap.org/wsdl/" xmlns:soap12="http://schemas.xmlsoap.org/wsdl/soap12/"
    xmlns:xsd="http://www.w3.org/2001/XMLSchema" xmlns:tns="urn:calc" name="Calc" targetNamespace="urn:calc">
  <message name="AddIn"><part name="a" type="xsd:int"/><part name="b" type="xsd:int"/></message>
  <message name="AddOut"><part name="sum" type="xsd:int"/></message>
  <portType name="CalcPort"><operation name="Add"><input message="tns:AddIn"/><output message="tns:AddOut"/></operation>
  </portType>
  <binding name="CalcBinding" type="tns:CalcPort"><soap12:binding style="rpc"/>
    <operation name="Add"><soap12:operation soapAction=""/>
      <input><soap12:body use="literal" namespace="urn:calc"/></input><output><soap12:body use="literal"/></output>
    </operation>
  </binding>
  <service name="Calc"><port name="CalcPort" binding="tns:CalcBinding"><soap12:address location="/calc"/></port></service>
</definitions>"""


def test_sniff():
    assert wsdl.sniff(WEATHER) and wsdl.sniff(RPC)
    assert not wsdl.sniff('{"openapi": "3.0.0"}') and not wsdl.sniff("<html></html>") and not wsdl.sniff(None)


def test_parse_document_literal():
    src = wsdl.parse(WEATHER)
    assert src.kind == "wsdl" and src.title == "Weather" and src.base_url == "https://weather.example.com"
    # the SOAP 1.2 port repeats the SOAP 1.1 operations and the HTTP GET port is not SOAP: one endpoint each
    assert [e.id for e in src.endpoints] == [
        "SOAP Weather.GetForecast",
        "SOAP Weather.ListStations",
        "SOAP Weather.ReportReading",
    ]
    assert "units.xsd not read" in src.description and "WeatherHttpGet: not a SOAP binding" in src.description
    forecast = src.endpoints[0]
    assert (forecast.method, forecast.path, forecast.body_encoding) == ("POST", "/ws/weather.asmx", "soap")
    params = {p.name: p for p in forecast.params}
    assert params["SOAPAction"].location == "header" and params["SOAPAction"].default == f"{NS}/GetForecast"
    assert params["GetForecast.@xmlns"].default == NS
    assert params["GetForecast.City"].required and params["GetForecast.City"].type == "string"
    assert not params["GetForecast.Days"].required and params["GetForecast.Days"].type == "integer"
    assert forecast.response_example == {
        "GetForecastResult": {"City": "text", "Day": [{"Date": "text", "Summary": "text", "High": "0.0"}]}
    }
    reading = {p.name: p for p in src.endpoints[2].params}["ReportReading.Reading"]
    assert reading.type == "object" and reading.location == "body"
    assert "StationId: string, Value: number, Kind: string" in reading.description


def test_parse_rpc_soap12_and_refusals():
    src = wsdl.parse(RPC, origin="https://calc.example.com/service?wsdl")
    (ep,) = src.endpoints
    assert src.base_url == "https://calc.example.com" and ep.path == "/calc"
    assert {p.name: p.default for p in ep.params} == {
        "SOAPAction": "",
        "m:Add.@xmlns:m": "urn:calc",
        "m:Add.a": None,
        "m:Add.b": None,
    }
    assert ep.response_example == {"sum": "0"} and "only a SOAP 1.2 binding" in src.description
    with pytest.raises(ValueError, match="DTD"):
        wsdl.parse('<!DOCTYPE x [<!ENTITY a "aaaa">]>' + WEATHER.split("?>", 1)[1])
    with pytest.raises(ValueError, match="WSDL 2.0"):
        wsdl.parse('<description xmlns="http://www.w3.org/ns/wsdl"/>')
    with pytest.raises(ValueError, match="no SOAP operation"):
        wsdl.parse('<definitions xmlns="http://schemas.xmlsoap.org/wsdl/"/>')


async def test_draft_validate_and_call():
    src = wsdl.parse(WEATHER)
    notes: list[str] = []
    plan = draft(src, notes=notes)
    tools = {t.name: t for t in plan.tools}
    forecast, report = tools["weather_get_forecast"], tools["weather_report_reading"]
    assert forecast.fixed == {"header.SOAPAction": f"{NS}/GetForecast", "body.GetForecast.@xmlns": NS}
    assert [(a.name, a.to) for a in forecast.args] == [
        ("city", "body.GetForecast.City"),
        ("days", "body.GetForecast.Days"),
    ]
    assert forecast.read_only and tools["weather_list_stations"].read_only and not report.read_only
    assert "read_only" in report.model_fields_set
    assert "weather_report_reading: read_only false guessed from the operation name; check it" in notes
    assert not [i for i in validate(plan, src) if i.level == "error"]

    seen = []

    def handler(request):
        seen.append(request)
        body = (
            '<?xml version="1.0"?><soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/"><soap:Body>'
            f'<GetForecastResponse xmlns="{NS}"><GetForecastResult><City>Paris</City>'
            "<Day><Date>2026-10-22</Date><High>14.5</High></Day><Day><Date>2026-10-23</Date><High>12</High></Day>"
            "</GetForecastResult></GetForecastResponse></soap:Body></soap:Envelope>"
        )
        return httpx.Response(200, text=body, headers={"content-type": "text/xml; charset=utf-8"})

    api = interpret.make_api(plan, src)
    api.set_transport(httpx.MockTransport(handler))
    out = await interpret.make_tool(forecast, plan, src, api)(city="Paris & <Co>", days=2)
    assert out["items"] == [{"date": "2026-10-22", "high": "14.5"}, {"date": "2026-10-23", "high": "12"}]
    req = seen[0]
    assert str(req.url) == "https://weather.example.com/ws/weather.asmx"
    assert (
        req.headers["SOAPAction"] == f'"{NS}/GetForecast"' and req.headers["content-type"] == "text/xml; charset=utf-8"
    )
    assert req.content == (
        b"<?xml version='1.0' encoding='utf-8'?>\n"
        b'<soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/"><soap:Body>'
        b'<GetForecast xmlns="http://example.com/weather"><City>Paris &amp; &lt;Co&gt;</City><Days>2</Days>'
        b"</GetForecast></soap:Body></soap:Envelope>"
    )

    def fault(request):
        return httpx.Response(
            500,
            text='<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/"><s:Body><s:Fault>'
            "<faultcode>s:Client</faultcode><faultstring>Unknown station</faultstring></s:Fault></s:Body></s:Envelope>",
            headers={"content-type": "text/xml"},
        )

    api.set_transport(httpx.MockTransport(fault))
    with pytest.raises(ApiError, match=r"SOAP fault \(s:Client\): Unknown station") as e:
        await interpret.make_tool(report, plan, src, api)(reading={"StationId": "x1", "Value": 3})
    assert e.value.status == 500
