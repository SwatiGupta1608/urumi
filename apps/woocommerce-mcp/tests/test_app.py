"""HTTP-level tests: health endpoints, DNS-rebinding protection, MCP over Streamable HTTP."""

import httpx
import pytest
import respx
from starlette.testclient import TestClient

from woocommerce_mcp.app import create_app
from woocommerce_mcp.config import Settings

from .conftest import API, CK, CS, product

MCP_HEADERS = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}


@pytest.fixture
def app_client():
    settings = Settings(
        base_url="https://traefik.test", consumer_key=CK, consumer_secret=CS,
        tls_server_name="shop.test", allowed_hosts=("testserver", "woocommerce-mcp:*"),
        allowed_origins=("http://localhost:*",),
    )
    with TestClient(create_app(settings)) as tc:
        yield tc


def rpc(method, params=None, id_=1):
    return {"jsonrpc": "2.0", "id": id_, "method": method, "params": params or {}}


INIT = rpc("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                          "clientInfo": {"name": "pytest", "version": "0"}})


def test_health_and_ready(app_client):
    assert app_client.get("/healthz").json() == {"status": "ok"}
    assert app_client.get("/readyz").status_code == 200


def test_no_openapi_docs(app_client):
    assert app_client.get("/docs").status_code == 404
    assert app_client.get("/openapi.json").status_code == 404


def test_mcp_initialize_and_list_tools(app_client):
    r = app_client.post("/mcp", json=INIT, headers=MCP_HEADERS)
    assert r.status_code == 200, r.text
    assert r.json()["result"]["serverInfo"]["name"] == "woocommerce"
    r = app_client.post("/mcp", json=rpc("tools/list", id_=2), headers=MCP_HEADERS)
    names = sorted(t["name"] for t in r.json()["result"]["tools"])
    assert names == ["create_product", "get_order", "list_orders", "list_products", "update_order_status"]


def test_mcp_tool_call_over_http(app_client):
    with respx.mock(base_url=API) as wc:
        wc.get("/products").mock(return_value=httpx.Response(200, json=[product()], headers={"X-WP-TotalPages": "1"}))
        r = app_client.post("/mcp", json=rpc("tools/call", {"name": "list_products", "arguments": {"per_page": 5}}),
                            headers=MCP_HEADERS)
    result = r.json()["result"]
    assert result["isError"] is False
    assert result["structuredContent"]["products"][0]["id"] == 11


def test_mcp_tool_validation_error_over_http(app_client):
    r = app_client.post("/mcp", json=rpc("tools/call", {"name": "list_orders", "arguments": {"per_page": 999}}),
                        headers=MCP_HEADERS)
    assert r.json()["result"]["isError"] is True


def test_disallowed_host_is_rejected(app_client):
    r = app_client.post("/mcp", json=INIT, headers={**MCP_HEADERS, "Host": "evil.example"})
    assert r.status_code == 421


def test_disallowed_browser_origin_is_rejected(app_client):
    r = app_client.post("/mcp", json=INIT, headers={**MCP_HEADERS, "Origin": "https://evil.example"})
    assert r.status_code == 403
