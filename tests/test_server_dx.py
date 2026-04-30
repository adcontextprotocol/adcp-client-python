"""Tests for server DX improvements: serve(), test_controller, responses."""

from __future__ import annotations

from typing import Any

import pytest

from adcp.server.responses import (
    activate_signal_response,
    build_creative_response,
    capabilities_response,
    creative_formats_response,
    delivery_response,
    error_response,
    list_creatives_response,
    log_event_response,
    media_buy_error_response,
    media_buy_response,
    media_buys_response,
    preview_creative_response,
    products_response,
    signals_response,
    sync_accounts_response,
    sync_catalogs_response,
    sync_creatives_response,
    sync_governance_response,
    update_media_buy_response,
)
from adcp.server.test_controller import (
    TestControllerError,
    TestControllerStore,
    _handle_test_controller,
    _list_scenarios,
)

# ============================================================================
# Response builder tests — match actual AdCP spec schemas
# ============================================================================


class TestCapabilitiesResponse:
    def test_basic(self):
        result = capabilities_response(["media_buy"])
        assert result["supported_protocols"] == ["media_buy"]
        assert result["adcp"]["major_versions"] == [3]
        assert result["sandbox"] is True

    def test_multiple_protocols(self):
        result = capabilities_response(["media_buy", "compliance_testing"])
        assert result["supported_protocols"] == ["media_buy", "compliance_testing"]

    def test_custom_versions(self):
        result = capabilities_response(["media_buy"], major_versions=[2, 3])
        assert result["adcp"]["major_versions"] == [2, 3]

    def test_sandbox_false(self):
        result = capabilities_response(["media_buy"], sandbox=False)
        assert result["sandbox"] is False


class TestSyncAccountsResponse:
    def test_uses_accounts_key(self):
        """Spec field name is 'accounts', not 'results'."""
        accts = [{"account_id": "a1", "status": "active"}]
        result = sync_accounts_response(accts)
        assert "accounts" in result
        assert "results" not in result
        assert result["accounts"] == accts

    def test_includes_sandbox(self):
        result = sync_accounts_response([])
        assert result["sandbox"] is True


class TestProductsResponse:
    def test_basic(self):
        products = [{"product_id": "p1", "name": "Product 1"}]
        result = products_response(products)
        assert result["products"] == products
        assert result["item_count"] == 1
        assert result["sandbox"] is True

    def test_pydantic_models(self):
        class FakeModel:
            def model_dump(self, **kwargs):
                return {"product_id": "p1"}

        result = products_response([FakeModel()])
        assert result["products"][0]["product_id"] == "p1"


class TestMediaBuyResponse:
    def test_basic(self):
        result = media_buy_response("mb-123", [{"package_id": "pkg-1"}])
        assert result["media_buy_id"] == "mb-123"
        assert len(result["packages"]) == 1
        assert result["sandbox"] is True

    def test_with_buyer_ref_and_status(self):
        result = media_buy_response("mb-123", [], buyer_ref="b1", status="active")
        assert result["buyer_ref"] == "b1"
        assert result["status"] == "active"


class TestMediaBuyErrorResponse:
    def test_basic(self):
        result = media_buy_error_response([{"code": "INVALID_PRODUCT", "message": "bad"}])
        assert result["errors"][0]["code"] == "INVALID_PRODUCT"


class TestUpdateMediaBuyResponse:
    def test_basic(self):
        result = update_media_buy_response("mb-123", status="active", revision=2)
        assert result["media_buy_id"] == "mb-123"
        assert result["status"] == "active"
        assert result["revision"] == 2


class TestMediaBuysResponse:
    def test_basic(self):
        buys = [{"media_buy_id": "mb-1", "status": "active"}]
        result = media_buys_response(buys)
        assert result["media_buys"] == buys
        assert result["sandbox"] is True


class TestDeliveryResponse:
    def test_spec_shape(self):
        """delivery_response uses media_buy_deliveries and reporting_period per spec."""
        deliveries = [
            {
                "media_buy_id": "mb-1",
                "status": "active",
                "totals": {"impressions": 1000, "spend": 50.0},
                "by_package": [],
            }
        ]
        result = delivery_response(
            deliveries,
            reporting_period={"start": "2026-01-01T00:00:00Z", "end": "2026-01-02T00:00:00Z"},
        )
        assert result["media_buy_deliveries"] == deliveries
        assert result["reporting_period"]["start"] == "2026-01-01T00:00:00Z"
        assert result["currency"] == "USD"

    def test_default_reporting_period(self):
        result = delivery_response([])
        assert "start" in result["reporting_period"]
        assert "end" in result["reporting_period"]


class TestCreativeFormatsResponse:
    def test_basic(self):
        formats = [
            {"format_id": {"agent_url": "http://localhost", "id": "d300"}, "name": "Display"}
        ]
        result = creative_formats_response(formats)
        assert result["formats"] == formats
        assert result["sandbox"] is True


class TestSyncCreativesResponse:
    def test_uses_creatives_key(self):
        """Spec field name is 'creatives', not 'results'."""
        creatives = [{"creative_id": "c1", "action": "created"}]
        result = sync_creatives_response(creatives)
        assert "creatives" in result
        assert "results" not in result
        assert result["creatives"] == creatives


class TestListCreativesResponse:
    def test_basic(self):
        creatives = [{"creative_id": "c1", "name": "Test", "status": "accepted"}]
        result = list_creatives_response(creatives)
        # Helper injects timestamps when caller omits them; identity compare
        # no longer holds, but payload shape and original fields do.
        assert len(result["creatives"]) == 1
        assert result["creatives"][0]["creative_id"] == "c1"
        assert result["pagination"]["total"] == 1
        assert result["query_summary"]["total_results"] == 1

    def test_fills_missing_timestamps(self):
        """Caller omits created_date/updated_date — helper fills both with now()."""
        import re

        creatives = [{"creative_id": "c1", "name": "Test", "status": "accepted"}]
        result = list_creatives_response(creatives)
        item = result["creatives"][0]
        assert "created_date" in item
        assert "updated_date" in item
        # ISO 8601 with timezone offset.
        iso_re = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?(\+\d{2}:\d{2}|Z)$")
        assert iso_re.match(item["created_date"]), item["created_date"]
        assert iso_re.match(item["updated_date"]), item["updated_date"]
        # When both are defaulted in a single call, they share the same value.
        assert item["created_date"] == item["updated_date"]

    def test_preserves_caller_provided_timestamps(self):
        """Explicit timestamps are preserved verbatim."""
        created = "2024-01-15T10:00:00+00:00"
        updated = "2024-02-20T15:30:00+00:00"
        creatives = [
            {
                "creative_id": "c1",
                "name": "Test",
                "status": "accepted",
                "created_date": created,
                "updated_date": updated,
            }
        ]
        result = list_creatives_response(creatives)
        item = result["creatives"][0]
        assert item["created_date"] == created
        assert item["updated_date"] == updated


class TestPreviewCreativeResponse:
    def test_basic(self):
        previews = [{"preview_id": "p1", "input": {}, "renders": []}]
        result = preview_creative_response(previews)
        assert result["response_type"] == "single"
        assert result["previews"] == previews
        assert "expires_at" in result


class TestBuildCreativeResponse:
    def test_basic(self):
        manifest = {"format_id": {"agent_url": "http://localhost", "id": "d300"}, "name": "Test"}
        result = build_creative_response(manifest)
        assert result["creative_manifest"] == manifest
        assert result["sandbox"] is True


class TestSignalsResponse:
    def test_basic(self):
        signals = [{"signal_agent_segment_id": "seg-1", "name": "Test"}]
        result = signals_response(signals)
        assert result["signals"] == signals
        assert result["sandbox"] is True


class TestActivateSignalResponse:
    def test_basic(self):
        deps = [{"type": "platform", "platform": "dsp-1", "is_live": True}]
        result = activate_signal_response(deps)
        assert result["deployments"] == deps
        assert result["sandbox"] is True


class TestLogEventResponse:
    def test_basic(self):
        result = log_event_response(5, 4)
        assert result["events_received"] == 5
        assert result["events_processed"] == 4


class TestSyncCatalogsResponse:
    def test_basic(self):
        cats = [{"catalog_id": "c1", "action": "created", "item_count": 10}]
        result = sync_catalogs_response(cats)
        assert result["catalogs"] == cats


class TestErrorResponse:
    def test_basic(self):
        result = error_response("NOT_FOUND", "Item not found")
        assert result["code"] == "NOT_FOUND"


# ============================================================================
# Test controller — _list_scenarios fix
# ============================================================================


class MinimalStore(TestControllerStore):
    """Only implements force_account_status and force_media_buy_status."""

    def __init__(self):
        self.accounts: dict[str, str] = {}
        self.media_buys: dict[str, str] = {}

    async def force_account_status(self, account_id: str, status: str) -> dict[str, Any]:
        prev = self.accounts.get(account_id, "unknown")
        self.accounts[account_id] = status
        return {"previous_state": prev, "current_state": status}

    async def force_media_buy_status(
        self, media_buy_id: str, status: str, rejection_reason: str | None = None
    ) -> dict[str, Any]:
        prev = self.media_buys.get(media_buy_id, "unknown")
        self.media_buys[media_buy_id] = status
        return {"previous_state": prev, "current_state": status}


class TestListScenarios:
    def test_detects_only_overridden_methods(self):
        """Must NOT report inherited-but-not-overridden methods."""
        store = MinimalStore()
        scenarios = _list_scenarios(store)
        assert "force_account_status" in scenarios
        assert "force_media_buy_status" in scenarios
        # These are NOT overridden — must NOT be reported
        assert "force_creative_status" not in scenarios
        assert "force_session_status" not in scenarios
        assert "simulate_delivery" not in scenarios
        assert "simulate_budget_spend" not in scenarios

    def test_empty_store(self):
        """A bare TestControllerStore has no implemented scenarios."""
        store = TestControllerStore()
        assert _list_scenarios(store) == []


class TestTestControllerError:
    @pytest.mark.asyncio
    async def test_error_is_caught(self):
        class ErrorStore(TestControllerStore):
            async def force_account_status(self, account_id: str, status: str) -> dict[str, Any]:
                raise TestControllerError("NOT_FOUND", f"Account {account_id} not found")

        store = ErrorStore()
        result = await _handle_test_controller(
            store,
            {"scenario": "force_account_status", "params": {"account_id": "x", "status": "active"}},
        )
        assert result["success"] is False
        assert result["error"] == "NOT_FOUND"
        assert "Account x not found" in result["error_detail"]

    @pytest.mark.asyncio
    async def test_error_with_current_state(self):
        class ErrorStore(TestControllerStore):
            async def force_media_buy_status(
                self, media_buy_id: str, status: str, rejection_reason: str | None = None
            ) -> dict[str, Any]:
                raise TestControllerError(
                    "INVALID_TRANSITION", "Cannot transition", current_state="completed"
                )

        store = ErrorStore()
        result = await _handle_test_controller(
            store,
            {
                "scenario": "force_media_buy_status",
                "params": {"media_buy_id": "mb-1", "status": "active"},
            },
        )
        assert result["error"] == "INVALID_TRANSITION"
        assert result["current_state"] == "completed"


class TestHandleTestController:
    @pytest.mark.asyncio
    async def test_list_scenarios(self):
        store = MinimalStore()
        result = await _handle_test_controller(store, {"scenario": "list_scenarios"})
        assert result["success"] is True
        assert "force_account_status" in result["scenarios"]
        assert "simulate_delivery" not in result["scenarios"]

    @pytest.mark.asyncio
    async def test_force_account_status(self):
        store = MinimalStore()
        result = await _handle_test_controller(
            store,
            {
                "scenario": "force_account_status",
                "params": {"account_id": "acct-1", "status": "suspended"},
            },
        )
        assert result["success"] is True
        assert result["current_state"] == "suspended"

    @pytest.mark.asyncio
    async def test_unimplemented_scenario_returns_error(self):
        store = MinimalStore()
        result = await _handle_test_controller(
            store, {"scenario": "simulate_delivery", "params": {"media_buy_id": "mb-1"}}
        )
        assert result["success"] is False
        assert result["error"] == "UNKNOWN_SCENARIO"

    @pytest.mark.asyncio
    async def test_unknown_scenario(self):
        store = MinimalStore()
        result = await _handle_test_controller(store, {"scenario": "nonexistent"})
        assert result["success"] is False
        assert result["error"] == "UNKNOWN_SCENARIO"

    @pytest.mark.asyncio
    async def test_missing_params(self):
        store = MinimalStore()
        result = await _handle_test_controller(
            store, {"scenario": "force_account_status", "params": {}}
        )
        assert result["success"] is False
        assert result["error"] == "INVALID_PARAMS"

    @pytest.mark.asyncio
    async def test_seed_creative_format_dispatches(self):
        """seed_creative_format is routed to the store method when implemented."""

        class _FormatStore(TestControllerStore):
            async def seed_creative_format(
                self,
                fixture: Any = None,
                format_id: str | None = None,
                *,
                context: Any = None,
            ) -> dict[str, Any]:
                return {"format_id": format_id or "fmt-default"}

        store = _FormatStore()
        result = await _handle_test_controller(
            store,
            {"scenario": "seed_creative_format", "params": {"format_id": "video_30s"}},
        )
        assert result["success"] is True
        assert result["format_id"] == "video_30s"

    @pytest.mark.asyncio
    async def test_seed_creative_format_in_list_scenarios(self):
        """seed_creative_format appears in list_scenarios when overridden."""

        class _FormatStore(TestControllerStore):
            async def seed_creative_format(
                self,
                fixture: Any = None,
                format_id: str | None = None,
            ) -> dict[str, Any]:
                return {"format_id": format_id or "fmt-x"}

        store = _FormatStore()
        result = await _handle_test_controller(store, {"scenario": "list_scenarios"})
        assert result["success"] is True
        assert "seed_creative_format" in result["scenarios"]
        assert "force_account_status" not in result["scenarios"]


# ============================================================================
# serve() and create_mcp_server tests
# ============================================================================


class TestCreateMcpServer:
    def test_creates_server_with_tools(self):
        from adcp.server import ADCPHandler
        from adcp.server.serve import create_mcp_server

        class TestHandler(ADCPHandler):
            async def get_adcp_capabilities(self, params, context=None):
                return {"adcp": {"major_versions": [3]}}

        mcp = create_mcp_server(TestHandler(), name="test-agent")
        tool_names = [t.name for t in mcp._tool_manager.list_tools()]
        assert "get_adcp_capabilities" in tool_names


class TestRegisterTestController:
    def test_registers_tool(self):
        from mcp.server.fastmcp import FastMCP

        from adcp.server.test_controller import register_test_controller

        mcp = FastMCP("test")
        store = MinimalStore()
        register_test_controller(mcp, store)

        tool_names = [t.name for t in mcp._tool_manager.list_tools()]
        assert "comply_test_controller" in tool_names


class TestServeWithTestController:
    def test_serve_accepts_test_controller(self):
        """Verify serve() signature accepts test_controller kwarg."""
        from adcp.server import ADCPHandler
        from adcp.server.serve import create_mcp_server

        class TestHandler(ADCPHandler):
            async def get_adcp_capabilities(self, params, context=None):
                return {}

        # We can't call serve() (it blocks), but we can verify the integration path
        mcp = create_mcp_server(TestHandler(), name="test")
        store = MinimalStore()
        from adcp.server.test_controller import register_test_controller

        register_test_controller(mcp, store)

        tool_names = [t.name for t in mcp._tool_manager.list_tools()]
        assert "get_adcp_capabilities" in tool_names
        assert "comply_test_controller" in tool_names


class TestSyncGovernanceResponse:
    def test_basic(self):
        accts = [{"account": {"brand": {"domain": "test.com"}}, "status": "synced"}]
        result = sync_governance_response(accts)
        assert result["accounts"] == accts
        assert result["sandbox"] is True


class TestPydanticSchemas:
    def test_schemas_are_generated(self):
        from adcp.server.mcp_tools import _PYDANTIC_SCHEMAS

        assert len(_PYDANTIC_SCHEMAS) > 0

    def test_no_dangling_refs(self):
        """Schemas with $ref must also have $defs to resolve them."""
        import json as json_mod

        from adcp.server.mcp_tools import _PYDANTIC_SCHEMAS

        for name, schema in _PYDANTIC_SCHEMAS.items():
            schema_str = json_mod.dumps(schema)
            if '"$ref"' in schema_str:
                assert "$defs" in schema, f"{name} has $ref but no $defs"

    def test_key_tools_have_pydantic_schemas(self):
        from adcp.server.mcp_tools import _PYDANTIC_SCHEMAS

        for tool in [
            "get_products",
            "create_media_buy",
            "sync_accounts",
            "get_signals",
            "activate_signal",
        ]:
            assert tool in _PYDANTIC_SCHEMAS, f"{tool} missing Pydantic schema"
