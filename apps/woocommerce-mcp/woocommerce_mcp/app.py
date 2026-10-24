"""FastAPI app hosting the WooCommerce MCP server over Streamable HTTP.

Run with:  uvicorn --factory woocommerce_mcp.app:create_app --host 0.0.0.0 --port 8000
MCP endpoint: POST /mcp     Health: GET /healthz (liveness), GET /readyz (readiness)
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import Annotated, Callable, Literal

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings
from pydantic import Field

from .client import WooCommerceClient, WooCommerceError
from .config import Settings
from .models import (
    Description,
    Order,
    OrderList,
    OrderStatus,
    OrderStatusChange,
    OrderSummary,
    Page,
    Pagination,
    PerPage,
    Price,
    Product,
    ProductList,
    ProductName,
    ResourceId,
    SearchText,
    UpdatableOrderStatus,
)

log = logging.getLogger("woocommerce_mcp")

INSTRUCTIONS = (
    "Tools for one WooCommerce store: list/search and create simple products, "
    "list and read orders, and change an order's status to an allowed value. "
    "Store credentials are held by the server; never ask the user for API keys."
)


def _pagination(page: int, per_page: int, count: int, total: int | None, total_pages: int | None) -> Pagination:
    has_more = page < total_pages if total_pages is not None else count == per_page
    return Pagination(page=page, per_page=per_page, total=total, total_pages=total_pages, has_more=has_more)


def _tool_error(tool: str, exc: WooCommerceError) -> ToolError:
    log.info("tool %s failed: status=%s retryable=%s", tool, exc.status, exc.retryable)
    return ToolError(exc.message)


def build_mcp(get_client: Callable[[], WooCommerceClient], settings: Settings) -> FastMCP:
    mcp = FastMCP(
        "woocommerce",
        instructions=INSTRUCTIONS,
        stateless_http=True,
        json_response=True,
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=list(settings.allowed_hosts),
            allowed_origins=list(settings.allowed_origins),
        ),
    )

    @mcp.tool(title="List products")
    async def list_products(
        page: Page = 1,
        per_page: PerPage = 10,
        search: Annotated[SearchText | None, Field(description="Optional search text.")] = None,
    ) -> ProductList:
        """List or search store products, newest first, with bounded pagination (max 50 per page)."""
        try:
            items, total, pages = await get_client().list_products(page=page, per_page=per_page, search=search)
        except WooCommerceError as exc:
            raise _tool_error("list_products", exc) from None
        return ProductList(
            products=[Product.from_wc(p) for p in items],
            pagination=_pagination(page, per_page, len(items), total, pages),
        )

    @mcp.tool(title="Create product")
    async def create_product(
        name: ProductName,
        regular_price: Price,
        description: Annotated[Description | None, Field(description="Optional description.")] = None,
        status: Annotated[
            Literal["publish", "draft"],
            Field(description='"publish" (visible in the store) or "draft".'),
        ] = "publish",
    ) -> Product:
        """Create a simple product with a name, a regular price and an optional description."""
        payload = {"name": name, "type": "simple", "regular_price": str(regular_price), "status": status}
        if description:
            payload["description"] = description
        try:
            created = await get_client().create_product(payload)
        except WooCommerceError as exc:
            raise _tool_error("create_product", exc) from None
        return Product.from_wc(created)

    @mcp.tool(title="List orders")
    async def list_orders(
        page: Page = 1,
        per_page: PerPage = 10,
        status: Annotated[OrderStatus | None, Field(description="Only orders with this status.")] = None,
    ) -> OrderList:
        """List orders, newest first, optionally filtered by status, with bounded pagination (max 50 per page)."""
        try:
            items, total, pages = await get_client().list_orders(page=page, per_page=per_page, status=status)
        except WooCommerceError as exc:
            raise _tool_error("list_orders", exc) from None
        return OrderList(
            orders=[OrderSummary.from_wc(o) for o in items],
            pagination=_pagination(page, per_page, len(items), total, pages),
        )

    @mcp.tool(title="Get order")
    async def get_order(order_id: ResourceId) -> Order:
        """Get one order by ID, including line items, totals and billing contact."""
        try:
            data = await get_client().get_order(order_id)
        except WooCommerceError as exc:
            raise _tool_error("get_order", exc) from None
        return Order.from_wc(data)

    @mcp.tool(title="Update order status")
    async def update_order_status(
        order_id: ResourceId,
        status: Annotated[
            UpdatableOrderStatus,
            Field(description="New status: pending, processing, on-hold, completed, cancelled or failed. "
                  "Refunds are not supported by this tool."),
        ],
    ) -> OrderStatusChange:
        """Change an order's status to an allowed WooCommerce status. Returns the previous and new status.

        WooCommerce may send customer emails and adjust stock as part of a status change.
        """
        client = get_client()
        try:
            current = await client.get_order(order_id)
            previous = str(current.get("status", ""))
            if previous == status:
                return OrderStatusChange(order_id=order_id, previous_status=previous, status=status, changed=False)
            updated = await client.update_order_status(order_id, status)
        except WooCommerceError as exc:
            raise _tool_error("update_order_status", exc) from None
        new_status = str(updated.get("status", ""))
        if new_status != status:
            raise ToolError(
                f"WooCommerce kept order {order_id} at '{new_status}' instead of '{status}'; "
                "a plugin or order rule may have blocked the change."
            )
        log.info("order %s status %s -> %s", order_id, previous, new_status)
        return OrderStatusChange(order_id=order_id, previous_status=previous, status=new_status, changed=True)

    return mcp


def create_app(settings: Settings | None = None, client: WooCommerceClient | None = None) -> FastAPI:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    # httpx logs full request lines at INFO; keep our own concise log instead.
    logging.getLogger("httpx").setLevel(logging.WARNING)

    settings = settings or Settings.from_env()
    state: dict[str, WooCommerceClient] = {}

    def get_client() -> WooCommerceClient:
        return state["client"]

    mcp = build_mcp(get_client, settings)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        state["client"] = client or WooCommerceClient(settings)
        log.info(
            "starting: upstream=%s server_name=%s custom_ca=%s",
            settings.base_url, settings.tls_server_name or "-", bool(settings.ca_file),
        )
        try:
            async with mcp.session_manager.run():
                yield
        finally:
            await state.pop("client").aclose()

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)

    @app.get("/healthz", include_in_schema=False)
    async def healthz() -> JSONResponse:
        return JSONResponse({"status": "ok"})

    @app.get("/readyz", include_in_schema=False)
    async def readyz() -> JSONResponse:
        # Ready once configuration loaded and the upstream client exists.
        # Upstream availability is reported per tool call instead, so a
        # WooCommerce outage does not flap this pod out of the Service.
        ready = "client" in state
        return JSONResponse({"status": "ready" if ready else "starting"}, status_code=200 if ready else 503)

    app.mount("/", mcp.streamable_http_app())
    return app
