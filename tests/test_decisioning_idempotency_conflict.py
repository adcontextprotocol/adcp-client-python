"""A decisioning method wrapped by ``IdempotencyStore.wrap`` answers a reused
key with ``IDEMPOTENCY_CONFLICT``, not ``INTERNAL_ERROR`` (#1622).

The conflict is an :class:`~adcp.exceptions.ADCPTaskError` raised inside the
platform method, so the decisioning dispatcher must project it to its own wire
error, as the handler ``serve()`` path does. Both requests run through the real
MCP and A2A servers.
"""

from __future__ import annotations

import secrets
from typing import Any

import pytest

from adcp.decisioning import DecisioningCapabilities, DecisioningPlatform, SingletonAccounts
from adcp.server import IdempotencyStore, MemoryBackend, ToolContext
from adcp.server.helpers import STANDARD_ERROR_CODES
from adcp.testing import build_test_client
from adcp.types import CreateMediaBuySuccessResponse, Package

store = IdempotencyStore(backend=MemoryBackend(), ttl_seconds=86_400)


class Seller(DecisioningPlatform):
    capabilities = DecisioningCapabilities(
        specialisms=["sales-non-guaranteed"], channels=["display"], pricing_models=["cpm"]
    )
    accounts = SingletonAccounts(account_id="acct_1")

    def __init__(self) -> None:
        self.created = 0

    @store.wrap
    async def create_media_buy(self, req: Any, ctx: Any) -> CreateMediaBuySuccessResponse:
        self.created += 1
        return CreateMediaBuySuccessResponse(
            media_buy_id=f"mb_{self.created}",
            confirmed_at="2026-05-01T00:00:00Z",
            revision=1,
            status="active",
            packages=[Package(package_id="pkg_1", product_id="prod_1")],
        )

    def get_products(self, req: Any, ctx: Any) -> Any:
        raise NotImplementedError

    def update_media_buy(self, media_buy_id: Any, patch: Any, ctx: Any) -> Any:
        raise NotImplementedError

    def sync_creatives(self, req: Any, ctx: Any) -> Any:
        raise NotImplementedError

    def get_media_buy_delivery(self, req: Any, ctx: Any) -> Any:
        raise NotImplementedError

    def get_media_buys(self, req: Any, ctx: Any) -> Any:
        raise NotImplementedError


def _create_request(key: str, budget: int) -> dict[str, Any]:
    return {
        "idempotency_key": key,
        "account": {"account_id": "acct_1"},
        "brand": {"domain": "brand.example"},
        "start_time": "2026-10-01T00:00:00Z",
        "end_time": "2026-10-31T00:00:00Z",
        "packages": [
            {
                "product_id": "prod_1",
                "pricing_option_id": "cpm_1",
                "budget": budget,
            }
        ],
    }


async def _call(client: Any, transport: str, arguments: dict[str, Any]) -> dict[str, Any]:
    if transport == "a2a":
        response = await client.post(
            "/",
            json={
                "jsonrpc": "2.0",
                "id": secrets.token_hex(4),
                "method": "message/send",
                "params": {
                    "message": {
                        "messageId": secrets.token_hex(8),
                        "role": "user",
                        "parts": [
                            {
                                "kind": "data",
                                "data": {"skill": "create_media_buy", "parameters": arguments},
                            }
                        ],
                    }
                },
            },
        )
        assert response.status_code == 200, response.text
        result = response.json()["result"]
        return next(p["data"] for p in result["artifacts"][0]["parts"] if "data" in p)
    response = await client.post(
        "/mcp/",
        headers={"accept": "application/json, text/event-stream"},
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "create_media_buy", "arguments": arguments},
        },
    )
    assert response.status_code == 200, response.text
    return response.json()["result"]["structuredContent"]


def _caller(meta: Any) -> ToolContext:
    return ToolContext(caller_identity="buyer_1")


@pytest.mark.parametrize("transport", ["mcp", "a2a"])
async def test_reused_key_with_different_payload_returns_idempotency_conflict(
    transport: str,
) -> None:
    seller = Seller()
    key = f"idem-{secrets.token_hex(8)}"
    async with build_test_client(
        seller, transport=transport, stateless_http=True, context_factory=_caller
    ) as client:
        first = await _call(client, transport, _create_request(key, 1000))
        assert "adcp_error" not in first, first
        second = await _call(client, transport, _create_request(key, 2000))

    error = second["adcp_error"]
    assert error["code"] == "IDEMPOTENCY_CONFLICT", second
    assert error["recovery"] == STANDARD_ERROR_CODES["IDEMPOTENCY_CONFLICT"]["recovery"]
    assert error["recovery"] == "correctable"
    assert "caused_by" not in error.get("details", {})
    assert seller.created == 1
