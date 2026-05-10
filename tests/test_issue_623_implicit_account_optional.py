"""Regression tests for issue #623.

The typed dispatcher was rejecting valid requests on ``create_media_buy``
and ``sync_creatives`` when the buyer omitted ``account``. Per the AdCP
3.0.6+ spec, ``account`` is optional when the seller uses implicit or
derived resolution mode — account identity is resolved from the verified
auth chain instead.

Root cause: the generated Pydantic models for those two request types had
``account`` as a required field (no default). This file ensures they stay
optional and that the end-to-end wire path accepts requests without
``account`` when the platform uses ``SingletonAccounts`` (derived mode).
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

import pytest

from adcp.decisioning import (
    DecisioningCapabilities,
    DecisioningPlatform,
    InMemoryTaskRegistry,
    SingletonAccounts,
)
from adcp.decisioning.handler import PlatformHandler
from adcp.server.mcp_tools import create_tool_caller
from adcp.types import CreateMediaBuyRequest, SyncCreativesRequest

# ---------------------------------------------------------------------------
# Model-level: account is optional (Pydantic validation)
# ---------------------------------------------------------------------------


def test_create_media_buy_request_accepts_missing_account() -> None:
    """``CreateMediaBuyRequest`` must not raise when ``account`` is absent."""
    # Use model_construct to bypass other required-field checks — this test
    # is specifically about account optionality, not the full request shape.
    req = CreateMediaBuyRequest.model_construct(
        idempotency_key="test-idem-key-abcdef01234",
    )
    assert req.account is None


def test_sync_creatives_request_accepts_missing_account() -> None:
    """``SyncCreativesRequest`` must not raise when ``account`` is absent."""
    from adcp.types.generated_poc.core.creative_asset import CreativeAsset

    req = SyncCreativesRequest.model_construct(
        idempotency_key="test-idem-key-abcdef01234",
        creatives=[
            CreativeAsset.model_construct(
                creative_id="cr_1",
                name="test",
                format_id="banner_300x250",
            )
        ],
    )
    assert req.account is None


def test_create_media_buy_account_field_not_required() -> None:
    """``model_fields['account'].is_required()`` must be False after patching."""
    fi = CreateMediaBuyRequest.model_fields["account"]
    assert not fi.is_required(), (
        "CreateMediaBuyRequest.account must not be required — "
        "implicit/derived adopters omit it and resolve from auth chain"
    )


def test_sync_creatives_account_field_not_required() -> None:
    """``model_fields['account'].is_required()`` must be False after patching."""
    fi = SyncCreativesRequest.model_fields["account"]
    assert not fi.is_required(), (
        "SyncCreativesRequest.account must not be required — "
        "implicit/derived adopters omit it and resolve from auth chain"
    )


# ---------------------------------------------------------------------------
# Wire path: SingletonAccounts (derived mode) + no account on wire
# ---------------------------------------------------------------------------


class _DerivedModePlatform(DecisioningPlatform):
    """Minimal platform using ``SingletonAccounts`` (resolution='derived')."""

    capabilities = DecisioningCapabilities(specialisms=["sales-non-guaranteed"])
    accounts = SingletonAccounts(account_id="derived-acc")

    def __init__(self) -> None:
        super().__init__()
        self.calls: list[tuple[str, object]] = []

    def get_products(self, req, ctx):
        self.calls.append(("get_products", req))
        return {"products": []}

    def create_media_buy(self, req, ctx):
        self.calls.append(("create_media_buy", req))
        return {"media_buy_id": "mb_1", "status": "active"}

    def update_media_buy(self, media_buy_id, patch, ctx):
        self.calls.append(("update_media_buy", (media_buy_id, patch)))
        return {"media_buy_id": media_buy_id, "status": "active"}

    def sync_creatives(self, req, ctx):
        self.calls.append(("sync_creatives", req))
        return {"creatives": []}

    def get_media_buy_delivery(self, req, ctx):
        self.calls.append(("get_media_buy_delivery", req))
        return {"media_buy_deliveries": []}


@pytest.fixture
def executor():
    pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="test-623-")
    yield pool
    pool.shutdown(wait=True)


@pytest.mark.asyncio
async def test_create_media_buy_wire_dispatch_without_account(executor) -> None:
    """Wire dispatch for ``create_media_buy`` must succeed when ``account``
    is absent and the platform uses derived (SingletonAccounts) resolution."""
    platform = _DerivedModePlatform()
    handler = PlatformHandler(
        platform,
        executor=executor,
        registry=InMemoryTaskRegistry(),
    )
    caller = create_tool_caller(handler, "create_media_buy")
    wire_payload = {
        "idempotency_key": "test-idem-key-abcdef01234",
        "brand": {"domain": "example.com"},
        "start_time": "asap",
        "end_time": "2030-12-31T00:00:00+00:00",
        # No 'account' field — valid for implicit/derived adopters
    }
    result = await caller(wire_payload)
    assert platform.calls and platform.calls[0][0] == "create_media_buy"
    assert "media_buy_id" in result


@pytest.mark.asyncio
async def test_sync_creatives_wire_dispatch_without_account(executor) -> None:
    """Wire dispatch for ``sync_creatives`` must succeed when ``account``
    is absent and the platform uses derived (SingletonAccounts) resolution."""
    platform = _DerivedModePlatform()
    handler = PlatformHandler(
        platform,
        executor=executor,
        registry=InMemoryTaskRegistry(),
    )
    caller = create_tool_caller(handler, "sync_creatives")
    wire_payload = {
        "idempotency_key": "test-idem-key-abcdef01234",
        "creatives": [
            {
                "creative_id": "cr_1",
                "name": "banner ad",
                "format_id": {
                    "agent_url": "https://formats.example.com",
                    "id": "banner_300x250",
                },
                "assets": {},
            }
        ],
        # No 'account' field — valid for implicit/derived adopters
    }
    await caller(wire_payload)
    assert platform.calls and platform.calls[0][0] == "sync_creatives"
