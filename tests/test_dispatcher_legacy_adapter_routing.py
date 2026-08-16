"""Stage 4 tests: dispatcher routes legacy versions through the adapter.

Three end-to-end scenarios:

1. v2.5 buyer + registered adapter → request is translated, handler
   receives v3-shape params, response goes back unchanged (sync_creatives
   has no normalize_response).
2. v2.5 buyer + unregistered tool → ``INVALID_REQUEST`` *before* the
   handler runs (legacy version doesn't expose this tool).
3. v3 buyer → adapter path skipped; existing behaviour intact.
"""

from __future__ import annotations

from typing import Any

import pytest

from adcp.exceptions import ADCPTaskError
from adcp.server.base import ADCPHandler, ToolContext
from adcp.server.mcp_tools import create_tool_caller

_CANONICAL_URL = "https://creative.adcontextprotocol.org"


class _SyncCreativesHandler(ADCPHandler[Any]):
    def __init__(self) -> None:
        self.received: list[dict[str, Any]] = []

    async def sync_creatives(self, params: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
        self.received.append(params)
        return {"creatives": []}


class _GetProductsHandler(ADCPHandler[Any]):
    def __init__(self) -> None:
        self.received: list[dict[str, Any]] = []

    async def get_products(self, params: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
        self.received.append(params)
        return {"products": []}


@pytest.mark.asyncio
async def test_v2_5_sync_creatives_translates_format_id_before_handler() -> None:
    """A v2.5 buyer sending a bare ``format_id`` string sees the
    structured form in the handler — proof the adapter ran before
    dispatch."""
    handler = _SyncCreativesHandler()
    caller = create_tool_caller(handler, "sync_creatives")
    await caller(
        {
            "adcp_version": "2.5",
            "creatives": [
                {
                    "creative_id": "c1",
                    "name": "Banner",
                    "format_id": "display_300x250",  # bare string (v2.5)
                    "assets": {
                        "image": {
                            "asset_type": "image",
                            "url": "https://cdn.example.com/i.jpg",
                            "width": 300,
                            "height": 250,
                        }
                    },
                }
            ],
        }
    )
    assert len(handler.received) == 1
    received = handler.received[0]["creatives"][0]
    assert received["format_kind"] == "image"
    assert "format_id" not in received


@pytest.mark.asyncio
async def test_v2_5_sync_creatives_infers_asset_type_before_handler() -> None:
    """v2.5 asset without ``asset_type`` discriminator is inferred from key."""
    handler = _SyncCreativesHandler()
    caller = create_tool_caller(handler, "sync_creatives")
    await caller(
        {
            "adcp_version": "2.5",
            "creatives": [
                {
                    "creative_id": "c1",
                    "name": "Banner",
                    "format_id": {
                        "agent_url": _CANONICAL_URL,
                        "id": "display_300x250_image",
                    },
                    "assets": {"video": {"url": "https://cdn.example.com/v.mp4"}},
                }
            ],
        }
    )
    assert handler.received[0]["creatives"][0]["assets"]["video"]["asset_type"] == "video"


@pytest.mark.asyncio
async def test_v2_5_tool_without_adapter_raises_invalid_request() -> None:
    """A v2.5 buyer calling a tool outside the v2.5 catalog sees
    ``INVALID_REQUEST`` before the handler runs.

    ``check_governance`` was added in v3 — no v2.5 adapter exists.
    Pick that as a known-out-of-v2.5-scope tool.
    """

    class _CheckGovernanceHandler(ADCPHandler[Any]):
        def __init__(self) -> None:
            self.received: list[dict[str, Any]] = []

        async def check_governance(
            self, params: dict[str, Any], ctx: ToolContext
        ) -> dict[str, Any]:
            self.received.append(params)
            return {"approved": True}

    handler = _CheckGovernanceHandler()
    caller = create_tool_caller(handler, "check_governance")

    with pytest.raises(ADCPTaskError) as exc_info:
        await caller({"adcp_version": "2.5"})

    err = exc_info.value.errors[0]
    assert err.code == "INVALID_REQUEST"
    assert "check_governance" in err.message
    assert "2.5" in err.message
    assert err.details is not None
    assert err.details.get("legacy_version") == "2.5"
    assert handler.received == []


@pytest.mark.asyncio
async def test_v3_buyer_bypasses_adapter_path() -> None:
    """A malformed 3.0 legacy selector fails before the canonical handler."""
    handler = _SyncCreativesHandler()
    caller = create_tool_caller(handler, "sync_creatives")
    with pytest.raises(ADCPTaskError):
        await caller(
            {
                "adcp_version": "3.0",
                "creatives": [
                    {
                        "creative_id": "c1",
                        "name": "Banner",
                        "format_id": "should_not_be_wrapped",
                        "assets": {},
                    }
                ],
            }
        )
    assert handler.received == []


@pytest.mark.asyncio
async def test_v2_5_buyer_handler_dispatched_after_translation() -> None:
    """End-to-end: v2.5 wire shape comes in, handler runs, response goes
    out — confirms the full pipeline runs cleanly."""
    handler = _SyncCreativesHandler()
    caller = create_tool_caller(handler, "sync_creatives")
    result = await caller(
        {
            "adcp_version": "2.5",
            "creatives": [],
        }
    )
    assert result == {"creatives": []}
    assert len(handler.received) == 1


@pytest.mark.asyncio
async def test_legacy_adapter_raising_surfaces_as_invalid_request() -> None:
    """If a legacy adapter raises, the dispatcher converts to
    ``INVALID_REQUEST`` with the adapter context. Uses a registered
    rogue adapter rather than patching the shipped v2.5 sync_creatives
    (the dataclass is frozen, so patching its bound attribute is brittle).
    """
    from adcp.compat.legacy import (
        AdapterPair,
        _reset_registry_for_tests,
        register_adapter,
    )

    def boom(_payload: dict[str, Any]) -> dict[str, Any]:
        raise RuntimeError("translator blew up")

    _reset_registry_for_tests()
    try:
        register_adapter(
            "2.5",
            AdapterPair(tool_name="sync_creatives", adapt_request=boom),
        )

        handler = _SyncCreativesHandler()
        caller = create_tool_caller(handler, "sync_creatives")
        with pytest.raises(ADCPTaskError) as exc_info:
            await caller({"adcp_version": "2.5", "creatives": []})

        err = exc_info.value.errors[0]
        assert err.code == "INVALID_REQUEST"
        assert "translator blew up" in err.message
        assert handler.received == []
    finally:
        _reset_registry_for_tests()
