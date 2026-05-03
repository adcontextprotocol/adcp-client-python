"""Domain helpers for the JS mock ad-server upstream.

The reference seller is a **translator**: AdCP wire on the inside, this
upstream's HTTP API on the outside. The mock-server ships in
``@adcp/client`` — boot it via::

    npx -y -p @adcp/client@latest \\
        adcp mock-server sales-guaranteed --port 4503 --api-key test-key

The full upstream surface is documented in the mock's openapi.yaml
(under ``src/lib/mock-server/sales-guaranteed/`` in the JS repo). The
helpers in this module mirror that surface 1:1 — translation from AdCP
shapes to upstream shapes happens in :mod:`platform`, not here.

Each helper takes an :class:`adcp.decisioning.UpstreamHttpClient` (the
SDK's pooled httpx wrapper handling auth + 4xx/5xx → ``AdcpError``
projection) and a ``network_code`` (the upstream's per-call routing key,
sent as the ``X-Network-Code`` header). The SDK projects upstream
non-2xx responses onto :class:`adcp.decisioning.AdcpError` with
spec-conformant codes (401 → ``AUTH_REQUIRED``, 403 →
``PERMISSION_DENIED``, 404 → ``not_found_code`` (default
``MEDIA_BUY_NOT_FOUND``, override per-call), 429 → ``RATE_LIMITED``,
5xx → ``SERVICE_UNAVAILABLE``, other 4xx → ``INVALID_REQUEST``).

Adopters fork this module and replace the URL / auth / methods with
their real ad server's API. The shape of the helpers (signatures and
return types) is what stays stable; what's inside the request body
and response parsing is whatever the adopter's upstream returns.
"""

from __future__ import annotations

from typing import Any

from adcp.decisioning import UpstreamHttpClient


def _network_headers(network_code: str) -> dict[str, str]:
    """``X-Network-Code`` header for the per-call routing the mock-server
    expects. Auth (``Authorization: Bearer ...``) is injected by the
    :class:`UpstreamHttpClient`'s :class:`StaticBearer` and is not set
    here.
    """
    return {"X-Network-Code": network_code}


# ----- products / inventory / forecast -----------------------------------


async def list_products(
    client: UpstreamHttpClient,
    *,
    network_code: str,
    delivery_type: str | None = None,
    channel: str | None = None,
    targeting: str | None = None,
    flight_start: str | None = None,
    flight_end: str | None = None,
    budget: float | None = None,
    not_found_code: str = "ACCOUNT_NOT_FOUND",
) -> dict[str, Any]:
    """``GET /v1/products``.

    Query params drive per-product forecast decoration upstream
    (see openapi.yaml). Returns the raw upstream payload — translation
    to AdCP ``Product[]`` happens in
    :meth:`platform.V3ReferenceSeller.get_products`.

    A 404 here means the network/account is unknown — pass
    ``not_found_code='ACCOUNT_NOT_FOUND'`` (default) so the buyer
    receives the right spec code.
    """
    params: dict[str, Any] = {
        "delivery_type": delivery_type,
        "channel": channel,
        "targeting": targeting,
        "flight_start": flight_start,
        "flight_end": flight_end,
        "budget": budget,
    }
    return await client.get(  # type: ignore[no-any-return]
        "/v1/products",
        params=params,
        headers=_network_headers(network_code),
        not_found_code=not_found_code,
    )


async def get_forecast(
    client: UpstreamHttpClient,
    *,
    network_code: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    """``POST /v1/forecast`` — single-product delivery forecast."""
    return await client.post(  # type: ignore[no-any-return]
        "/v1/forecast",
        json=payload,
        headers=_network_headers(network_code),
    )


# ----- orders ------------------------------------------------------------


async def list_orders(
    client: UpstreamHttpClient,
    *,
    network_code: str,
) -> dict[str, Any]:
    """``GET /v1/orders``."""
    return await client.get(  # type: ignore[no-any-return]
        "/v1/orders",
        headers=_network_headers(network_code),
    )


async def create_order(
    client: UpstreamHttpClient,
    *,
    network_code: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    """``POST /v1/orders`` — returns ``Order`` in ``pending_approval``
    status with an ``approval_task_id``.
    """
    return await client.post(  # type: ignore[no-any-return]
        "/v1/orders",
        json=payload,
        headers=_network_headers(network_code),
    )


async def get_order(
    client: UpstreamHttpClient,
    *,
    network_code: str,
    order_id: str,
) -> dict[str, Any]:
    """``GET /v1/orders/{order_id}``."""
    return await client.get(  # type: ignore[no-any-return]
        f"/v1/orders/{order_id}",
        headers=_network_headers(network_code),
    )


async def add_line_item(
    client: UpstreamHttpClient,
    *,
    network_code: str,
    order_id: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    """``POST /v1/orders/{order_id}/lineitems``."""
    return await client.post(  # type: ignore[no-any-return]
        f"/v1/orders/{order_id}/lineitems",
        json=payload,
        headers=_network_headers(network_code),
    )


async def attach_creative(
    client: UpstreamHttpClient,
    *,
    network_code: str,
    order_id: str,
    line_item_id: str,
    creative_id: str,
) -> dict[str, Any]:
    """``POST /v1/orders/{order_id}/lineitems/{li}/creative-attach``."""
    return await client.post(  # type: ignore[no-any-return]
        f"/v1/orders/{order_id}/lineitems/{line_item_id}/creative-attach",
        json={"creative_id": creative_id},
        headers=_network_headers(network_code),
    )


async def get_delivery(
    client: UpstreamHttpClient,
    *,
    network_code: str,
    order_id: str,
    start: str | None = None,
    end: str | None = None,
) -> dict[str, Any]:
    """``GET /v1/orders/{order_id}/delivery``."""
    return await client.get(  # type: ignore[no-any-return]
        f"/v1/orders/{order_id}/delivery",
        params={"start": start, "end": end},
        headers=_network_headers(network_code),
    )


async def post_conversions(
    client: UpstreamHttpClient,
    *,
    network_code: str,
    order_id: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    """``POST /v1/orders/{order_id}/conversions`` (CAPI)."""
    return await client.post(  # type: ignore[no-any-return]
        f"/v1/orders/{order_id}/conversions",
        json=payload,
        headers=_network_headers(network_code),
    )


# ----- creatives ---------------------------------------------------------


async def list_creatives(
    client: UpstreamHttpClient,
    *,
    network_code: str,
    not_found_code: str = "ACCOUNT_NOT_FOUND",
) -> dict[str, Any]:
    """``GET /v1/creatives``.

    A 404 here means the network/account is unknown (no creatives ever
    existed under it) — surface as ``ACCOUNT_NOT_FOUND``.
    """
    return await client.get(  # type: ignore[no-any-return]
        "/v1/creatives",
        headers=_network_headers(network_code),
        not_found_code=not_found_code,
    )


async def upload_creative(
    client: UpstreamHttpClient,
    *,
    network_code: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    """``POST /v1/creatives``."""
    return await client.post(  # type: ignore[no-any-return]
        "/v1/creatives",
        json=payload,
        headers=_network_headers(network_code),
    )


# ----- async approval task ----------------------------------------------


async def get_task(
    client: UpstreamHttpClient,
    *,
    network_code: str,
    task_id: str,
) -> dict[str, Any]:
    """``GET /v1/tasks/{task_id}`` — poll an approval task."""
    return await client.get(  # type: ignore[no-any-return]
        f"/v1/tasks/{task_id}",
        headers=_network_headers(network_code),
    )


__all__ = [
    "add_line_item",
    "attach_creative",
    "create_order",
    "get_delivery",
    "get_forecast",
    "get_order",
    "get_task",
    "list_creatives",
    "list_orders",
    "list_products",
    "post_conversions",
    "upload_creative",
]
