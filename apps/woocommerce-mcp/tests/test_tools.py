import base64
import json
import logging
import re

import httpx
import pytest
from mcp.server.fastmcp.exceptions import ToolError

from .conftest import CK, CS, call, order, product


# ---------- input validation (rejected before any upstream call) ----------

@pytest.mark.parametrize(
    "tool,args",
    [
        ("list_products", {"per_page": 51}),
        ("list_products", {"per_page": 0}),
        ("list_products", {"page": 0}),
        ("list_products", {"page": 1001}),
        ("list_products", {"page": True}),
        ("list_products", {"page": 1.5}),
        ("list_products", {"search": ""}),
        ("list_products", {"search": "x" * 101}),
        ("list_orders", {"per_page": 500}),
        ("list_orders", {"status": "trash"}),
        ("list_orders", {"status": "any"}),
        ("get_order", {"order_id": 0}),
        ("get_order", {"order_id": -5}),
        ("get_order", {"order_id": 10**13}),
        ("get_order", {"order_id": True}),
        ("get_order", {}),
        ("create_product", {"name": "", "regular_price": "1"}),
        ("create_product", {"name": "   ", "regular_price": "1"}),
        ("create_product", {"name": "x" * 201, "regular_price": "1"}),
        ("create_product", {"name": "bad\x00name", "regular_price": "1"}),
        ("create_product", {"name": "ok", "regular_price": "0"}),
        ("create_product", {"name": "ok", "regular_price": "-1"}),
        ("create_product", {"name": "ok", "regular_price": "1.999"}),
        ("create_product", {"name": "ok", "regular_price": "1000000.01"}),
        ("create_product", {"name": "ok", "regular_price": "NaN"}),
        ("create_product", {"name": "ok", "regular_price": "abc"}),
        ("create_product", {"name": "ok", "regular_price": "1", "status": "private"}),
        ("create_product", {"name": "ok", "regular_price": "1", "description": "x" * 5001}),
        ("create_product", {"name": "ok"}),
        ("update_order_status", {"order_id": 1, "status": "refunded"}),
        ("update_order_status", {"order_id": 1, "status": "trash"}),
        ("update_order_status", {"order_id": 1, "status": "checkout-draft"}),
        ("update_order_status", {"order_id": 1, "status": "COMPLETED"}),
        ("update_order_status", {"order_id": 1}),
    ],
)
async def test_invalid_inputs_rejected_without_upstream_call(mcp, wc, tool, args):
    route = wc.route().mock(return_value=httpx.Response(500))
    with pytest.raises(ToolError):
        await call(mcp, tool, args)
    assert not route.called


async def test_tools_do_not_accept_credentials_or_urls(mcp):
    for tool in await mcp.list_tools():
        props = set(tool.inputSchema.get("properties", {}))
        assert not props & {"consumer_key", "consumer_secret", "api_key", "url", "base_url", "path", "method"}


async def test_exactly_the_five_tools(mcp):
    names = {t.name for t in await mcp.list_tools()}
    assert names == {"list_products", "create_product", "list_orders", "get_order", "update_order_status"}


# ---------- products ----------

async def test_list_products_pagination_and_request(mcp, wc):
    route = wc.get("/products").mock(
        return_value=httpx.Response(200, json=[product()], headers={"X-WP-Total": "21", "X-WP-TotalPages": "3"})
    )
    res = await call(mcp, "list_products", {"page": 2, "per_page": 10, "search": "  Spike "})
    req = route.calls.last.request
    assert dict(req.url.params) == {"page": "2", "per_page": "10", "search": "Spike", "orderby": "date", "order": "desc"}
    assert res["pagination"] == {"page": 2, "per_page": 10, "total": 21, "total_pages": 3, "has_more": True}
    assert res["products"][0]["name"] == "Spike product"


async def test_list_products_last_page(mcp, wc):
    wc.get("/products").mock(return_value=httpx.Response(200, json=[], headers={"X-WP-Total": "0", "X-WP-TotalPages": "0"}))
    res = await call(mcp, "list_products", {})
    assert res["pagination"]["has_more"] is False and res["products"] == []


async def test_requests_use_basic_auth_host_and_sni(mcp, wc):
    route = wc.get("/products").mock(return_value=httpx.Response(200, json=[]))
    await call(mcp, "list_products", {})
    req = route.calls.last.request
    assert req.headers["Host"] == "shop.test"
    assert req.extensions["sni_hostname"] == "shop.test"
    assert req.url.scheme == "https"
    expected = "Basic " + base64.b64encode(f"{CK}:{CS}".encode()).decode()
    assert req.headers["Authorization"] == expected


async def test_create_product_payload(mcp, wc):
    route = wc.post("/products").mock(return_value=httpx.Response(201, json=product(id=99, name="New", regular_price="19.90")))
    res = await call(mcp, "create_product", {"name": " New ", "regular_price": 19.9, "description": "Nice"})
    body = json.loads(route.calls.last.request.content)
    assert body == {"name": "New", "type": "simple", "regular_price": "19.90", "status": "publish", "description": "Nice"}
    assert res["id"] == 99


async def test_create_product_accepts_string_price_and_draft(mcp, wc):
    route = wc.post("/products").mock(return_value=httpx.Response(201, json=product()))
    await call(mcp, "create_product", {"name": "x", "regular_price": "5", "status": "draft"})
    body = json.loads(route.calls.last.request.content)
    assert body["regular_price"] == "5.00" and body["status"] == "draft" and "description" not in body


# ---------- orders ----------

async def test_list_orders_with_status(mcp, wc):
    route = wc.get("/orders").mock(
        return_value=httpx.Response(200, json=[order()], headers={"X-WP-Total": "1", "X-WP-TotalPages": "1"})
    )
    res = await call(mcp, "list_orders", {"status": "on-hold", "per_page": 50})
    assert route.calls.last.request.url.params["status"] == "on-hold"
    assert route.calls.last.request.url.params["per_page"] == "50"
    o = res["orders"][0]
    assert o["billing_name"] == "Demo Buyer" and o["item_count"] == 2
    assert "billing_email" not in o  # summaries carry less PII than get_order


async def test_get_order(mcp, wc):
    wc.get("/orders/42").mock(return_value=httpx.Response(200, json=order()))
    res = await call(mcp, "get_order", {"order_id": 42})
    assert res["billing_email"] == "demo@example.invalid"
    assert res["line_items"] == [{"product_id": 11, "name": "Demo item", "quantity": 2, "total": "12.00"}]


async def test_update_order_status_changes(mcp, wc):
    wc.get("/orders/42").mock(return_value=httpx.Response(200, json=order(status="pending")))
    put = wc.put("/orders/42").mock(return_value=httpx.Response(200, json=order(status="on-hold")))
    res = await call(mcp, "update_order_status", {"order_id": 42, "status": "on-hold"})
    assert json.loads(put.calls.last.request.content) == {"status": "on-hold"}
    assert res == {"order_id": 42, "previous_status": "pending", "status": "on-hold", "changed": True}


async def test_update_order_status_noop_does_not_write(mcp, wc):
    wc.get("/orders/42").mock(return_value=httpx.Response(200, json=order(status="processing")))
    put = wc.put("/orders/42").mock(return_value=httpx.Response(200, json=order()))
    res = await call(mcp, "update_order_status", {"order_id": 42, "status": "processing"})
    assert res["changed"] is False and not put.called


async def test_update_order_status_detects_blocked_change(mcp, wc):
    wc.get("/orders/42").mock(return_value=httpx.Response(200, json=order(status="pending")))
    wc.put("/orders/42").mock(return_value=httpx.Response(200, json=order(status="pending")))
    with pytest.raises(ToolError, match="kept order 42"):
        await call(mcp, "update_order_status", {"order_id": 42, "status": "completed"})


@pytest.mark.parametrize("status", ["pending", "processing", "on-hold", "completed", "cancelled", "failed"])
async def test_every_allowed_status_is_accepted(mcp, wc, status):
    wc.get("/orders/7").mock(return_value=httpx.Response(200, json=order(id=7, status="pending" if status != "pending" else "failed")))
    wc.put("/orders/7").mock(return_value=httpx.Response(200, json=order(id=7, status=status)))
    res = await call(mcp, "update_order_status", {"order_id": 7, "status": status})
    assert res["status"] == status


# ---------- upstream errors, timeouts, leakage ----------

@pytest.mark.parametrize(
    "status,body,expect",
    [
        (400, {"code": "rest_invalid_param", "message": "Invalid parameter(s): status"}, "rejected the request: Invalid parameter"),
        (401, {"code": "woocommerce_rest_authentication_error", "message": "Consumer secret is invalid."}, "API credentials (401)"),
        (403, {"code": "woocommerce_rest_cannot_view", "message": "Sorry"}, "not permitted"),
        (404, {"code": "woocommerce_rest_shop_order_invalid_id", "message": "Invalid ID."}, "Not found"),
        (429, {}, "temporarily unavailable (HTTP 429)"),
        (502, "<html>bad gateway</html>", "temporarily unavailable (HTTP 502)"),
        (301, None, "Unexpected redirect"),
    ],
)
async def test_upstream_errors_are_actionable(mcp, wc, status, body, expect):
    kwargs = {"json": body} if isinstance(body, dict) else {"text": body or ""}
    if status == 301:
        kwargs["headers"] = {"Location": "https://evil.test/steal"}
    wc.get("/orders/42").mock(return_value=httpx.Response(status, **kwargs))
    with pytest.raises(ToolError, match=re.escape(expect)):
        await call(mcp, "get_order", {"order_id": 42})


async def test_redirect_is_not_followed(mcp, wc):
    evil = wc.route(host="evil.test").mock(return_value=httpx.Response(200, json=order()))
    wc.get("/orders/42").mock(return_value=httpx.Response(302, headers={"Location": "https://evil.test/x"}))
    with pytest.raises(ToolError):
        await call(mcp, "get_order", {"order_id": 42})
    assert not evil.called


async def test_non_json_success_body(mcp, wc):
    wc.get("/products").mock(return_value=httpx.Response(200, text="<html>login</html>"))
    with pytest.raises(ToolError, match="non-JSON"):
        await call(mcp, "list_products", {})


@pytest.mark.parametrize("exc", [httpx.ReadTimeout("slow"), httpx.ConnectTimeout("slow"), httpx.PoolTimeout("busy")])
async def test_timeouts(mcp, wc, exc):
    wc.get("/orders").mock(side_effect=exc)
    with pytest.raises(ToolError, match="did not respond in time .connect 1s / read 2s"):
        await call(mcp, "list_orders", {})


async def test_connect_error(mcp, wc):
    wc.get("/orders").mock(side_effect=httpx.ConnectError("refused"))
    with pytest.raises(ToolError, match="Could not reach WooCommerce .ConnectError"):
        await call(mcp, "list_orders", {})


async def test_tls_verification_error(mcp, wc):
    wc.get("/orders").mock(side_effect=httpx.ConnectError("[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed"))
    with pytest.raises(ToolError, match="TLS certificate verification"):
        await call(mcp, "list_orders", {})


async def test_real_socket_timeout_is_bounded(settings):
    """A server that accepts TCP but never answers must fail within the configured timeouts."""
    import asyncio
    import time

    from woocommerce_mcp.client import WooCommerceClient, WooCommerceError

    async def never_answer(reader, writer):
        await asyncio.sleep(30)

    server = await asyncio.start_server(never_answer, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    fast = type(settings)(**{**settings.__dict__, "base_url": f"https://127.0.0.1:{port}",
                             "tls_server_name": None, "connect_timeout": 0.5, "read_timeout": 0.5})
    c = WooCommerceClient(fast)
    started = time.monotonic()
    try:
        with pytest.raises(WooCommerceError, match="did not respond in time") as exc:
            await c.list_orders(page=1, per_page=1, status=None)
        assert exc.value.retryable is True
        assert time.monotonic() - started < 3
    finally:
        await c.aclose()
        server.close()


async def test_no_credentials_in_errors_or_logs(mcp, wc, caplog):
    caplog.set_level(logging.DEBUG)
    wc.get("/orders/1").mock(return_value=httpx.Response(401, json={"message": "Consumer secret is invalid."}))
    wc.get("/orders").mock(side_effect=httpx.ReadTimeout("t"))
    for args in (("get_order", {"order_id": 1}), ("list_orders", {})):
        with pytest.raises(ToolError) as exc:
            await call(mcp, *args)
        assert CK not in str(exc.value) and CS not in str(exc.value)
    assert CK not in caplog.text and CS not in caplog.text
    assert "Authorization" not in caplog.text
