"""HTTP client for the JS mock ad-server upstream.

The reference seller is a **translator**: AdCP wire on the inside,
this client over HTTP on the outside. The mock-server ships in
``@adcp/client`` — boot it via::

    npx -y -p @adcp/client@latest \\
        adcp mock-server sales-guaranteed --port 4503 --api-key test-key

The full upstream surface is documented in the mock's openapi.yaml
(under ``src/lib/mock-server/sales-guaranteed/`` in the JS repo).
This client mirrors that surface 1:1 — translation from AdCP shapes
to upstream shapes happens in :mod:`platform`, not here.

Adopters fork this module and replace the URL / auth / methods with
their real ad server's API. The shape of the methods (signatures and
return types) is what stays stable; what's inside the request body
and response parsing is whatever the adopter's upstream returns.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

logger = logging.getLogger(__name__)


class UpstreamError(Exception):
    """Raised when the upstream returns a non-2xx status.

    Carries the upstream's structured error payload (``{code, message}``
    per the mock's openapi.yaml ``Error`` schema) plus the HTTP status
    code. The platform layer decides whether to project this onto an
    AdCP wire error (typically ``UPSTREAM_FAILURE`` /
    ``MEDIA_BUY_NOT_FOUND``) or to retry / fail terminal.
    """

    def __init__(self, status_code: int, payload: dict[str, Any] | None) -> None:
        self.status_code = status_code
        self.payload = payload or {}
        message = self.payload.get("message") or f"upstream {status_code}"
        super().__init__(message)


class MockUpstreamClient:
    """httpx-based client for the sales-guaranteed mock upstream.

    Connection-pooled via ``httpx.AsyncClient``. The client carries a
    customer-level API key (constructor) and per-call routing via
    ``X-Network-Code`` (from ``ctx.account.ext['network_code']`` —
    each platform method passes the network_code through).

    ::

        client = MockUpstreamClient(
            base_url="http://127.0.0.1:4503",
            api_key="test-key",
        )
        products = await client.list_products(
            network_code="net_premium_us",
            delivery_type="guaranteed",
        )
    """

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        timeout: float = 30.0,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._timeout = timeout
        self._client: httpx.AsyncClient | None = None

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=self._base_url,
                timeout=self._timeout,
                limits=httpx.Limits(
                    max_keepalive_connections=10,
                    max_connections=20,
                ),
            )
        return self._client

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    def _headers(self, network_code: str) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._api_key}",
            "X-Network-Code": network_code,
            "Content-Type": "application/json",
        }

    async def _request(
        self,
        method: str,
        path: str,
        *,
        network_code: str,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        client = await self._get_client()
        response = await client.request(
            method,
            path,
            params=params,
            json=json,
            headers=self._headers(network_code),
        )
        if response.status_code >= 400:
            try:
                payload = response.json()
            except ValueError:
                payload = None
            raise UpstreamError(response.status_code, payload)
        if response.status_code == 204 or not response.content:
            return {}
        return response.json()  # type: ignore[no-any-return]

    # ----- products / inventory / forecast --------------------------------

    async def list_products(
        self,
        *,
        network_code: str,
        delivery_type: str | None = None,
        channel: str | None = None,
        targeting: str | None = None,
        flight_start: str | None = None,
        flight_end: str | None = None,
        budget: float | None = None,
    ) -> dict[str, Any]:
        """``GET /v1/products``.

        Query params drive per-product forecast decoration upstream
        (see openapi.yaml). Returns the raw upstream payload —
        translation to AdCP ``Product[]`` happens in
        :meth:`platform.V3ReferenceSeller.get_products`.
        """
        params: dict[str, Any] = {}
        if delivery_type is not None:
            params["delivery_type"] = delivery_type
        if channel is not None:
            params["channel"] = channel
        if targeting is not None:
            params["targeting"] = targeting
        if flight_start is not None:
            params["flight_start"] = flight_start
        if flight_end is not None:
            params["flight_end"] = flight_end
        if budget is not None:
            params["budget"] = budget
        return await self._request("GET", "/v1/products", network_code=network_code, params=params)

    async def get_forecast(
        self,
        *,
        network_code: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        """``POST /v1/forecast`` — single-product delivery forecast."""
        return await self._request("POST", "/v1/forecast", network_code=network_code, json=payload)

    # ----- orders ---------------------------------------------------------

    async def list_orders(self, *, network_code: str) -> dict[str, Any]:
        """``GET /v1/orders``."""
        return await self._request("GET", "/v1/orders", network_code=network_code)

    async def create_order(
        self,
        *,
        network_code: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        """``POST /v1/orders`` — returns ``Order`` in
        ``pending_approval`` status with an ``approval_task_id``."""
        return await self._request("POST", "/v1/orders", network_code=network_code, json=payload)

    async def get_order(self, *, network_code: str, order_id: str) -> dict[str, Any]:
        """``GET /v1/orders/{order_id}``."""
        return await self._request("GET", f"/v1/orders/{order_id}", network_code=network_code)

    async def add_line_item(
        self,
        *,
        network_code: str,
        order_id: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        """``POST /v1/orders/{order_id}/lineitems``."""
        return await self._request(
            "POST",
            f"/v1/orders/{order_id}/lineitems",
            network_code=network_code,
            json=payload,
        )

    async def attach_creative(
        self,
        *,
        network_code: str,
        order_id: str,
        line_item_id: str,
        creative_id: str,
    ) -> dict[str, Any]:
        """``POST /v1/orders/{order_id}/lineitems/{li}/creative-attach``."""
        return await self._request(
            "POST",
            f"/v1/orders/{order_id}/lineitems/{line_item_id}/creative-attach",
            network_code=network_code,
            json={"creative_id": creative_id},
        )

    async def get_delivery(
        self,
        *,
        network_code: str,
        order_id: str,
        start: str | None = None,
        end: str | None = None,
    ) -> dict[str, Any]:
        """``GET /v1/orders/{order_id}/delivery``."""
        params: dict[str, Any] = {}
        if start is not None:
            params["start"] = start
        if end is not None:
            params["end"] = end
        return await self._request(
            "GET",
            f"/v1/orders/{order_id}/delivery",
            network_code=network_code,
            params=params,
        )

    async def post_conversions(
        self,
        *,
        network_code: str,
        order_id: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        """``POST /v1/orders/{order_id}/conversions`` (CAPI)."""
        return await self._request(
            "POST",
            f"/v1/orders/{order_id}/conversions",
            network_code=network_code,
            json=payload,
        )

    # ----- creatives ------------------------------------------------------

    async def list_creatives(self, *, network_code: str) -> dict[str, Any]:
        """``GET /v1/creatives``."""
        return await self._request("GET", "/v1/creatives", network_code=network_code)

    async def upload_creative(
        self,
        *,
        network_code: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        """``POST /v1/creatives``."""
        return await self._request("POST", "/v1/creatives", network_code=network_code, json=payload)

    # ----- async approval task --------------------------------------------

    async def get_task(self, *, network_code: str, task_id: str) -> dict[str, Any]:
        """``GET /v1/tasks/{task_id}`` — poll an approval task."""
        return await self._request("GET", f"/v1/tasks/{task_id}", network_code=network_code)


__all__ = ["MockUpstreamClient", "UpstreamError"]
