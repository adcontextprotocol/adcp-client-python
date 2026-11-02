"""AdCP 3.3 ``expected_confirmation_at``: buyer scheduling, seller helpers, downgrade rules.

The seller's estimate of when a submitted task confirms rides on the submitted
envelope of ``create_media_buy``, ``buy_products``, ``accept_proposal`` and
finalize-only ``refine_proposals``, and on ``get_media_buys`` buys. On the three
compact tasks it is sent only to callers on 3.3; ``create_media_buy`` and
``get_media_buys`` keep it for older callers, whose objects are open.
"""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

import adcp.client as client_module
from adcp import ADCPClient, expected_confirmation_at, next_status_check_delay
from adcp.compat.purchase_continuation import (
    CompatibilityOperationState,
    CompatibilityPurchaseOperation,
)
from adcp.compat.versions import CONVERSION_METADATA_KEY, convert_payload
from adcp.decisioning import (
    DecisioningCapabilities,
    DecisioningPlatform,
    InMemoryTaskRegistry,
    SingletonAccounts,
)
from adcp.decisioning.capabilities import LifecycleTool, MediaBuy
from adcp.decisioning.handler import PlatformHandler
from adcp.negotiation import WIRE_RESPONSE_METADATA_KEY, VerifiedRefinementResult
from adcp.server import ADCPHandler, ToolContext, submitted_response
from adcp.server.mcp_tools import create_tool_caller
from adcp.server.responses import format_expected_confirmation_at
from adcp.types import BuyProductsRequest, CreateMediaBuySubmittedResponse, RefineProposalsRequest
from adcp.types.core import AgentConfig, Protocol, TaskResult, TaskStatus
from adcp.validation import validate_response

PIN = "3.3-beta.3"
ESTIMATE = "2030-01-15T09:00:00Z"
SUBMITTED = {"status": "submitted", "task_id": "task_1", "expected_confirmation_at": ESTIMATE}
GATED = ["buy_products", "accept_proposal", "refine_proposals"]


# --- downgrade rules -------------------------------------------------------


@pytest.mark.parametrize("tool", GATED)
@pytest.mark.parametrize("direction", ["submitted", "sync"])
def test_gated_tasks_drop_the_estimate_below_3_3_with_a_loss(tool: str, direction: str) -> None:
    converted = convert_payload(
        tool, SUBMITTED, direction=direction, source_version=PIN, target_version="3.2"
    )
    assert "expected_confirmation_at" not in converted.payload
    assert [(loss.pointer, loss.reason) for loss in converted.losses] == [
        ("/expected_confirmation_at", "field_unavailable")
    ]
    assert validate_response(tool, converted.payload, version="3.2").valid


@pytest.mark.parametrize("tool", GATED)
def test_gated_tasks_keep_the_estimate_for_3_3(tool: str) -> None:
    converted = convert_payload(
        tool, SUBMITTED, direction="submitted", source_version=PIN, target_version=PIN
    )
    assert converted.payload["expected_confirmation_at"] == ESTIMATE


@pytest.mark.parametrize("target", ["3.2", "3.1", "3.0"])
@pytest.mark.parametrize("direction", ["submitted", "sync"])
def test_create_media_buy_keeps_the_estimate_for_every_release(target: str, direction: str) -> None:
    converted = convert_payload(
        "create_media_buy",
        SUBMITTED,
        direction=direction,
        source_version=PIN,
        target_version=target,
    )
    assert converted.payload["expected_confirmation_at"] == ESTIMATE
    assert converted.losses == ()
    assert validate_response("create_media_buy", converted.payload, version=target).valid


@pytest.mark.parametrize("target", ["3.2", "3.1"])
def test_get_media_buys_keeps_the_estimate_on_a_provisional_buy(target: str) -> None:
    payload = {
        "media_buys": [
            {
                "media_buy_id": "mb_1",
                "status": "pending_creatives",
                "confirmed_at": None,
                "expected_confirmation_at": ESTIMATE,
                "currency": "USD",
                "total_budget": 100,
                "packages": [],
            }
        ]
    }
    converted = convert_payload(
        "get_media_buys", payload, direction="sync", source_version=PIN, target_version=target
    )
    assert converted.payload["media_buys"][0]["expected_confirmation_at"] == ESTIMATE
    assert not any(loss.pointer.endswith("expected_confirmation_at") for loss in converted.losses)


# --- seller helpers --------------------------------------------------------


def test_submitted_response_normalizes_the_estimate_to_utc() -> None:
    aware = datetime(2030, 1, 15, 10, 0, tzinfo=timezone(timedelta(hours=1)))
    assert submitted_response("task_1", expected_confirmation_at=aware) == SUBMITTED
    assert submitted_response("task_1", expected_confirmation_at="2030-01-15T09:00:00+00:00") == (
        SUBMITTED
    )
    assert submitted_response("task_1") == {"status": "submitted", "task_id": "task_1"}


@pytest.mark.parametrize("value", [datetime(2030, 1, 15, 9, 0), "2030-01-15T09:00:00", "soon"])
def test_estimate_must_name_an_instant(value: Any) -> None:
    with pytest.raises(ValueError):
        format_expected_confirmation_at(value)


def test_a_past_estimate_is_allowed_and_logged(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING, logger="adcp.server"):
        assert format_expected_confirmation_at("2001-01-01T00:00:00Z") == "2001-01-01T00:00:00Z"
    assert "in the past" in caplog.text


class _Seller(ADCPHandler[Any]):
    async def buy_products(self, params, context=None):
        return submitted_response("task_1", expected_confirmation_at=ESTIMATE)

    async def create_media_buy(self, params, context=None):
        return submitted_response("task_2", expected_confirmation_at=ESTIMATE)


_BUY = {
    "idempotency_key": "expected-confirmation-1640",
    "account": {"account_id": "acct_1"},
    "feed_version": "feed_1",
    "purchases": [{"product_id": "prod_1", "pricing_option_id": "po_1", "budget": 1000}],
    "start_time": "2030-01-20T00:00:00Z",
    "end_time": "2030-02-01T00:00:00Z",
}


@pytest.mark.asyncio
async def test_server_drops_the_estimate_for_a_3_2_buy_products_caller() -> None:
    caller = create_tool_caller(_Seller(), "buy_products")
    context = ToolContext()
    older = await caller({**_BUY, "adcp_version": "3.2"}, context)
    assert "expected_confirmation_at" not in older
    losses = context.metadata[CONVERSION_METADATA_KEY]["losses"]
    assert any(loss["pointer"] == "/expected_confirmation_at" for loss in losses)

    current = await caller({**_BUY, "adcp_version": PIN})
    assert current["expected_confirmation_at"] == ESTIMATE


class _Platform(DecisioningPlatform):
    capabilities = DecisioningCapabilities(
        specialisms=["sales-non-guaranteed"],
        media_buy=MediaBuy(lifecycle_tools=[LifecycleTool.buy_products]),
    )
    accounts = SingletonAccounts(account_id="acct_1")

    def get_products(self, req, ctx):
        return {"products": []}

    def create_media_buy(self, req, ctx):
        return {"media_buy_id": "mb_1"}

    def update_media_buy(self, media_buy_id, patch, ctx):
        return {"media_buy_id": media_buy_id}

    def sync_creatives(self, req, ctx):
        return {"creatives": []}

    def get_media_buy_delivery(self, req, ctx):
        return {"media_buy_deliveries": []}

    def buy_products(self, req, ctx):
        return ctx.handoff_to_workflow(
            lambda task_ctx: None,
            expected_confirmation_at=datetime(2030, 1, 15, 9, 0, tzinfo=timezone.utc),
        )


@pytest.mark.asyncio
async def test_decisioning_handoff_carries_the_estimate_on_the_submitted_envelope() -> None:
    with ThreadPoolExecutor(max_workers=1) as executor:
        handler = PlatformHandler(_Platform(), executor=executor, registry=InMemoryTaskRegistry())
        response = await handler.buy_products(
            BuyProductsRequest.model_validate(_BUY), ToolContext()
        )
    assert response["status"] == "submitted"
    assert response["expected_confirmation_at"] == ESTIMATE
    assert validate_response("buy_products", dict(response), version=PIN).valid


def test_handoff_refuses_a_naive_estimate() -> None:
    from adcp.decisioning.context import RequestContext

    with pytest.raises(ValueError, match="timezone"):
        RequestContext().handoff_to_task(
            lambda task_ctx: None, expected_confirmation_at=datetime(2030, 1, 15)
        )


def test_estimate_on_a_task_that_does_not_define_it_is_omitted(
    caplog: pytest.LogCaptureFixture,
) -> None:
    from adcp.decisioning.dispatch import _submitted_projection

    with caplog.at_level(logging.WARNING):
        projection = _submitted_projection("task_1", "sync_creatives", ESTIMATE)
    assert projection == {"task_id": "task_1", "status": "submitted"}
    assert "not defined for sync_creatives" in caplog.text
    assert _submitted_projection("task_1", "refine_proposals", ESTIMATE) == {
        "task_id": "task_1",
        "status": "submitted",
        "expected_confirmation_at": ESTIMATE,
    }


# --- buyer scheduling ------------------------------------------------------


def test_task_result_surfaces_the_estimate_from_typed_and_wire_data() -> None:
    typed = CreateMediaBuySubmittedResponse.model_validate(SUBMITTED)
    result = TaskResult[Any](status=TaskStatus.SUBMITTED, data=typed)
    assert result.expected_confirmation_at == datetime(2030, 1, 15, 9, 0, tzinfo=timezone.utc)
    wire = TaskResult[Any](status=TaskStatus.SUBMITTED, data=dict(SUBMITTED))
    assert wire.expected_confirmation_at == result.expected_confirmation_at
    assert TaskResult[Any](status=TaskStatus.SUBMITTED, data={}).expected_confirmation_at is None
    assert expected_confirmation_at({"expected_confirmation_at": "not a time"}) is None


NOW = datetime(2030, 1, 15, 8, 0, tzinfo=timezone.utc)


@pytest.mark.parametrize(
    ("estimate", "expected"),
    [
        (None, 5.0),  # no estimate: normal cadence
        ("2030-01-15T07:00:00Z", 5.0),  # passed: normal cadence, never a failure
        ("2030-01-15T08:00:02Z", 5.0),  # sooner than the floor
        ("2030-01-15T08:01:00Z", 60.0),  # scheduled for the estimate
        ("2030-01-15T09:00:00Z", 300.0),  # capped at the ceiling
    ],
)
def test_next_check_is_bounded_by_floor_and_ceiling(estimate: str | None, expected: float) -> None:
    assert next_status_check_delay(estimate, poll_interval=5, max_interval=300, now=NOW) == expected


def test_delay_bounds_are_validated() -> None:
    with pytest.raises(ValueError):
        next_status_check_delay(None, poll_interval=10, max_interval=5)
    with pytest.raises(ValueError):
        next_status_check_delay(None, poll_interval=0, max_interval=5)


class _StopError(Exception):
    pass


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("offset", "expected"),
    [(timedelta(hours=2), 30.0), (timedelta(hours=-1), 0.5), (None, 0.5)],
)
async def test_refinement_waiter_schedules_checks_from_the_estimate(
    monkeypatch: pytest.MonkeyPatch, offset: timedelta | None, expected: float
) -> None:
    client = ADCPClient(
        AgentConfig(id="seller", agent_uri="https://seller.example", protocol=Protocol.A2A)
    )
    wire: dict[str, Any] = {"status": "submitted", "task_id": "task-1"}
    if offset is not None:
        wire["expected_confirmation_at"] = (datetime.now(timezone.utc) + offset).isoformat()
    initial = VerifiedRefinementResult(
        TaskResult(
            status=TaskStatus.SUBMITTED,
            data={"task_id": "task-1"},
            metadata={WIRE_RESPONSE_METADATA_KEY: wire},
        )
    )

    class _Working:
        status = "working"

    async def get_task_status(_request):
        return TaskResult(status=TaskStatus.WORKING, data=_Working())

    delays: list[float] = []

    async def sleep(delay: float) -> None:
        delays.append(delay)
        raise _StopError

    monkeypatch.setattr(client, "get_task_status", get_task_status)
    monkeypatch.setattr(client_module.asyncio, "sleep", sleep)
    with pytest.raises(_StopError):
        await client.wait_for_refinement_verified(
            RefineProposalsRequest.model_validate(
                {
                    "idempotency_key": "refine-expected-1640",
                    "account": {"account_id": "acct_1"},
                    "refinements": [{"proposal_id": "prop_1", "action": "finalize"}],
                }
            ),
            initial,
            poll_interval=0.5,
            max_poll_interval=30.0,
        )
    assert delays == [pytest.approx(expected)]


@pytest.mark.parametrize(
    ("state", "result", "expected"),
    [
        (CompatibilityOperationState.PENDING, dict(SUBMITTED), 300.0),
        (CompatibilityOperationState.PENDING, {"status": "submitted", "task_id": "t"}, 5.0),
        (CompatibilityOperationState.SUCCEEDED, dict(SUBMITTED), 5.0),
    ],
)
def test_purchase_continuation_schedules_refresh_from_the_estimate(
    state: CompatibilityOperationState, result: dict[str, Any], expected: float
) -> None:
    operation = CompatibilityPurchaseOperation(
        operation_id="op_1",
        principal_id="buyer",
        idempotency_key="key",
        token_hash="hash",
        payload_hash="hash",
        state=state,
        revision=1,
        execution_input={},
        result=result,
    )
    assert operation.next_refresh_delay(poll_interval=5, max_interval=300, now=NOW) == expected
