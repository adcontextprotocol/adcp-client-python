"""Adopter-facing type checks for negotiated catalog purchase inputs."""

from __future__ import annotations

from typing_extensions import assert_type

from adcp.compat import (
    CompatibleCatalog,
    CompatiblePurchaseResult,
    CoordinatorBuyProductsInput,
    LegacyCatalogContinuation,
    MediaBuyLifecycleCoordinator,
)

purchase_input: CoordinatorBuyProductsInput = {
    "idempotency_key": "catalog-purchase-2026-09-13-0001",
    "account": {"account_id": "account-acme"},
    "brand": {"domain": "acme.example"},
    "purchases": [
        {
            "product_id": "product-1",
            "pricing_option_id": "fixed-cpm",
            "budget": 1_000,
        }
    ],
    "total_budget": {"amount": 1_000, "currency": "USD"},
    "start_time": "2026-10-01T00:00:00Z",
    "end_time": "2026-11-01T00:00:00Z",
}


async def purchase(
    coordinator: MediaBuyLifecycleCoordinator,
    listing: CompatibleCatalog,
    continuation: LegacyCatalogContinuation,
) -> None:
    assert_type(
        await coordinator.buy_products(listing, purchase_input),
        CompatiblePurchaseResult,
    )
    assert_type(
        await coordinator.continue_legacy_purchase(continuation, purchase_input),
        CompatiblePurchaseResult,
    )
