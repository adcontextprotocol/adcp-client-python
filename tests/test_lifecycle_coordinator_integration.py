"""Real client/adapter/server boundary tests for lifecycle coordination.

These tests prove Python routing and native-handler preservation. They are not
the release-pinned, cross-language response certification tracked by adcp#7439.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from adcp import ADCPClient
from adcp.compat import (
    InMemoryCompatibilityContinuationStore,
    LegacyPurchaseCoordinator,
    MediaBuyLifecycle,
    MediaBuyLifecycleCoordinator,
)
from adcp.compat.purchase_continuation import LegacyPurchaseExecution
from adcp.negotiation import compute_terms_digest
from adcp.server import ADCPHandler, create_mcp_tools
from adcp.server.responses import capabilities_response, media_buy_response
from adcp.types import LegacyCreateMediaBuyRequest, LegacyGetProductsRequest, ListProductsRequest

_VECTOR_FILE = (
    Path(__file__).parent
    / "conformance"
    / "vectors"
    / "products-only-brief-compatibility"
    / "vectors.json"
)
_VECTOR_CASES = json.loads(_VECTOR_FILE.read_text())["cases"]


class _LifecycleHandler(ADCPHandler[Any]):
    """Seller with explicit compact and established handlers over one catalog."""

    def __init__(self, version: str) -> None:
        self.version = version
        self.calls: list[tuple[str, dict[str, Any]]] = []

    @staticmethod
    def _payload(params: Any) -> dict[str, Any]:
        if hasattr(params, "model_dump"):
            return params.model_dump(mode="json", exclude_none=True)
        return dict(params)

    async def get_adcp_capabilities(self, params: Any, context: Any = None) -> dict[str, Any]:
        del params, context
        self.calls.append(("get_adcp_capabilities", {}))
        capability_kwargs: dict[str, Any] = {
            "adcp_version": self.version,
            "idempotency": {"supported": False},
        }
        # The packaged prerelease normalizes its release-precision pin before
        # comparing; let the helper supply its canonical supported-version set.
        if not self.version.startswith("3.2"):
            capability_kwargs["supported_versions"] = [self.version]
        response = capabilities_response(["media_buy"], **capability_kwargs)
        response["account"] = {
            "supported_billing": ["operator"],
            "required_for_products": True,
        }
        if self.version.startswith("3.2"):
            response["media_buy"] = {
                "lifecycle_tools": ["list_products", "buy_products"],
                "buying_modes": ["brief", "wholesale"],
            }
        elif self.version.startswith("3.1"):
            response.setdefault("media_buy", {})["buying_modes"] = ["brief", "wholesale"]
        return response

    async def list_products(self, params: Any, context: Any = None) -> dict[str, Any]:
        del context
        self.calls.append(("list_products", self._payload(params)))
        product = copy.deepcopy(_VECTOR_CASES[1]["legacy_response"]["products"][0])
        product.pop("format_ids", None)
        return {
            "outcome": "listed",
            "products": [product],
            "feed_version": "compact-feed-1",
            "pricing_version": "compact-pricing-1",
            "cache_scope": "account",
        }

    async def buy_products(self, params: Any, context: Any = None) -> dict[str, Any]:
        del context
        request = self._payload(params)
        self.calls.append(("buy_products", request))
        purchase = request["purchases"][0]
        accepted_purchase = {
            **purchase,
            "pricing": {
                "pricing_option_id": "fixed-cpm",
                "pricing_model": "cpm",
                "currency": "USD",
                "fixed_price": 12,
            },
            "start_time": request["start_time"],
            "end_time": request["end_time"],
        }
        commercial_terms = {
            "brand": request["brand"],
            "purchases": [accepted_purchase],
            "start_time": request["start_time"],
            "end_time": request["end_time"],
        }
        return {
            "status": "completed",
            "media_buy_id": "mb-compact",
            "revision": 1,
            "media_buy_status": "active",
            "confirmed_at": "2098-01-01T00:00:00Z",
            "accepted_proposal": {
                "proposal_id": "direct-compact-1",
                "proposal_kind": "new_media_buy",
                "proposal_status": "accepted",
                "media_buy_id": "mb-compact",
                "accepted_at": "2098-01-01T00:00:00Z",
                "name": "Direct compact purchase",
                "commercial_terms": commercial_terms,
                "terms_digest": compute_terms_digest(commercial_terms),
            },
            "purchase_bindings": [
                {
                    "purchase_index": 0,
                    "product_id": purchase["product_id"],
                    "package_id": "pkg-compact-1",
                }
            ],
            "available_actions": [],
        }

    async def get_products(self, params: Any, context: Any = None) -> dict[str, Any]:
        del context
        self.calls.append(("get_products", self._payload(params)))
        case_index = 2 if self.version.startswith("3.1") else 1
        response = copy.deepcopy(_VECTOR_CASES[case_index]["legacy_response"])
        # This handler intentionally serves the retained established identity
        # shape. The canonical response builder rejects legacy format_ids so a
        # seller cannot accidentally use it for a compact response.
        response["cache_scope"] = "account"
        return response

    async def create_media_buy(self, params: Any, context: Any = None) -> dict[str, Any]:
        del context
        request = self._payload(params)
        self.calls.append(("create_media_buy", request))
        return media_buy_response(
            "mb-established",
            [],
            adcp_version=self.version,
        )


class _InProcessMcpSession:
    """MCP ClientSession-shaped bridge to the SDK's real server tool callers."""

    def __init__(self, tools: Any) -> None:
        self._tools = tools

    async def list_tools(self) -> Any:
        return SimpleNamespace(
            tools=[SimpleNamespace(name=name) for name in self._tools.get_tool_names()]
        )

    async def call_tool(
        self,
        name: str,
        arguments: dict[str, Any],
        *,
        meta: Any = None,
    ) -> Any:
        del meta
        result = await self._tools.call_tool(name, arguments)
        return SimpleNamespace(isError=False, structuredContent=result, content=[])


def _client_and_tools(
    handler: _LifecycleHandler,
) -> tuple[ADCPClient, Any]:
    tools = create_mcp_tools(handler, adcp_version=handler.version)
    session = _InProcessMcpSession(tools)
    client = ADCPClient.from_mcp_client(session, agent_id=f"seller-{handler.version}")  # type: ignore[arg-type]
    return client, tools


def _list_request() -> ListProductsRequest:
    return ListProductsRequest.model_validate(
        {
            "idempotency_key": "integration-catalog-read-0001",
            "account": {"account_id": "account-acme"},
            "criteria": {"offer_filters": {"countries": ["US"]}},
        }
    )


def _purchase_request(product_id: str) -> dict[str, Any]:
    return {
        "idempotency_key": "53f1f0cb-71e4-4ea4-8d8f-f24ac23c6168",
        "account": {"account_id": "account-acme"},
        "brand": {"domain": "acme.example"},
        "purchases": [
            {
                "product_id": product_id,
                "pricing_option_id": "fixed-cpm",
                "budget": 1_000,
            }
        ],
        "start_time": "2099-01-01T00:00:00Z",
        "end_time": "2099-02-01T00:00:00Z",
    }


@pytest.mark.asyncio
async def test_compact_route_crosses_real_client_adapter_and_server_boundary() -> None:
    handler = _LifecycleHandler("3.2.0-rc.6")
    client, tools = _client_and_tools(handler)

    # Explicit established handlers stay natively available on a compact
    # seller. Registration does not synthesize these handlers; #1147 owns the
    # future opt-in reverse facade for sellers that implement only compact code.
    assert {"get_products", "create_media_buy"}.issubset(tools.get_tool_names())

    lifecycle = await MediaBuyLifecycleCoordinator.negotiate(client)
    listing = await lifecycle.list_products(_list_request())
    purchase = await lifecycle.buy_products(
        listing,
        _purchase_request(listing.products[0]["product_id"]),
    )

    assert lifecycle.negotiated_version == "3.2-rc.6"
    assert listing.compatibility.lifecycle is MediaBuyLifecycle.COMPACT
    assert purchase.success
    sent_buy = next(payload for name, payload in handler.calls if name == "buy_products")
    assert sent_buy["feed_version"] == "compact-feed-1"
    assert sent_buy["pricing_version"] == "compact-pricing-1"
    assert not any(name in {"get_products", "create_media_buy"} for name, _ in handler.calls)

    legacy_read = await client.get_products_legacy(
        LegacyGetProductsRequest.model_validate(
            {
                "adcp_version": "3.2-rc.6",
                "adcp_major_version": 3,
                "buying_mode": "wholesale",
                "account": {"account_id": "account-acme"},
                "filters": {"countries": ["US"]},
            }
        )
    )
    assert legacy_read.success
    assert handler.calls[-1][0] == "get_products"
    assert legacy_read.data is not None
    legacy_payload = legacy_read.data.model_dump(mode="json", exclude_none=True)
    returned_format = legacy_payload["products"][0]["format_ids"][0]
    expected_format = _VECTOR_CASES[1]["legacy_response"]["products"][0]["format_ids"][0]
    assert returned_format["id"] == expected_format["id"]
    # Pydantic canonicalizes a URL with an empty path to the equivalent trailing-slash form.
    assert returned_format["agent_url"].rstrip("/") == expected_format["agent_url"].rstrip("/")

    legacy_create = await client.create_media_buy_legacy(
        LegacyCreateMediaBuyRequest.model_validate(
            {
                "adcp_version": "3.2-rc.6",
                "adcp_major_version": 3,
                "idempotency_key": "compact-legacy-create-0001",
                "account": {"account_id": "account-acme"},
                "brand": {"domain": "acme.example"},
                "packages": [
                    {
                        "product_id": legacy_payload["products"][0]["product_id"],
                        "pricing_option_id": "fixed-cpm",
                        "budget": 1_000,
                    }
                ],
                "start_time": "2099-01-01T00:00:00Z",
                "end_time": "2099-02-01T00:00:00Z",
            }
        )
    )
    assert legacy_create.success
    assert handler.calls[-1][0] == "create_media_buy"


@pytest.mark.asyncio
@pytest.mark.parametrize("seller_version", ["3.0", "3.1"])
async def test_established_route_negotiates_old_response_through_real_boundary(
    seller_version: str,
) -> None:
    handler = _LifecycleHandler(seller_version)
    client, tools = _client_and_tools(handler)

    assert "get_products" in tools.get_tool_names()
    assert "create_media_buy" in tools.get_tool_names()
    assert "list_products" not in tools.get_tool_names()
    assert "buy_products" not in tools.get_tool_names()

    async def execute_legacy_create(execution: LegacyPurchaseExecution) -> Any:
        request = LegacyCreateMediaBuyRequest.model_validate(execution.legacy_create_request)
        return await client.create_media_buy_legacy(request)

    legacy = LegacyPurchaseCoordinator(
        store=InMemoryCompatibilityContinuationStore(),
        executor=execute_legacy_create,
        token_derivation_key=b"integration-only-continuation-key-32-bytes",
        allow_non_durable_store=True,
    )
    lifecycle = await MediaBuyLifecycleCoordinator.negotiate(
        client,
        legacy_purchase_coordinator=legacy,
        principal_id="principal-acme",
        target_binding=f"bound-session-{seller_version}",
        allowed_losses=[
            "feed_version_not_atomic",
            "pricing_version_not_atomic",
            "mutation_idempotency_not_guaranteed",
        ],
    )
    listing = await lifecycle.list_products(_list_request())
    purchase = await lifecycle.buy_products(
        listing,
        _purchase_request(listing.products[0]["product_id"]),
    )

    assert lifecycle.negotiated_version == seller_version
    assert listing.compatibility.lifecycle is MediaBuyLifecycle.ESTABLISHED
    assert purchase.success
    routed = [name for name, _ in handler.calls]
    assert "get_products" in routed
    assert "create_media_buy" in routed
    assert "list_products" not in routed
    assert "buy_products" not in routed
