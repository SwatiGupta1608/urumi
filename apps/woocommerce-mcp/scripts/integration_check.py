"""Controlled end-to-end check of a deployed WooCommerce MCP server over Streamable HTTP.

Run from a pod allowed by the MCP NetworkPolicy (see README). It:
  * discovers the five tools,
  * creates ONE uniquely named product and reads it back,
  * reads demo orders,
  * if --status-test-order is given, moves ONLY that order
    pending -> on-hold -> (no-op) on-hold -> pending, verifying each step,
  * checks a few invalid inputs are rejected.

It never sees WooCommerce credentials; those stay inside the MCP server.
Usage: python integration_check.py --url http://shop-woocommerce-mcp:8000/mcp [--status-test-order 17] [--read-order 16]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import uuid

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

EXPECTED = {"list_products", "create_product", "list_orders", "get_order", "update_order_status"}
results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, ok, detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}{': ' + detail if detail else ''}", flush=True)


async def tool(session: ClientSession, name: str, args: dict):
    r = await session.call_tool(name, args)
    text = " ".join(c.text for c in r.content if getattr(c, "type", "") == "text")
    return r.isError, r.structuredContent, text


async def main(url: str, status_order: int | None, read_order: int | None) -> int:
    async with streamablehttp_client(url, timeout=30) as (read, write, _):
        async with ClientSession(read, write) as session:
            init = await session.initialize()
            check("initialize", init.serverInfo.name == "woocommerce", f"server={init.serverInfo.name}")
            tools = {t.name for t in (await session.list_tools()).tools}
            check("discover five tools", tools == EXPECTED, ",".join(sorted(tools)))

            name = f"MCP integration {uuid.uuid4().hex[:10]}"
            err, p, text = await tool(session, "create_product", {"name": name, "regular_price": "3.21", "description": "Created by integration_check.py"})
            check("create_product", not err and p and p["name"] == name and p["regular_price"] == "3.21", text if err else f"id={p['id']}")
            if not err:
                err, lst, text = await tool(session, "list_products", {"search": name, "per_page": 5})
                ids = [x["id"] for x in (lst or {}).get("products", [])]
                check("list_products read-back", not err and p["id"] in ids, text if err else f"ids={ids}")

            err, lst, text = await tool(session, "list_orders", {"per_page": 5})
            check("list_orders", not err and lst["pagination"]["per_page"] == 5,
                  text if err else json.dumps([(o["id"], o["status"]) for o in lst["orders"]]))
            err, lst, text = await tool(session, "list_orders", {"status": "processing", "per_page": 50})
            check("list_orders status filter", not err and all(o["status"] == "processing" for o in lst["orders"]),
                  text if err else f"{len(lst['orders'])} processing")

            if read_order:
                err, o, text = await tool(session, "get_order", {"order_id": read_order})
                check("get_order", not err and o["id"] == read_order and o["line_items"], text if err else f"status={o['status']} total={o['total']}")

            err, _, text = await tool(session, "get_order", {"order_id": 999999999})
            check("get_order unknown id -> actionable error", err and "Not found" in text, text[:120])

            for bad_name, bad_args in [
                ("list_orders per_page=51", ("list_orders", {"per_page": 51})),
                ("update_order_status refunded", ("update_order_status", {"order_id": 1, "status": "refunded"})),
                ("create_product price=0", ("create_product", {"name": "x", "regular_price": "0"})),
            ]:
                err, _, text = await tool(session, *bad_args)
                check(f"rejects {bad_name}", err, text[:100])

            if status_order:
                err, o, text = await tool(session, "get_order", {"order_id": status_order})
                if err or o["status"] != "pending":
                    check("status-test order is pending before test", False, text or o["status"])
                else:
                    check("status-test order is pending before test", True, f"order {status_order}")
                    for target, expect_changed in [("on-hold", True), ("on-hold", False), ("pending", True)]:
                        err, ch, text = await tool(session, "update_order_status", {"order_id": status_order, "status": target})
                        ok = not err and ch["status"] == target and ch["changed"] is expect_changed
                        check(f"update_order_status -> {target}", ok, text if err else json.dumps(ch))
                        err, o, _ = await tool(session, "get_order", {"order_id": status_order})
                        check(f"verify order {status_order} is {target}", not err and o["status"] == target)

    failed = [r for r in results if not r[1]]
    print(f"\n{len(results) - len(failed)}/{len(results)} checks passed")
    return 1 if failed else 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", required=True)
    ap.add_argument("--status-test-order", type=int)
    ap.add_argument("--read-order", type=int)
    a = ap.parse_args()
    sys.exit(asyncio.run(main(a.url, a.status_test_order, a.read_order)))
