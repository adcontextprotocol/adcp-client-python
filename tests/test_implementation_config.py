"""Tests for adcp.decisioning.implementation_config + PlatformHandler wiring.

Covers:
- Full 3-product-id lookup: adopter sees complete configs dict.
- Partial return (2-of-3): adopter sees partial dict, no framework error.
- Store raises: translates to SERVICE_UNAVAILABLE.
- packages=None (proposal_id flow): no lookup, adopter sees configs={}.
- No store wired: adopter sees configs={} without store being called.
- Adopter without 'configs' kwarg: lookup skipped even when store wired.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from adcp.decisioning import (
    AdcpError,
    DecisioningCapabilities,
    DecisioningPlatform,
    InMemoryTaskRegistry,
    ProductConfigStore,
    SingletonAccounts,
)
from adcp.decisioning.handler import PlatformHandler
from adcp.decisioning.implementation_config import ProductConfigStore as _ProductConfigStoreProto
from adcp.server.base import ToolContext


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def executor():
    pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="test-config-store-")
    yield pool
    pool.shutdown(wait=True)


def _make_request(product_ids: list[str] | None):
    """Build a minimal CreateMediaBuyRequest with the given product ids.

    Passing None simulates a proposal_id-driven request (no packages).
    """
    from adcp.types import CreateMediaBuyRequest

    packages = None
    if product_ids is not None:
        packages = [
            {"product_id": pid, "budget": 1000.0, "pricing_option_id": "cpm_flat"}
            for pid in product_ids
        ]

    return CreateMediaBuyRequest(
        idempotency_key="test-key-12345678",
        account={"account_id": "acct_test"},
        brand={"domain": "example.com"},
        start_time="2026-06-01T00:00:00Z",
        end_time="2026-07-01T00:00:00Z",
        packages=packages,
        proposal_id=None if product_ids is not None else "prop-123",
    )


def _make_handler(
    platform: DecisioningPlatform,
    executor: ThreadPoolExecutor,
    config_store: Any = None,
) -> PlatformHandler:
    return PlatformHandler(
        platform,
        executor=executor,
        registry=InMemoryTaskRegistry(),
        config_store=config_store,
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_full_lookup_all_three_products(executor) -> None:
    """Store returns configs for all 3 product_ids; adopter sees full dict."""
    received_configs: list[dict] = []

    class _Store:
        async def lookup_implementation_configs(self, product_ids, ctx):
            return {pid: {"line_item_id": f"li_{pid}"} for pid in product_ids}

    class _Platform(DecisioningPlatform):
        capabilities = DecisioningCapabilities()
        accounts = SingletonAccounts(account_id="seller")

        async def create_media_buy(self, req, ctx, configs=None):
            received_configs.append(configs or {})
            from adcp.types import CreateMediaBuySuccessResponse

            return CreateMediaBuySuccessResponse(media_buy_id="mb_001", packages=[])

    handler = _make_handler(_Platform(), executor, config_store=_Store())
    req = _make_request(["pid1", "pid2", "pid3"])
    await handler.create_media_buy(req, ToolContext())

    assert len(received_configs) == 1
    assert set(received_configs[0].keys()) == {"pid1", "pid2", "pid3"}
    assert received_configs[0]["pid1"] == {"line_item_id": "li_pid1"}


@pytest.mark.asyncio
async def test_partial_return_adopter_sees_subset(executor) -> None:
    """Store returns only 2 of 3 configs; framework does not fail."""
    received_configs: list[dict] = []

    class _Store:
        async def lookup_implementation_configs(self, product_ids, ctx):
            # Intentionally omit pid3
            return {
                "pid1": {"kind": "display"},
                "pid2": {"kind": "video"},
            }

    class _Platform(DecisioningPlatform):
        capabilities = DecisioningCapabilities()
        accounts = SingletonAccounts(account_id="seller")

        async def create_media_buy(self, req, ctx, configs=None):
            received_configs.append(configs or {})
            from adcp.types import CreateMediaBuySuccessResponse

            return CreateMediaBuySuccessResponse(media_buy_id="mb_002", packages=[])

    handler = _make_handler(_Platform(), executor, config_store=_Store())
    req = _make_request(["pid1", "pid2", "pid3"])
    await handler.create_media_buy(req, ToolContext())

    assert received_configs[0] == {"pid1": {"kind": "display"}, "pid2": {"kind": "video"}}


@pytest.mark.asyncio
async def test_store_raises_translates_to_service_unavailable(executor) -> None:
    """Store raises a generic exception; framework surfaces SERVICE_UNAVAILABLE."""

    class _Store:
        async def lookup_implementation_configs(self, product_ids, ctx):
            raise ConnectionError("db gone")

    class _Platform(DecisioningPlatform):
        capabilities = DecisioningCapabilities()
        accounts = SingletonAccounts(account_id="seller")

        async def create_media_buy(self, req, ctx, configs=None):
            from adcp.types import CreateMediaBuySuccessResponse

            return CreateMediaBuySuccessResponse(media_buy_id="mb_003", packages=[])

    handler = _make_handler(_Platform(), executor, config_store=_Store())
    req = _make_request(["pid1"])
    with pytest.raises(AdcpError) as exc_info:
        await handler.create_media_buy(req, ToolContext())

    err = exc_info.value
    assert err.code == "SERVICE_UNAVAILABLE"
    assert err.recovery == "transient"


@pytest.mark.asyncio
async def test_store_adcp_error_propagates_verbatim(executor) -> None:
    """An AdcpError raised by the store is not wrapped — it propagates as-is."""

    class _Store:
        async def lookup_implementation_configs(self, product_ids, ctx):
            raise AdcpError("RATE_LIMITED", message="slow down", recovery="transient")

    class _Platform(DecisioningPlatform):
        capabilities = DecisioningCapabilities()
        accounts = SingletonAccounts(account_id="seller")

        async def create_media_buy(self, req, ctx, configs=None):
            from adcp.types import CreateMediaBuySuccessResponse

            return CreateMediaBuySuccessResponse(media_buy_id="mb_004", packages=[])

    handler = _make_handler(_Platform(), executor, config_store=_Store())
    req = _make_request(["pid1"])
    with pytest.raises(AdcpError) as exc_info:
        await handler.create_media_buy(req, ToolContext())

    assert exc_info.value.code == "RATE_LIMITED"


@pytest.mark.asyncio
async def test_proposal_id_flow_no_lookup(executor) -> None:
    """packages=None (proposal_id path) → store not called, adopter gets configs={}."""
    store_calls: list[list] = []

    class _Store:
        async def lookup_implementation_configs(self, product_ids, ctx):
            store_calls.append(product_ids)
            return {}

    received_configs: list[dict] = []

    class _Platform(DecisioningPlatform):
        capabilities = DecisioningCapabilities()
        accounts = SingletonAccounts(account_id="seller")

        async def create_media_buy(self, req, ctx, configs=None):
            received_configs.append(configs or {})
            from adcp.types import CreateMediaBuySuccessResponse

            return CreateMediaBuySuccessResponse(media_buy_id="mb_005", packages=[])

    handler = _make_handler(_Platform(), executor, config_store=_Store())
    req = _make_request(None)  # proposal_id flow, packages=None
    await handler.create_media_buy(req, ToolContext())

    assert store_calls == []  # store never called
    assert received_configs == [{}]


@pytest.mark.asyncio
async def test_no_store_wired_adopter_gets_empty_configs(executor) -> None:
    """When config_store=None, adopter receives configs={} without any lookup."""
    received_configs: list[dict] = []

    class _Platform(DecisioningPlatform):
        capabilities = DecisioningCapabilities()
        accounts = SingletonAccounts(account_id="seller")

        async def create_media_buy(self, req, ctx, configs=None):
            received_configs.append(configs or {})
            from adcp.types import CreateMediaBuySuccessResponse

            return CreateMediaBuySuccessResponse(media_buy_id="mb_006", packages=[])

    import warnings

    # Warning fires at construction time (not at call time), so suppress there.
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", UserWarning)
        handler = _make_handler(_Platform(), executor, config_store=None)

    req = _make_request(["pid1", "pid2"])
    await handler.create_media_buy(req, ToolContext())

    assert received_configs == [{}]


@pytest.mark.asyncio
async def test_adopter_without_configs_kwarg_store_skips_injection(executor) -> None:
    """Adopter not declaring 'configs' still works; store is called but not injected."""
    store_calls: list[list] = []

    class _Store:
        async def lookup_implementation_configs(self, product_ids, ctx):
            store_calls.append(list(product_ids))
            return {"pid1": {"x": 1}}

    received_req_ids: list[str] = []

    class _Platform(DecisioningPlatform):
        capabilities = DecisioningCapabilities()
        accounts = SingletonAccounts(account_id="seller")

        async def create_media_buy(self, req, ctx):  # no configs kwarg
            received_req_ids.append(req.idempotency_key)
            from adcp.types import CreateMediaBuySuccessResponse

            return CreateMediaBuySuccessResponse(media_buy_id="mb_007", packages=[])

    handler = _make_handler(_Platform(), executor, config_store=_Store())
    req = _make_request(["pid1"])
    await handler.create_media_buy(req, ToolContext())

    # Store was called (lookup happened)
    assert len(store_calls) == 1
    # Method still received the request correctly (no injection)
    assert received_req_ids == ["test-key-12345678"]


def test_product_config_store_protocol_exported() -> None:
    """ProductConfigStore is importable from adcp.decisioning."""
    from adcp.decisioning import ProductConfigStore as PCS

    assert PCS is _ProductConfigStoreProto


def test_no_store_with_configs_kwarg_emits_warning(executor) -> None:
    """UserWarning fires when method accepts 'configs' but no store is wired."""
    import warnings

    class _Platform(DecisioningPlatform):
        capabilities = DecisioningCapabilities()
        accounts = SingletonAccounts(account_id="seller")

        async def create_media_buy(self, req, ctx, configs=None):
            from adcp.types import CreateMediaBuySuccessResponse

            return CreateMediaBuySuccessResponse(media_buy_id="mb_008", packages=[])

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        _make_handler(_Platform(), executor, config_store=None)

    user_warnings = [w for w in caught if issubclass(w.category, UserWarning)]
    assert any("ProductConfigStore" in str(w.message) for w in user_warnings)
