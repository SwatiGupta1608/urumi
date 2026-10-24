"""Tool input types (validated by FastMCP/pydantic) and structured outputs."""

from __future__ import annotations

import re
from decimal import Decimal
from typing import Annotated, Any, Literal

from pydantic import AfterValidator, BaseModel, Field

MAX_PER_PAGE = 50
MAX_PAGE = 1000
MAX_ID = 10**12

# WooCommerce core order statuses (REST names, without the "wc-" prefix).
OrderStatus = Literal["pending", "processing", "on-hold", "completed", "cancelled", "refunded", "failed"]

# Statuses update_order_status may set. "refunded" is excluded on purpose:
# setting it through the REST status field records a refund without moving
# any money, so refunds must go through a proper refund flow, not this tool.
UpdatableOrderStatus = Literal["pending", "processing", "on-hold", "completed", "cancelled", "failed"]
UPDATABLE_ORDER_STATUSES: tuple[str, ...] = UpdatableOrderStatus.__args__  # type: ignore[attr-defined]
ORDER_STATUSES: tuple[str, ...] = OrderStatus.__args__  # type: ignore[attr-defined]

_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def _clean_text(value: str) -> str:
    value = value.strip()
    if _CONTROL_CHARS.search(value):
        raise ValueError("must not contain control characters")
    return value


def _non_empty(value: str) -> str:
    if not value:
        raise ValueError("must not be blank")
    return value


def _price(value: Decimal) -> Decimal:
    if not value.is_finite():
        raise ValueError("must be a finite number")
    if value.as_tuple().exponent < -2:  # type: ignore[operator]
        raise ValueError("must have at most 2 decimal places")
    return value.quantize(Decimal("0.01"))


Page = Annotated[int, Field(ge=1, le=MAX_PAGE, strict=True, description=f"1-based page number (1-{MAX_PAGE}).")]
PerPage = Annotated[
    int, Field(ge=1, le=MAX_PER_PAGE, strict=True, description=f"Items per page (1-{MAX_PER_PAGE}).")
]
ResourceId = Annotated[int, Field(ge=1, le=MAX_ID, strict=True, description="Positive WooCommerce ID.")]
SearchText = Annotated[
    str,
    Field(min_length=1, max_length=100, description="Free-text search on product name/content (1-100 chars)."),
    AfterValidator(_clean_text),
    AfterValidator(_non_empty),
]
ProductName = Annotated[
    str,
    Field(min_length=1, max_length=200, description="Product name shown in the store (1-200 chars)."),
    AfterValidator(_clean_text),
    AfterValidator(_non_empty),
]
Price = Annotated[
    Decimal,
    Field(
        gt=Decimal("0"),
        le=Decimal("1000000"),
        description="Regular price in the store currency, > 0 and <= 1000000, at most 2 decimals, e.g. \"19.99\".",
    ),
    AfterValidator(_price),
]
Description = Annotated[
    str,
    Field(max_length=5000, description="Optional product description (plain text or basic HTML, max 5000 chars)."),
    AfterValidator(_clean_text),
]


class Pagination(BaseModel):
    page: int
    per_page: int
    total: int | None = Field(None, description="Total matching items reported by WooCommerce.")
    total_pages: int | None = None
    has_more: bool


class Product(BaseModel):
    id: int
    name: str
    status: str
    type: str
    sku: str
    regular_price: str
    price: str
    stock_status: str | None = None
    permalink: str | None = None
    date_created: str | None = None

    @classmethod
    def from_wc(cls, d: dict[str, Any]) -> "Product":
        return cls(
            id=int(d.get("id", 0)),
            name=str(d.get("name", "")),
            status=str(d.get("status", "")),
            type=str(d.get("type", "")),
            sku=str(d.get("sku") or ""),
            regular_price=str(d.get("regular_price") or ""),
            price=str(d.get("price") or ""),
            stock_status=d.get("stock_status"),
            permalink=d.get("permalink"),
            date_created=d.get("date_created"),
        )


class ProductList(BaseModel):
    products: list[Product]
    pagination: Pagination


class LineItem(BaseModel):
    product_id: int | None
    name: str
    quantity: int
    total: str


class OrderSummary(BaseModel):
    id: int
    number: str
    status: str
    currency: str
    total: str
    date_created: str | None = None
    customer_id: int | None = None
    billing_name: str = ""
    item_count: int

    @classmethod
    def from_wc(cls, d: dict[str, Any]) -> "OrderSummary":
        billing = d.get("billing") or {}
        items = d.get("line_items") or []
        return cls(
            id=int(d.get("id", 0)),
            number=str(d.get("number", "")),
            status=str(d.get("status", "")),
            currency=str(d.get("currency", "")),
            total=str(d.get("total", "")),
            date_created=d.get("date_created"),
            customer_id=d.get("customer_id"),
            billing_name=" ".join(p for p in (billing.get("first_name"), billing.get("last_name")) if p),
            item_count=sum(int(i.get("quantity") or 0) for i in items),
        )


class Order(OrderSummary):
    date_modified: str | None = None
    billing_email: str = ""
    payment_method_title: str = ""
    shipping_total: str = ""
    discount_total: str = ""
    customer_note: str = ""
    line_items: list[LineItem]

    @classmethod
    def from_wc(cls, d: dict[str, Any]) -> "Order":
        base = OrderSummary.from_wc(d).model_dump()
        billing = d.get("billing") or {}
        return cls(
            **base,
            date_modified=d.get("date_modified"),
            billing_email=str(billing.get("email") or ""),
            payment_method_title=str(d.get("payment_method_title") or ""),
            shipping_total=str(d.get("shipping_total") or ""),
            discount_total=str(d.get("discount_total") or ""),
            customer_note=str(d.get("customer_note") or ""),
            line_items=[
                LineItem(
                    product_id=i.get("product_id"),
                    name=str(i.get("name", "")),
                    quantity=int(i.get("quantity") or 0),
                    total=str(i.get("total", "")),
                )
                for i in (d.get("line_items") or [])
            ],
        )


class OrderList(BaseModel):
    orders: list[OrderSummary]
    pagination: Pagination


class OrderStatusChange(BaseModel):
    order_id: int
    previous_status: str
    status: str
    changed: bool = Field(description="False when the order already had the requested status.")
