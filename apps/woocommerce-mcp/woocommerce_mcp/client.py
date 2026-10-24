"""Minimal WooCommerce REST v3 client covering only the operations the MCP tools need.

There is deliberately no generic "request" method exposed to tools: each
public method maps to one fixed endpoint and HTTP verb.
"""

from __future__ import annotations

import logging
import ssl
import time
from typing import Any

import certifi
import httpx

from .config import Settings

log = logging.getLogger(__name__)

API_PREFIX = "/wp-json/wc/v3"
_MAX_UPSTREAM_MESSAGE = 300


class WooCommerceError(Exception):
    """An actionable, credential-free error suitable for returning to MCP clients."""

    def __init__(self, message: str, *, status: int | None = None, retryable: bool = False):
        super().__init__(message)
        self.message = message
        self.status = status
        self.retryable = retryable


def build_ssl_context(settings: Settings) -> ssl.SSLContext:
    """TLS context for the WooCommerce upstream only.

    Verification is always on. With WC_CA_FILE the context trusts *only* that
    CA (e.g. the local k3d dev CA); otherwise it trusts the public certifi
    bundle. No other HTTP client in the process is affected.
    """
    ctx = ssl.create_default_context(cafile=settings.ca_file or certifi.where())
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    return ctx


def _upstream_message(response: httpx.Response) -> str:
    try:
        body = response.json()
    except ValueError:
        return ""
    if isinstance(body, dict):
        msg = body.get("message")
        code = body.get("code")
        if isinstance(msg, str):
            text = f"{msg} ({code})" if isinstance(code, str) else msg
            return text[:_MAX_UPSTREAM_MESSAGE]
    return ""


class WooCommerceClient:
    def __init__(self, settings: Settings, *, transport: httpx.AsyncBaseTransport | None = None):
        self._settings = settings
        headers = {"Accept": "application/json", "User-Agent": "urumi-woocommerce-mcp/0.1"}
        if settings.tls_server_name:
            headers["Host"] = settings.tls_server_name
        self._extensions = (
            {"sni_hostname": settings.tls_server_name} if settings.tls_server_name else {}
        )
        self._http = httpx.AsyncClient(
            base_url=settings.base_url + API_PREFIX,
            auth=httpx.BasicAuth(settings.consumer_key, settings.consumer_secret),
            headers=headers,
            verify=build_ssl_context(settings),
            timeout=httpx.Timeout(
                connect=settings.connect_timeout,
                read=settings.read_timeout,
                write=settings.read_timeout,
                pool=settings.connect_timeout,
            ),
            limits=httpx.Limits(max_connections=10, max_keepalive_connections=5),
            # Never follow redirects: a redirect would be unexpected for the
            # REST API and must not carry credentials anywhere else.
            follow_redirects=False,
            trust_env=False,
            transport=transport,
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
    ) -> httpx.Response:
        started = time.monotonic()
        try:
            response = await self._http.request(
                method, path, params=params, json=json, extensions=self._extensions
            )
        except httpx.TimeoutException as exc:
            log.warning("woocommerce %s %s timed out (%s)", method, path, type(exc).__name__)
            raise WooCommerceError(
                "WooCommerce did not respond in time "
                f"(connect {self._settings.connect_timeout:g}s / read {self._settings.read_timeout:g}s). "
                "Retry later; if this persists, check the WordPress pod.",
                retryable=True,
            ) from None
        except httpx.TransportError as exc:
            cause = exc.__cause__ or exc.__context__
            if isinstance(cause, ssl.SSLCertVerificationError) or "CERTIFICATE_VERIFY_FAILED" in str(exc):
                log.error("woocommerce TLS verification failed for %s", path)
                raise WooCommerceError(
                    "TLS certificate verification for the WooCommerce upstream failed. "
                    "Check WC_CA_FILE / WC_TLS_SERVER_NAME and the Ingress certificate.",
                ) from None
            log.warning("woocommerce %s %s transport error (%s)", method, path, type(exc).__name__)
            raise WooCommerceError(
                f"Could not reach WooCommerce ({type(exc).__name__}). Retry later.",
                retryable=True,
            ) from None

        elapsed_ms = (time.monotonic() - started) * 1000
        log.info("woocommerce %s %s -> %s in %.0fms", method, path, response.status_code, elapsed_ms)

        if response.is_success:
            return response
        detail = _upstream_message(response)
        suffix = f": {detail}" if detail else ""
        status = response.status_code
        if status == 400:
            raise WooCommerceError(f"WooCommerce rejected the request{suffix}", status=status)
        if status == 401:
            raise WooCommerceError(
                "WooCommerce rejected the server's API credentials (401). "
                "An operator must check the woocommerce-api-key Secret and the init Job.",
                status=status,
            )
        if status == 403:
            raise WooCommerceError(
                "The MCP server's WooCommerce API key is not permitted to do this (403).",
                status=status,
            )
        if status == 404:
            raise WooCommerceError(f"Not found in WooCommerce{suffix}", status=status)
        if 300 <= status < 400:
            raise WooCommerceError(
                f"Unexpected redirect from WooCommerce (HTTP {status}); check WC_BASE_URL.",
                status=status,
            )
        if status == 429 or status >= 500:
            raise WooCommerceError(
                f"WooCommerce is temporarily unavailable (HTTP {status}). Retry later.",
                status=status,
                retryable=True,
            )
        raise WooCommerceError(f"WooCommerce returned HTTP {status}{suffix}", status=status)

    @staticmethod
    def _json(response: httpx.Response) -> Any:
        try:
            return response.json()
        except ValueError:
            raise WooCommerceError(
                "WooCommerce returned a non-JSON response; check WC_BASE_URL and the Ingress.",
                status=response.status_code,
            ) from None

    @staticmethod
    def _total(response: httpx.Response, header: str) -> int | None:
        value = response.headers.get(header)
        return int(value) if value and value.isdigit() else None

    async def list_products(self, *, page: int, per_page: int, search: str | None) -> tuple[list[dict], int | None, int | None]:
        params: dict[str, Any] = {"page": page, "per_page": per_page, "orderby": "date", "order": "desc"}
        if search:
            params["search"] = search
        r = await self._request("GET", "/products", params=params)
        data = self._json(r)
        if not isinstance(data, list):
            raise WooCommerceError("Unexpected product list payload from WooCommerce.")
        return data, self._total(r, "X-WP-Total"), self._total(r, "X-WP-TotalPages")

    async def create_product(self, payload: dict[str, Any]) -> dict:
        r = await self._request("POST", "/products", json=payload)
        return self._expect_object(self._json(r))

    async def list_orders(self, *, page: int, per_page: int, status: str | None) -> tuple[list[dict], int | None, int | None]:
        params: dict[str, Any] = {"page": page, "per_page": per_page, "orderby": "date", "order": "desc"}
        if status:
            params["status"] = status
        r = await self._request("GET", "/orders", params=params)
        data = self._json(r)
        if not isinstance(data, list):
            raise WooCommerceError("Unexpected order list payload from WooCommerce.")
        return data, self._total(r, "X-WP-Total"), self._total(r, "X-WP-TotalPages")

    async def get_order(self, order_id: int) -> dict:
        r = await self._request("GET", f"/orders/{int(order_id)}")
        return self._expect_object(self._json(r))

    async def update_order_status(self, order_id: int, status: str) -> dict:
        r = await self._request("PUT", f"/orders/{int(order_id)}", json={"status": status})
        return self._expect_object(self._json(r))

    @staticmethod
    def _expect_object(data: Any) -> dict:
        if not isinstance(data, dict):
            raise WooCommerceError("Unexpected payload from WooCommerce.")
        return data
