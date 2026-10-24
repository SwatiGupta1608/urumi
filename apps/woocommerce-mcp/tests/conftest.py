import pytest
import respx

from woocommerce_mcp.app import build_mcp
from woocommerce_mcp.client import WooCommerceClient
from woocommerce_mcp.config import Settings

# Obviously fake, correctly shaped credentials.
CK = "ck_" + "a" * 40
CS = "cs_" + "b" * 40
BASE = "https://traefik.test"
API = BASE + "/wp-json/wc/v3"


@pytest.fixture
def settings() -> Settings:
    return Settings(
        base_url=BASE,
        consumer_key=CK,
        consumer_secret=CS,
        tls_server_name="shop.test",
        connect_timeout=1.0,
        read_timeout=2.0,
    )


@pytest.fixture
async def client(settings):
    c = WooCommerceClient(settings)
    yield c
    await c.aclose()


@pytest.fixture
def mcp(client, settings):
    return build_mcp(lambda: client, settings)


@pytest.fixture
def wc():
    with respx.mock(base_url=API, assert_all_called=False) as router:
        yield router


async def call(mcp, name, args):
    """Call a tool and return its structured result."""
    _, structured = await mcp.call_tool(name, args)
    return structured


def product(**over):
    d = {
        "id": 11, "name": "Spike product", "status": "publish", "type": "simple", "sku": "",
        "regular_price": "9.99", "price": "9.99", "stock_status": "instock",
        "permalink": "https://shop.test/product/spike-product/", "date_created": "2026-10-24T05:00:00",
    }
    d.update(over)
    return d


def order(**over):
    d = {
        "id": 42, "number": "42", "status": "pending", "currency": "USD", "total": "12.00",
        "date_created": "2026-10-24T05:00:00", "date_modified": "2026-10-24T05:00:00",
        "customer_id": 0, "billing": {"first_name": "Demo", "last_name": "Buyer", "email": "demo@example.invalid"},
        "payment_method_title": "Cash", "shipping_total": "0.00", "discount_total": "0.00",
        "customer_note": "", "line_items": [{"product_id": 11, "name": "Demo item", "quantity": 2, "total": "12.00"}],
    }
    d.update(over)
    return d
