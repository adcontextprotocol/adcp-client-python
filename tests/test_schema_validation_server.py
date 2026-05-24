"""Server-middleware integration tests for schema-driven validation (issue #249).

Exercises the opt-in request/response validator on ``create_tool_caller``
against real handlers, using the MCP-facing dispatcher shape.
"""

from __future__ import annotations

from typing import Any

import pytest

from adcp.exceptions import ADCPTaskError
from adcp.server.base import ADCPHandler, ToolContext
from adcp.server.mcp_tools import create_tool_caller
from adcp.server.responses import media_buy_response, products_response
from adcp.types.generated_poc.enums.media_buy_status import MediaBuyStatus
from adcp.validation import ValidationHookConfig


class _StubHandler(ADCPHandler[Any]):
    """Minimal handler that records whether it was invoked and returns the
    payload supplied in the constructor."""

    def __init__(self, response: dict[str, Any]) -> None:
        self._response = response
        self.called = False

    async def get_products(self, params: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
        self.called = True
        return dict(self._response)


class _CreateMediaBuyHandler(ADCPHandler[Any]):
    async def create_media_buy(self, params: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
        return media_buy_response("mb_1", [], status="pending_creatives")


class _LegacyCreateMediaBuyStatusHandler(ADCPHandler[Any]):
    async def create_media_buy(self, params: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
        return {
            "media_buy_id": "mb_1",
            "packages": [],
            "status": "active",
            "revision": 1,
            "confirmed_at": "2026-05-23T10:00:00Z",
            "sandbox": True,
        }


class _InvalidLegacyCreateMediaBuyStatusHandler(ADCPHandler[Any]):
    async def create_media_buy(self, params: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
        return {
            "media_buy_id": "mb_1",
            "packages": [],
            "status": "draft",
            "revision": 1,
            "confirmed_at": "2026-05-23T10:00:00Z",
            "sandbox": True,
        }


class _TaskIdWithoutStatusHandler(ADCPHandler[Any]):
    async def create_media_buy(self, params: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
        return {
            "task_id": "task_1",
        }


class _EnumMediaBuyStatusHandler(ADCPHandler[Any]):
    async def create_media_buy(self, params: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
        return {
            "media_buy_id": "mb_1",
            "packages": [],
            "status": MediaBuyStatus.active,
            "revision": 1,
            "confirmed_at": "2026-05-23T10:00:00Z",
            "sandbox": True,
        }

    async def update_media_buy(self, params: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
        return {
            "media_buy_id": "mb_1",
            "media_buy_status": MediaBuyStatus.paused,
            "sandbox": True,
        }


VALID_GET_PRODUCTS = {
    "brief": "test campaign",
    "promoted_offering": "shoes",
    "buying_mode": "brief",
}


class TestRequestsStrict:
    @pytest.mark.asyncio
    async def test_rejects_malformed_with_validation_error_before_dispatch(
        self,
    ) -> None:
        handler = _StubHandler({"products": []})
        caller = create_tool_caller(
            handler,
            "get_products",
            validation=ValidationHookConfig(requests="strict"),
        )
        with pytest.raises(ADCPTaskError) as info:
            await caller({})
        assert handler.called is False
        first = info.value.errors[0]
        assert first.code == "VALIDATION_ERROR"
        assert first.field, "expected field pointer on error"
        assert first.details
        assert first.details["side"] == "request"

    @pytest.mark.asyncio
    async def test_accepts_valid_requests(self) -> None:
        handler = _StubHandler({"products": []})
        caller = create_tool_caller(
            handler,
            "get_products",
            validation=ValidationHookConfig(requests="strict"),
        )
        result = await caller(dict(VALID_GET_PRODUCTS))
        assert isinstance(result["products"], list)


class TestRequestsWarn:
    @pytest.mark.asyncio
    async def test_logs_warning_but_dispatches(self, caplog: pytest.LogCaptureFixture) -> None:
        handler = _StubHandler({"products": []})
        caller = create_tool_caller(
            handler,
            "get_products",
            validation=ValidationHookConfig(requests="warn"),
        )
        with caplog.at_level("WARNING", logger="adcp.server.mcp_tools"):
            await caller({})
        assert handler.called is True
        messages = [r.message for r in caplog.records]
        assert any(
            "Schema validation warning (request) for get_products" in m for m in messages
        ), f"no warning log: {messages}"


class TestNoValidationConfig:
    @pytest.mark.asyncio
    async def test_unconfigured_does_not_validate(self) -> None:
        handler = _StubHandler({"products": []})
        caller = create_tool_caller(handler, "get_products")
        await caller({})
        assert handler.called is True


class TestResponses:
    @pytest.mark.asyncio
    async def test_strict_drift_surfaces_validation_error(self) -> None:
        handler = _StubHandler({"products": "oops"})
        caller = create_tool_caller(
            handler,
            "get_products",
            validation=ValidationHookConfig(responses="strict"),
        )
        with pytest.raises(ADCPTaskError) as info:
            await caller(dict(VALID_GET_PRODUCTS))
        first = info.value.errors[0]
        assert first.code == "VALIDATION_ERROR"
        assert first.details["side"] == "response"

    @pytest.mark.asyncio
    async def test_warn_logs_but_returns_response_unchanged(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        handler = _StubHandler({"products": "oops"})
        caller = create_tool_caller(
            handler,
            "get_products",
            validation=ValidationHookConfig(responses="warn"),
        )
        with caplog.at_level("WARNING", logger="adcp.server.mcp_tools"):
            result = await caller(dict(VALID_GET_PRODUCTS))
        assert result["products"] == "oops"
        messages = [r.message for r in caplog.records]
        assert any(
            "Schema validation warning (response) for get_products" in m for m in messages
        ), f"no warning log: {messages}"

    @pytest.mark.asyncio
    async def test_valid_response_passes_strict(self) -> None:
        handler = _StubHandler(products_response([]))
        caller = create_tool_caller(
            handler,
            "get_products",
            validation=ValidationHookConfig(responses="strict"),
        )
        result = await caller(dict(VALID_GET_PRODUCTS))
        assert result["products"] == []
        assert result["cache_scope"] == "public"

    @pytest.mark.asyncio
    async def test_media_buy_status_keeps_task_envelope_status(self) -> None:
        handler = _CreateMediaBuyHandler()
        caller = create_tool_caller(
            handler,
            "create_media_buy",
            validation=ValidationHookConfig(responses="strict"),
        )
        result = await caller(
            {
                "brand": {"domain": "acme.example"},
                "packages": [
                    {
                        "product_id": "premium-homepage",
                        "budget": 1000,
                        "pricing_option_id": "po-cpm-homepage",
                    }
                ],
            }
        )
        assert result["media_buy_status"] == "pending_creatives"
        assert result["status"] == "completed"

    @pytest.mark.asyncio
    async def test_legacy_media_buy_status_normalizes_to_task_envelope(self) -> None:
        handler = _LegacyCreateMediaBuyStatusHandler()
        caller = create_tool_caller(
            handler,
            "create_media_buy",
            validation=ValidationHookConfig(responses="strict"),
        )
        result = await caller(
            {
                "brand": {"domain": "acme.example"},
                "packages": [
                    {
                        "product_id": "premium-homepage",
                        "budget": 1000,
                        "pricing_option_id": "po-cpm-homepage",
                    }
                ],
            }
        )
        assert result["media_buy_status"] == "active"
        assert result["status"] == "completed"

    @pytest.mark.asyncio
    async def test_enum_media_buy_status_normalizes_to_task_envelope(self) -> None:
        handler = _EnumMediaBuyStatusHandler()
        create = create_tool_caller(
            handler,
            "create_media_buy",
            validation=ValidationHookConfig(responses="strict"),
        )
        create_result = await create(
            {
                "brand": {"domain": "acme.example"},
                "packages": [
                    {
                        "product_id": "premium-homepage",
                        "budget": 1000,
                        "pricing_option_id": "po-cpm-homepage",
                    }
                ],
            }
        )
        assert create_result["media_buy_status"] == "active"
        assert create_result["status"] == "completed"

        update = create_tool_caller(
            handler,
            "update_media_buy",
            validation=ValidationHookConfig(responses="strict"),
        )
        update_result = await update({"media_buy_id": "mb_1"})
        assert update_result["media_buy_status"] == "paused"
        assert update_result["status"] == "completed"

    @pytest.mark.asyncio
    async def test_invalid_legacy_media_buy_status_is_not_rewritten(self) -> None:
        handler = _InvalidLegacyCreateMediaBuyStatusHandler()
        caller = create_tool_caller(
            handler,
            "create_media_buy",
            validation=ValidationHookConfig(responses="warn"),
        )
        result = await caller(
            {
                "brand": {"domain": "acme.example"},
                "packages": [
                    {
                        "product_id": "premium-homepage",
                        "budget": 1000,
                        "pricing_option_id": "po-cpm-homepage",
                    }
                ],
            }
        )
        assert result["status"] == "draft"
        assert "media_buy_status" not in result

    @pytest.mark.asyncio
    async def test_task_id_response_without_status_is_not_marked_completed(self) -> None:
        handler = _TaskIdWithoutStatusHandler()
        warn_caller = create_tool_caller(
            handler,
            "create_media_buy",
            validation=ValidationHookConfig(responses="warn"),
        )
        result = await warn_caller(
            {
                "brand": {"domain": "acme.example"},
                "packages": [
                    {
                        "product_id": "premium-homepage",
                        "budget": 1000,
                        "pricing_option_id": "po-cpm-homepage",
                    }
                ],
            }
        )
        assert result["task_id"] == "task_1"
        assert "status" not in result

        caller = create_tool_caller(
            handler,
            "create_media_buy",
            validation=ValidationHookConfig(responses="strict"),
        )
        with pytest.raises(ADCPTaskError) as info:
            await caller(
                {
                    "brand": {"domain": "acme.example"},
                    "packages": [
                        {
                            "product_id": "premium-homepage",
                            "budget": 1000,
                            "pricing_option_id": "po-cpm-homepage",
                        }
                    ],
                }
            )
        first = info.value.errors[0]
        assert first.code == "VALIDATION_ERROR"
        assert first.details["side"] == "response"

    @pytest.mark.asyncio
    async def test_account_scoped_wholesale_requires_explicit_cache_scope(self) -> None:
        handler = _StubHandler({"products": []})
        caller = create_tool_caller(
            handler,
            "get_products",
            validation=ValidationHookConfig(responses="strict"),
        )
        request = dict(VALID_GET_PRODUCTS)
        request["account"] = {"account_id": "acc_1"}
        with pytest.raises(ADCPTaskError) as info:
            await caller(request)
        first = info.value.errors[0]
        assert first.code == "VALIDATION_ERROR"
        assert first.details["side"] == "response"

    @pytest.mark.asyncio
    async def test_adcp_error_envelope_skips_response_validation(self) -> None:
        """Handler-returned ``adcp_error`` envelopes have their own shape
        enforced by the ``Error`` builder; validating them against the
        per-tool success schema would convert a real protocol error
        (e.g. ``NOT_FOUND``) into a fake ``VALIDATION_ERROR``."""
        handler = _StubHandler(
            {
                "adcp_error": {
                    "code": "NOT_FOUND",
                    "message": "no products match the brief",
                }
            }
        )
        caller = create_tool_caller(
            handler,
            "get_products",
            validation=ValidationHookConfig(responses="strict"),
        )
        result = await caller(dict(VALID_GET_PRODUCTS))
        # Envelope passes through unchanged — no VALIDATION_ERROR raised.
        assert result["adcp_error"]["code"] == "NOT_FOUND"
