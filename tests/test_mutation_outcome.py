"""AdCP 3.3 ``error.mutation_outcome`` and unsupported-targeting-dimension details (#1642)."""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError as PydanticValidationError

from adcp.decisioning import (
    AdcpError,
    UnsupportedFeatureError,
    UnsupportedTargetingDimensionError,
    unsupported_targeting_dimension_details,
)
from adcp.exceptions import (
    ADCPTaskError,
    IdempotencyConflictError,
    extract_adcp_error_info,
    normalize_mutation_outcome,
)
from adcp.server import ADCPHandler, IdempotencyStore, MemoryBackend
from adcp.server.base import ToolContext
from adcp.server.mcp_tools import create_tool_caller, mark_not_applied
from adcp.server.translate import build_mcp_error_result, translate_error
from adcp.types import Error
from adcp.types.core import TaskResult, TaskStatus
from adcp.types.error_details import UnsupportedTargetingDimensionDetails


class TestAdcpErrorField:
    def test_to_wire_emits_mutation_outcome(self) -> None:
        error = AdcpError("INVALID_REQUEST", message="bad", mutation_outcome="not_applied")
        assert error.to_wire()["mutation_outcome"] == "not_applied"

    def test_absent_by_default(self) -> None:
        assert "mutation_outcome" not in AdcpError("INVALID_REQUEST").to_wire()

    def test_from_errors_takes_the_first_entry_claim(self) -> None:
        error = AdcpError.from_errors(
            [
                AdcpError("INVALID_REQUEST", message="a", mutation_outcome="not_applied"),
                AdcpError("INVALID_REQUEST", message="b"),
            ]
        )
        assert error.mutation_outcome == "not_applied"

    @pytest.mark.parametrize("outcome", ["not_applied", "unknown", "applied"])
    def test_mcp_projection(self, outcome: Any) -> None:
        structured = build_mcp_error_result(
            AdcpError("SERVICE_UNAVAILABLE", recovery="transient", mutation_outcome=outcome)
        ).structured_content
        assert structured["adcp_error"]["mutation_outcome"] == outcome
        assert structured["errors"][0]["mutation_outcome"] == outcome

    def test_task_error_projects_its_first_entry_claim(self) -> None:
        exc = ADCPTaskError(
            "create_media_buy",
            [Error(code="INVALID_REQUEST", message="bad", mutation_outcome="unknown")],
        )
        structured = build_mcp_error_result(exc).structured_content
        assert structured["adcp_error"]["mutation_outcome"] == "unknown"

    def test_translate_error_a2a_data(self) -> None:
        a2a_error = translate_error(
            AdcpError("INVALID_REQUEST", mutation_outcome="not_applied"), "a2a"
        )
        data: Any = a2a_error.data  # type: ignore[attr-defined]
        assert data["mutation_outcome"] == "not_applied"

    def test_dispatch_keeps_a_task_error_claim(self) -> None:
        from adcp.decisioning.dispatch import _task_error_to_adcp_error

        exc = IdempotencyConflictError(
            operation="create_media_buy",
            errors=[
                {"code": "IDEMPOTENCY_CONFLICT", "message": "x", "mutation_outcome": "not_applied"}
            ],
        )
        assert _task_error_to_adcp_error(exc).mutation_outcome == "not_applied"


class TestMarkNotApplied:
    def test_sets_absent_claims_only(self) -> None:
        error = AdcpError("INVALID_REQUEST")
        mark_not_applied(error)
        assert error.mutation_outcome == "not_applied"

        claimed = AdcpError("INVALID_REQUEST", mutation_outcome="unknown")
        mark_not_applied(claimed)
        assert claimed.mutation_outcome == "unknown"

    def test_task_error_entries(self) -> None:
        exc = ADCPTaskError(
            "create_media_buy",
            [Error(code="INVALID_REQUEST", message="a"), {"code": "X", "message": "b"}],
        )
        mark_not_applied(exc)
        assert [normalize_mutation_outcome(e) for e in exc.errors] == ["not_applied"] * 2

    def test_other_exceptions_are_ignored(self) -> None:
        mark_not_applied(ValueError("crash"))


class _Seller(ADCPHandler):
    def __init__(self, exc: BaseException | None = None) -> None:
        self.exc = exc

    async def create_media_buy(self, params: Any, context: Any = None) -> Any:
        if self.exc is not None:
            raise self.exc
        return {"media_buy_id": "mb-1", "packages": []}

    async def get_products(self, params: Any, context: Any = None) -> Any:
        if self.exc is not None:
            raise self.exc
        return {"products": []}


def _reject(method_name: str, params: dict[str, Any]) -> dict[str, Any]:
    raise AdcpError("INVALID_REQUEST", message="rejected by hook", recovery="correctable")


class TestFrameworkNotApplied:
    @pytest.mark.asyncio
    async def test_pre_dispatch_rejection_on_a_mutating_tool(self) -> None:
        caller = create_tool_caller(_Seller(), "create_media_buy", pre_validation_hook=_reject)
        with pytest.raises(AdcpError) as caught:
            await caller({})
        wire = build_mcp_error_result(caught.value).structured_content
        assert wire["adcp_error"]["mutation_outcome"] == "not_applied"

    @pytest.mark.asyncio
    async def test_schema_rejection_on_a_mutating_tool(self) -> None:
        from adcp.validation.client_hooks import ValidationHookConfig

        caller = create_tool_caller(
            _Seller(),
            "create_media_buy",
            validation=ValidationHookConfig(requests="strict", responses="off"),
        )
        with pytest.raises(ADCPTaskError) as caught:
            await caller({"packages": "not-a-list"})
        assert caught.value.mutation_outcome == "not_applied"

    @pytest.mark.asyncio
    async def test_a_read_carries_no_claim(self) -> None:
        caller = create_tool_caller(_Seller(), "get_products", pre_validation_hook=_reject)
        with pytest.raises(AdcpError) as caught:
            await caller({})
        assert caught.value.mutation_outcome is None

    @pytest.mark.asyncio
    async def test_a_handler_raise_carries_no_claim(self) -> None:
        caller = create_tool_caller(
            _Seller(AdcpError("INVALID_REQUEST", message="handler")), "create_media_buy"
        )
        with pytest.raises(AdcpError) as caught:
            await caller({})
        assert caught.value.mutation_outcome is None

    @pytest.mark.asyncio
    async def test_a_handler_claim_is_kept(self) -> None:
        caller = create_tool_caller(
            _Seller(AdcpError("PRODUCT_UNAVAILABLE", mutation_outcome="unknown")),
            "create_media_buy",
        )
        with pytest.raises(AdcpError) as caught:
            await caller({})
        assert caught.value.mutation_outcome == "unknown"


@pytest.mark.asyncio
async def test_idempotency_conflict_is_not_applied() -> None:
    store = IdempotencyStore(backend=MemoryBackend(), ttl_seconds=86400)

    class _Wrapped(ADCPHandler):
        @store.wrap
        async def create_media_buy(self, params: Any, context: Any = None) -> Any:
            return {"media_buy_id": "mb-1", "packages": []}

    seller = _Wrapped()
    ctx = ToolContext(caller_identity="buyer-1")
    key = "k-0123456789abcdef"
    await seller.create_media_buy({"idempotency_key": key, "total_budget": 1}, ctx)
    with pytest.raises(IdempotencyConflictError) as caught:
        await seller.create_media_buy({"idempotency_key": key, "total_budget": 2}, ctx)
    assert caught.value.mutation_outcome == "not_applied"
    wire = build_mcp_error_result(caught.value).structured_content
    assert wire["adcp_error"]["mutation_outcome"] == "not_applied"


class TestBuyerSide:
    @pytest.mark.parametrize(
        ("error", "expected"),
        [
            ({"code": "X", "message": "m"}, None),
            ({"code": "X", "message": "m", "mutation_outcome": "not_applied"}, "not_applied"),
            ({"code": "X", "message": "m", "mutation_outcome": "applied"}, "applied"),
            ({"code": "X", "message": "m", "mutation_outcome": "rolled_back"}, "unknown"),
            ({"code": "X", "message": "m", "mutation_outcome": ""}, "unknown"),
            ({"code": "X", "message": "m", "mutation_outcome": 3}, "unknown"),
            ({"code": "X", "message": "m", "mutation_outcome": None}, "unknown"),
        ],
    )
    def test_normalize(self, error: dict[str, Any], expected: str | None) -> None:
        assert normalize_mutation_outcome(error) == expected
        assert extract_adcp_error_info(error).mutation_outcome == expected

    def test_model_input(self) -> None:
        assert normalize_mutation_outcome(Error(code="X", message="m")) is None
        assert (
            normalize_mutation_outcome(Error(code="X", message="m", mutation_outcome="applied"))
            == "applied"
        )

    def test_task_error_reads_the_first_entry(self) -> None:
        exc = ADCPTaskError(
            "update_media_buy",
            [
                {"code": "A", "message": "a", "mutation_outcome": "unknown"},
                {"code": "B", "message": "b"},
            ],
        )
        assert exc.mutation_outcome == "unknown"
        assert ADCPTaskError("update_media_buy", []).mutation_outcome is None

    def test_task_result_reads_adcp_error_then_errors(self) -> None:
        failed = TaskResult[Any](
            status=TaskStatus.FAILED,
            success=False,
            adcp_error={"code": "X", "message": "m", "mutation_outcome": "not_applied"},
        )
        assert failed.mutation_outcome == "not_applied"
        from_payload = TaskResult[Any](
            status=TaskStatus.FAILED,
            success=False,
            data={"errors": [{"code": "X", "message": "m", "mutation_outcome": "bogus"}]},
        )
        assert from_payload.mutation_outcome == "unknown"
        assert TaskResult[Any](status=TaskStatus.COMPLETED, data={}).mutation_outcome is None


class TestUnsupportedTargetingDimension:
    def test_wire_shape(self) -> None:
        error = UnsupportedTargetingDimensionError(
            unsupported_dimension="geo_postal_areas",
            field="packages[1].targeting_overlay.geo_postal_areas",
            supported_dimensions=["geo_countries", "geo_regions"],
            product_id="prod-1",
            package_index=1,
        )
        assert isinstance(error, UnsupportedFeatureError)
        wire = error.to_wire()
        assert wire["code"] == "UNSUPPORTED_FEATURE"
        assert wire["recovery"] == "correctable"
        assert wire["field"] == "packages[1].targeting_overlay.geo_postal_areas"
        assert wire["details"] == {
            "unsupported_dimension": "geo_postal_areas",
            "supported_dimensions": ["geo_countries", "geo_regions"],
            "product_id": "prod-1",
            "package_index": 1,
        }
        UnsupportedTargetingDimensionDetails.model_validate(wire["details"])

    def test_deprecated_dimension_suggests_the_replacement(self) -> None:
        error = UnsupportedTargetingDimensionError(
            unsupported_dimension="geo_metros",
            field="packages[0].targeting_overlay.geo_metros",
            deprecated=True,
            replacement_dimension="geo_regions",
            mutation_outcome="not_applied",
        )
        wire = error.to_wire()
        assert wire["details"]["deprecated"] is True
        assert "no longer supported" in wire["message"]
        assert wire["suggestion"] == "Use 'geo_regions' instead."
        assert wire["mutation_outcome"] == "not_applied"

    def test_invalid_details_are_refused(self) -> None:
        with pytest.raises(PydanticValidationError):
            UnsupportedTargetingDimensionError(
                unsupported_dimension="geo_countries", field="x", package_index=-1
            )
        with pytest.raises(PydanticValidationError):
            UnsupportedTargetingDimensionError(unsupported_dimension="", field="x")

    def test_reader(self) -> None:
        error = UnsupportedTargetingDimensionError(
            unsupported_dimension="geo_countries", field="packages[0].targeting_overlay"
        )
        details = unsupported_targeting_dimension_details(error)
        assert details is not None and details.unsupported_dimension == "geo_countries"
        assert unsupported_targeting_dimension_details(error.to_wire()) is not None
        assert unsupported_targeting_dimension_details(UnsupportedFeatureError()) is None
        assert (
            unsupported_targeting_dimension_details(
                {"code": "INVALID_REQUEST", "message": "x", "details": details.model_dump()}
            )
            is None
        )
