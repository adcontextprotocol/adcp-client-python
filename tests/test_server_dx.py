"""Tests for server DX improvements: serve(), test_controller, responses."""

from __future__ import annotations

from typing import Any

import pytest

from adcp import get_adcp_spec_version
from adcp._version import get_supported_adcp_versions, normalize_to_release_precision
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
    TestControllerError as ControllerError,
)
from adcp.server.test_controller import (
    TestControllerStore,
    _handle_test_controller,
    _list_scenarios,
)

_PACKAGED_ADCP_VERSION = normalize_to_release_precision(get_adcp_spec_version())


@pytest.fixture(autouse=True)
def _admit_sandbox_gate(monkeypatch: pytest.MonkeyPatch) -> None:
    """These tests cover dispatcher / scenarios / response builders,
    not the sandbox-authority gate. Set the legacy env opt-in so the
    gate admits without requiring per-call resolver wiring. The gate's
    own behavior is exercised in ``test_account_mode_gate.py``."""
    monkeypatch.setenv("ADCP_SANDBOX", "1")


# ============================================================================
# Response builder tests — match actual AdCP spec schemas
# ============================================================================


class TestCapabilitiesResponse:
    def test_basic(self):
        result = capabilities_response(["media_buy"])
        assert result["supported_protocols"] == ["media_buy"]
        assert result["adcp"]["major_versions"] == [3]
        assert result["adcp"]["supported_versions"] == list(get_supported_adcp_versions())
        assert result["sandbox"] is True

    def test_multiple_protocols(self):
        result = capabilities_response(["media_buy", "compliance_testing"])
        assert result["supported_protocols"] == ["media_buy", "compliance_testing"]

    def test_custom_versions(self):
        result = capabilities_response(["media_buy"], major_versions=[2, 3])
        assert result["adcp"]["major_versions"] == [2, 3]
        assert "supported_versions" not in result["adcp"]

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
        assert result["status"] == "completed"
        assert result["sandbox"] is True
        assert "cache_scope" not in result

    def test_pydantic_models(self):
        class FakeModel:
            def model_dump(self, **kwargs):
                return {"product_id": "p1"}

        result = products_response([FakeModel()])
        assert result["products"][0]["product_id"] == "p1"

    def test_wholesale_metadata(self):
        result = products_response(
            [{"product_id": "p1"}],
            wholesale_feed_version="wf_1",
            pricing_version="pr_1",
            cache_scope="public",
            pagination={"next_cursor": "c2"},
            incomplete=[{"scope": "pricing"}],
        )
        assert result["wholesale_feed_version"] == "wf_1"
        assert result["pricing_version"] == "pr_1"
        assert result["cache_scope"] == "public"
        assert result["pagination"] == {"next_cursor": "c2"}
        assert result["incomplete"] == [{"scope": "pricing"}]

    def test_wholesale_unchanged_can_omit_products(self):
        result = products_response(
            products=None,
            wholesale_feed_version="wf_1",
            pricing_version="pr_1",
            cache_scope="public",
            unchanged=True,
        )
        assert "products" not in result
        assert result["unchanged"] is True


class TestMediaBuyResponse:
    def test_basic(self):
        result = media_buy_response("mb-123", [{"package_id": "pkg-1"}])
        assert result["media_buy_id"] == "mb-123"
        assert len(result["packages"]) == 1
        assert result["sandbox"] is True

    def test_with_buyer_ref_and_status(self):
        result = media_buy_response("mb-123", [], buyer_ref="b1", status="active")
        assert result["buyer_ref"] == "b1"
        assert "status" not in result
        assert result["media_buy_status"] == "active"

    def test_explicit_30_status_shape(self):
        result = media_buy_response("mb-123", [], status="pending_creatives", adcp_version="3.0")
        assert result["status"] == "pending_creatives"
        assert "media_buy_status" not in result

    def test_explicit_31_status_shape(self):
        result = media_buy_response(
            "mb-123",
            [],
            status="pending_creatives",
            adcp_version=_PACKAGED_ADCP_VERSION,
        )
        assert result["status"] == "completed"
        assert result["media_buy_status"] == "pending_creatives"

    def test_typed_success_normalizes_legacy_status(self):
        from adcp.types import CreateMediaBuySuccessResponse

        result = CreateMediaBuySuccessResponse(
            media_buy_id="mb-123",
            packages=[],
            status="active",
        )

        assert result.status == "completed"
        assert result.media_buy_status is not None
        assert result.media_buy_status.value == "active"

    def test_typed_success_rejects_async_task_status(self):
        from pydantic import ValidationError

        from adcp.types import CreateMediaBuySuccessResponse

        with pytest.raises(ValidationError):
            CreateMediaBuySuccessResponse(
                media_buy_id="mb-123",
                packages=[],
                status="working",
            )

    def test_typed_submitted_response_rejects_non_submitted_status(self):
        from pydantic import ValidationError

        from adcp.types import CreateMediaBuySubmittedResponse

        with pytest.raises(ValidationError):
            CreateMediaBuySubmittedResponse(task_id="task-123", status="working")

    def test_typed_success_does_not_infer_completed_lifecycle(self):
        from adcp.types import CreateMediaBuySuccessResponse, MediaBuyStatus

        result = CreateMediaBuySuccessResponse(
            media_buy_id="mb-123",
            packages=[],
            status="completed",
        )

        assert result.status == "completed"
        assert result.media_buy_status is None

        enum_result = CreateMediaBuySuccessResponse(
            media_buy_id="mb-123",
            packages=[],
            status=MediaBuyStatus.completed,
        )

        assert enum_result.status == "completed"
        assert enum_result.media_buy_status is None


class TestMediaBuyErrorResponse:
    def test_basic(self):
        result = media_buy_error_response([{"code": "INVALID_PRODUCT", "message": "bad"}])
        assert result["errors"][0]["code"] == "INVALID_PRODUCT"


class TestUpdateMediaBuyResponse:
    def test_basic(self):
        result = update_media_buy_response("mb-123", status="active", revision=2)
        assert result["media_buy_id"] == "mb-123"
        assert "status" not in result
        assert result["media_buy_status"] == "active"
        assert result["revision"] == 2

    def test_explicit_30_status_shape(self):
        result = update_media_buy_response("mb-123", status="paused", adcp_version="3.0")
        assert result["status"] == "paused"
        assert "media_buy_status" not in result

    def test_explicit_31_status_shape(self):
        result = update_media_buy_response(
            "mb-123",
            status="paused",
            adcp_version=_PACKAGED_ADCP_VERSION,
        )
        assert result["status"] == "completed"
        assert result["media_buy_status"] == "paused"

    def test_typed_success_normalizes_legacy_status(self):
        from adcp.types import UpdateMediaBuySuccessResponse

        result = UpdateMediaBuySuccessResponse(
            media_buy_id="mb-123",
            status="paused",
        )

        assert result.status == "completed"
        assert result.media_buy_status is not None
        assert result.media_buy_status.value == "paused"

    def test_typed_success_rejects_async_task_status(self):
        from pydantic import ValidationError

        from adcp.types import UpdateMediaBuySuccessResponse

        with pytest.raises(ValidationError):
            UpdateMediaBuySuccessResponse(
                media_buy_id="mb-123",
                status="working",
            )

    def test_typed_success_does_not_infer_completed_lifecycle(self):
        from adcp.types import MediaBuyStatus, UpdateMediaBuySuccessResponse

        result = UpdateMediaBuySuccessResponse(
            media_buy_id="mb-123",
            status="completed",
        )

        assert result.status == "completed"
        assert result.media_buy_status is None

        enum_result = UpdateMediaBuySuccessResponse(
            media_buy_id="mb-123",
            status=MediaBuyStatus.completed,
        )

        assert enum_result.status == "completed"
        assert enum_result.media_buy_status is None

    def test_typed_submitted_response(self):
        from adcp import UpdateMediaBuyResponse3, UpdateMediaBuySubmittedResponse

        result = UpdateMediaBuySubmittedResponse(task_id="task-123", status="submitted")

        assert isinstance(result, UpdateMediaBuyResponse3)
        assert result.status == "submitted"
        assert result.task_id == "task-123"
        assert result.adcp_version is None
        assert result.context_id is None
        assert result.replayed is False
        assert result.push_notification_config is None
        assert result.governance_context is None

    def test_typed_submitted_response_rejects_non_submitted_status(self):
        from pydantic import ValidationError

        from adcp.types import UpdateMediaBuySubmittedResponse

        with pytest.raises(ValidationError):
            UpdateMediaBuySubmittedResponse(task_id="task-123", status="working")


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

    def test_strips_none_from_sync_result_fields(self):
        """None-valued fields in sync creative dicts are stripped from wire output."""
        creatives = [{"creative_id": "c1", "action": "created", "status": None}]
        result = sync_creatives_response(creatives)
        assert result["creatives"][0] == {"creative_id": "c1", "action": "created"}


class TestStripNoneValues:
    """_strip_none_values removes None-valued keys from dicts recursively."""

    def test_flat_dict_strips_none(self):
        from adcp.server.responses import _strip_none_values

        result = _strip_none_values({"a": "hello", "b": None, "c": 42})
        assert result == {"a": "hello", "c": 42}

    def test_nested_dict_strips_none(self):
        from adcp.server.responses import _strip_none_values

        result = _strip_none_values({"outer": {"inner": None, "keep": "yes"}, "top_none": None})
        assert result == {"outer": {"keep": "yes"}}

    def test_list_items_stripped(self):
        from adcp.server.responses import _strip_none_values

        result = _strip_none_values([{"x": None, "y": 1}, {"x": 2, "y": None}])
        assert result == [{"y": 1}, {"x": 2}]

    def test_non_none_values_preserved(self):
        from adcp.server.responses import _strip_none_values

        result = _strip_none_values({"a": 0, "b": False, "c": "", "d": []})
        assert result == {"a": 0, "b": False, "c": "", "d": []}


class TestListCreativesResponse:
    def test_basic(self):
        creatives = [{"creative_id": "c1", "name": "Test", "status": "accepted"}]
        result = list_creatives_response(creatives)
        # Helper injects timestamps when caller omits them; identity compare
        # no longer holds, but payload shape and original fields do.
        assert len(result["creatives"]) == 1
        assert result["creatives"][0]["creative_id"] == "c1"
        assert result["pagination"]["total_count"] == 1
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

    def test_strips_none_from_asset_fields_in_dict_creatives(self):
        """None-valued asset fields must not appear as null on the wire.

        ImageAsset.format / alt_text / provenance are optional (non-required)
        in the JSON schema but non-nullable (type: string, not [string, null]).
        When an adopter builds a dict-based creative with None-valued asset
        fields, the response builder must strip them before wire serialisation
        so the storyboard schema validator does not reject the payload.
        """
        creative = {
            "creative_id": "c1",
            "created_date": "2024-01-01T00:00:00+00:00",
            "updated_date": "2024-01-01T00:00:00+00:00",
            "assets": {
                "banner": {
                    "asset_type": "image",
                    "url": "https://cdn.example.com/banner.png",
                    "width": 300,
                    "height": 250,
                    "format": None,
                    "alt_text": None,
                    "provenance": None,
                }
            },
        }
        result = list_creatives_response([creative])
        asset = result["creatives"][0]["assets"]["banner"]
        assert "format" not in asset, "format: null must be stripped from wire output"
        assert "alt_text" not in asset, "alt_text: null must be stripped from wire output"
        assert "provenance" not in asset, "provenance: null must be stripped from wire output"
        assert asset["asset_type"] == "image"
        assert asset["url"] == "https://cdn.example.com/banner.png"
        assert asset["width"] == 300
        assert asset["height"] == 250

    def test_strips_none_from_video_asset_fields(self):
        """VideoAsset optional fields (container_format, video_codec, etc.) are non-nullable."""
        creative = {
            "creative_id": "v1",
            "created_date": "2024-01-01T00:00:00+00:00",
            "updated_date": "2024-01-01T00:00:00+00:00",
            "assets": {
                "main_video": {
                    "asset_type": "video",
                    "url": "https://cdn.example.com/video.mp4",
                    "width": 1920,
                    "height": 1080,
                    "container_format": None,
                    "video_codec": None,
                    "duration_ms": None,
                    "provenance": None,
                }
            },
        }
        result = list_creatives_response([creative])
        asset = result["creatives"][0]["assets"]["main_video"]
        assert "container_format" not in asset
        assert "video_codec" not in asset
        assert "duration_ms" not in asset
        assert "provenance" not in asset
        assert asset["asset_type"] == "video"


class TestPreviewCreativeResponse:
    def test_basic(self):
        previews = [{"preview_id": "p1", "input": {}, "renders": []}]
        result = preview_creative_response(previews)
        assert result["response_type"] == "single"
        assert result["previews"] == previews
        assert "expires_at" in result

    def test_strips_none_from_asset_fields_in_preview(self):
        """None asset fields in preview input are stripped from wire output."""
        previews = [
            {
                "preview_id": "p1",
                "input": {
                    "assets": {
                        "hero": {
                            "asset_type": "image",
                            "url": "https://cdn.example.com/hero.png",
                            "width": 1200,
                            "height": 628,
                            "alt_text": None,
                            "format": None,
                        }
                    }
                },
                "renders": [],
            }
        ]
        result = preview_creative_response(previews)
        asset = result["previews"][0]["input"]["assets"]["hero"]
        assert "alt_text" not in asset
        assert "format" not in asset
        assert asset["asset_type"] == "image"


class TestBuildCreativeResponse:
    def test_basic(self):
        manifest = {"format_id": {"agent_url": "http://localhost", "id": "d300"}, "name": "Test"}
        result = build_creative_response(manifest)
        assert result["creative_manifest"] == manifest
        assert result["sandbox"] is True

    def test_strips_none_from_asset_fields_in_manifest(self):
        """None asset fields in build_creative manifest are stripped from wire output."""
        manifest = {
            "format_id": {"agent_url": "http://localhost", "id": "d300"},
            "name": "Test",
            "assets": {
                "banner": {
                    "asset_type": "image",
                    "url": "https://cdn.example.com/banner.png",
                    "width": 300,
                    "height": 250,
                    "format": None,
                    "alt_text": None,
                }
            },
        }
        result = build_creative_response(manifest)
        asset = result["creative_manifest"]["assets"]["banner"]
        assert "format" not in asset
        assert "alt_text" not in asset
        assert asset["url"] == "https://cdn.example.com/banner.png"

    def test_strips_none_from_multi_manifest(self):
        """None stripping works for multi-manifest (list) variant."""
        manifests = [
            {
                "name": "A",
                "assets": {
                    "img": {
                        "asset_type": "image",
                        "url": "u",
                        "width": 1,
                        "height": 1,
                        "format": None,
                    }
                },
            }
        ]
        result = build_creative_response(manifests)
        asset = result["creative_manifests"][0]["assets"]["img"]
        assert "format" not in asset


class TestSignalsResponse:
    def test_basic(self):
        signals = [{"signal_agent_segment_id": "seg-1", "name": "Test"}]
        result = signals_response(signals)
        assert result["signals"] == signals
        assert result["status"] == "completed"
        assert result["sandbox"] is True
        assert "cache_scope" not in result

    def test_wholesale_metadata(self):
        result = signals_response(
            [{"signal_agent_segment_id": "seg-1", "name": "Test"}],
            wholesale_feed_version="swf_1",
            pricing_version="spr_1",
            cache_scope="account",
            pagination={"next_cursor": "sig2"},
            incomplete=[{"scope": "signals"}],
        )
        assert result["wholesale_feed_version"] == "swf_1"
        assert result["pricing_version"] == "spr_1"
        assert result["cache_scope"] == "account"
        assert result["pagination"] == {"next_cursor": "sig2"}
        assert result["incomplete"] == [{"scope": "signals"}]

    def test_wholesale_unchanged_can_omit_signals(self):
        result = signals_response(
            signals=None,
            wholesale_feed_version="swf_1",
            pricing_version="spr_1",
            cache_scope="public",
            unchanged=True,
        )
        assert "signals" not in result
        assert result["unchanged"] is True


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
                raise ControllerError("NOT_FOUND", f"Account {account_id} not found")

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
                raise ControllerError(
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

    @pytest.mark.asyncio
    async def test_seed_creative_format_structured_fixture_id(self):
        """format_id extracted from a structured fixture object must not become a dict key."""
        received: list[Any] = []

        class _FormatStore(TestControllerStore):
            async def seed_creative_format(
                self,
                fixture: Any = None,
                format_id: str | None = None,
                *,
                context: Any = None,
            ) -> dict[str, Any]:
                received.append(format_id)
                return {"format_id": format_id or "fmt-structured"}

        store = _FormatStore()
        # Storyboard sends format_id only inside fixture (no top-level params.format_id).
        result = await _handle_test_controller(
            store,
            {
                "scenario": "seed_creative_format",
                "params": {
                    "fixture": {
                        "format_id": {"agent_url": "http://localhost", "id": "display_300x250"}
                    }
                },
            },
        )
        # The dispatcher passes format_id=None when it's absent from params.
        assert result["success"] is True
        assert received[0] is None


# ============================================================================
# serve() and create_mcp_server tests
# ============================================================================


class TestCreateMcpServer:
    def test_creates_server_with_tools(self):
        from adcp.server import ADCPHandler
        from adcp.server.serve import create_mcp_server

        class TestHandler(ADCPHandler):
            advertised_tools = {"get_adcp_capabilities"}

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
            advertised_tools = {"get_adcp_capabilities"}

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

    def test_media_buy_output_schema_allows_30_and_31_sync_shapes(self):
        from adcp.server.mcp_tools import (
            _PYDANTIC_OUTPUT_SCHEMAS,
            _ensure_pydantic_schemas_applied,
        )

        _ensure_pydantic_schemas_applied()
        media_buy_statuses = {
            "pending_creatives",
            "pending_start",
            "active",
            "paused",
            "completed",
            "rejected",
            "canceled",
        }
        for tool in ("create_media_buy", "update_media_buy"):
            success_schema = _PYDANTIC_OUTPUT_SCHEMAS[tool]["anyOf"][0]
            properties = success_schema["properties"]
            assert "status" not in success_schema["required"]
            status_variants = properties["status"]["anyOf"]
            assert status_variants[0]["const"] == "completed"
            advertised_statuses = {status_variants[0]["const"], *status_variants[1]["enum"]}
            assert advertised_statuses == media_buy_statuses
            assert set(properties["media_buy_status"]["anyOf"][0]["enum"]) == media_buy_statuses

            submitted_schema = _PYDANTIC_OUTPUT_SCHEMAS[tool]["anyOf"][2]
            submitted_properties = submitted_schema["properties"]
            assert submitted_properties["status"]["const"] == "submitted"
            assert "task_id" in submitted_schema["required"]
            for field in ("message", "errors", "context", "ext"):
                assert field in submitted_properties
