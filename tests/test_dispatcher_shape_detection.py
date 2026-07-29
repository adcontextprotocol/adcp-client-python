"""Stage 6 tests: shape-based legacy detection.

Real v2.5 buyers can't send ``adcp_version`` — the field didn't exist
in the v2.5 schema. When the envelope is empty, the dispatcher falls
through to per-tool shape probes registered on the ``AdapterPair``.
A match promotes the request to the legacy adapter path; a miss
proceeds with SDK-pin validation.

These tests cover the four tools that have unambiguous v2.5 markers
(``sync_creatives``, ``get_products``, ``create_media_buy``,
``update_media_buy``) plus the bias-conservative case where a real v3
buyer's payload doesn't trigger downgrade.
"""

from __future__ import annotations

from typing import Any

import pytest

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


class _CreateMediaBuyHandler(ADCPHandler[Any]):
    def __init__(self) -> None:
        self.received: list[dict[str, Any]] = []

    async def create_media_buy(self, params: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
        self.received.append(params)
        return {"media_buy_id": "mb-1"}


class _UpdateMediaBuyHandler(ADCPHandler[Any]):
    def __init__(self) -> None:
        self.received: list[dict[str, Any]] = []

    async def update_media_buy(self, params: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
        self.received.append(params)
        return {"updated": True}


# ---------------------------------------------------------------------------
# sync_creatives — bare-string format_id is the v2.5 marker
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sync_creatives_bare_string_format_id_triggers_v2_5_adapter() -> None:
    """No ``adcp_version`` on the wire, but a creative has
    ``format_id`` as a bare string. Shape probe matches, adapter runs,
    handler sees the canonical creative declaration."""
    handler = _SyncCreativesHandler()
    caller = create_tool_caller(handler, "sync_creatives")

    await caller(
        {
            "creatives": [
                {
                    "creative_id": "c1",
                    "name": "Banner",
                    "format_id": "display_300x250",  # v2.5 bare string
                    "assets": {},
                }
            ],
        }
    )

    assert len(handler.received) == 1
    creative = handler.received[0]["creatives"][0]
    assert creative["format_kind"] == "image"
    assert creative["format_option_ref"] == {
        "scope": "product",
        "format_option_id": "migrated_63b8bee2b00d33f86c76587cd5474b83",
    }
    assert "format_id" not in creative


@pytest.mark.asyncio
async def test_sync_creatives_structured_format_id_does_not_trigger_v2_5() -> None:
    """v3 buyer (no envelope, structured format_id) → no shape match,
    it falls through to the default 3.0 compatibility projection. The probe
    itself must not be required for canonical handler input."""
    handler = _SyncCreativesHandler()
    caller = create_tool_caller(handler, "sync_creatives")

    structured = {"agent_url": _CANONICAL_URL, "id": "display_300x250"}
    await caller(
        {
            "creatives": [
                {
                    "creative_id": "c1",
                    "name": "Banner",
                    "format_id": structured,
                    "assets": {},
                }
            ],
        }
    )

    assert len(handler.received) == 1
    creative = handler.received[0]["creatives"][0]
    assert creative["format_kind"] == "image"
    assert creative["format_option_ref"] == {
        "scope": "product",
        "format_option_id": "migrated_63b8bee2b00d33f86c76587cd5474b83",
    }
    assert "format_id" not in creative


# ---------------------------------------------------------------------------
# get_products — brand_manifest OR promoted_offerings is the marker
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_products_brand_manifest_triggers_v2_5_adapter() -> None:
    handler = _GetProductsHandler()
    caller = create_tool_caller(handler, "get_products")

    await caller({"brand_manifest": "https://acme.example.com"})

    assert handler.received[0]["brand"] == {"domain": "acme.example.com"}
    assert "brand_manifest" not in handler.received[0]


@pytest.mark.asyncio
async def test_get_products_promoted_offerings_triggers_v2_5_adapter() -> None:
    handler = _GetProductsHandler()
    caller = create_tool_caller(handler, "get_products")

    await caller({"promoted_offerings": {"offerings": [{"name": "x"}]}})

    assert handler.received[0]["catalog"] == {
        "type": "offering",
        "items": [{"name": "x"}],
    }
    assert "promoted_offerings" not in handler.received[0]


@pytest.mark.asyncio
async def test_get_products_v3_payload_no_shape_match() -> None:
    """v3 buyer with ``brand: {domain}`` — no v2.5 marker. Adapter must
    not fire."""
    handler = _GetProductsHandler()
    caller = create_tool_caller(handler, "get_products")

    await caller({"brand": {"domain": "acme.example.com"}, "brief": "Q4"})

    # Handler sees the v3 payload unchanged.
    assert handler.received[0]["brand"] == {"domain": "acme.example.com"}


# ---------------------------------------------------------------------------
# create_media_buy / update_media_buy — creative_ids in package is the marker
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_media_buy_creative_ids_in_package_triggers_v2_5() -> None:
    handler = _CreateMediaBuyHandler()
    caller = create_tool_caller(handler, "create_media_buy")

    await caller({"packages": [{"pkg_id": "p1", "creative_ids": ["c1", "c2"]}]})

    pkg = handler.received[0]["packages"][0]
    assert pkg["creative_assignments"] == [
        {"creative_id": "c1"},
        {"creative_id": "c2"},
    ]
    assert "creative_ids" not in pkg


@pytest.mark.asyncio
async def test_update_media_buy_creative_ids_in_package_triggers_v2_5() -> None:
    handler = _UpdateMediaBuyHandler()
    caller = create_tool_caller(handler, "update_media_buy")

    await caller({"packages": [{"pkg_id": "p1", "creative_ids": ["c1"]}]})

    pkg = handler.received[0]["packages"][0]
    assert pkg["creative_assignments"] == [{"creative_id": "c1"}]
    assert "creative_ids" not in pkg


@pytest.mark.asyncio
async def test_create_media_buy_v3_assignments_no_shape_match() -> None:
    """v3 buyer with ``creative_assignments`` — no v2.5 marker."""
    handler = _CreateMediaBuyHandler()
    caller = create_tool_caller(handler, "create_media_buy")

    v3_assignments = [{"creative_id": "c1", "weight": 50}]
    await caller({"packages": [{"pkg_id": "p1", "creative_assignments": v3_assignments}]})

    pkg = handler.received[0]["packages"][0]
    assert pkg["creative_assignments"] == v3_assignments
    assert "creative_ids" not in pkg


# ---------------------------------------------------------------------------
# Explicit version wins over shape detection
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_explicit_adcp_version_skips_shape_probe() -> None:
    """If the buyer DID send ``adcp_version="3.0"`` but the payload
    coincidentally has a v2.5-looking field, the explicit version wins.
    Shape detection is the fallback, not an override.

    (Constructed scenario: this shouldn't happen with a well-behaved v3
    client, but the precedence needs to be tested.)
    """
    handler = _GetProductsHandler()
    caller = create_tool_caller(handler, "get_products")

    await caller(
        {
            "adcp_version": "3.0",
            "brand_manifest": "https://acme.example.com",  # v2.5 marker
            "brief": "Q4",
        }
    )

    # Adapter was NOT run — handler sees the raw payload with
    # brand_manifest still present.
    assert handler.received[0].get("brand_manifest") == "https://acme.example.com"
    assert "brand" not in handler.received[0]


# ---------------------------------------------------------------------------
# Tools without a probe (list_creative_formats, preview_creative)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_probe_means_no_shape_detection() -> None:
    """``list_creative_formats`` and ``preview_creative`` have
    pass-through requests (no v2.5 wire marker possible), so they
    declare no probe. A request without envelope reaches the handler
    without adapter routing.
    """

    class _ListFormatsHandler(ADCPHandler[Any]):
        def __init__(self) -> None:
            self.received: list[dict[str, Any]] = []

        async def list_creative_formats_legacy(
            self, params: dict[str, Any], ctx: ToolContext
        ) -> dict[str, Any]:
            self.received.append(params)
            return {"formats": []}

    handler = _ListFormatsHandler()
    caller = create_tool_caller(handler, "list_creative_formats")

    await caller({"filter": {"x": 1}})

    # No adapter ran (would have wrapped in something v3-shaped).
    # Handler sees the payload as the buyer sent it.
    assert handler.received[0] == {"filter": {"x": 1}}
