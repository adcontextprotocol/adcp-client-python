"""Server unknown-field policy before tool argument coercion."""

from __future__ import annotations

from typing import Any

import pytest

from adcp.exceptions import ADCPTaskError
from adcp.server.a2a_server import ADCPAgentExecutor
from adcp.server.base import ADCPHandler, ToolContext
from adcp.server.mcp_tools import create_tool_caller
from adcp.validation import UnknownFieldPolicy, ValidationHookConfig


class _RecorderHandler(ADCPHandler[Any]):
    def __init__(self) -> None:
        self.received: list[dict[str, Any]] = []

    async def get_products(self, params: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
        self.received.append(dict(params))
        return {"products": []}

    async def create_media_buy(self, params: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
        self.received.append(dict(params))
        return {"media_buy_id": "mb_1", "packages": []}


@pytest.mark.asyncio
async def test_reject_unknown_top_level_field_before_dispatch() -> None:
    handler = _RecorderHandler()
    caller = create_tool_caller(
        handler,
        "get_products",
        validation=ValidationHookConfig(unknown_fields=UnknownFieldPolicy.REJECT),
    )

    with pytest.raises(ADCPTaskError) as exc_info:
        await caller({"buying_mode": "brief", "brief": "ads", "nonsense_field": "bar"})

    assert handler.received == []
    err = exc_info.value.errors[0]
    assert err.code == "INVALID_REQUEST"
    assert err.field == "nonsense_field"
    assert err.details is not None
    assert err.details["unknown_fields"] == ["nonsense_field"]


@pytest.mark.asyncio
async def test_strip_unknown_top_level_field_for_mutating_tool() -> None:
    handler = _RecorderHandler()
    caller = create_tool_caller(
        handler,
        "create_media_buy",
        validation=ValidationHookConfig(unknown_fields=UnknownFieldPolicy.STRIP),
    )

    await caller({"proposal_id": "prop_1", "nonsense_field": "bar"})

    assert handler.received == [{"proposal_id": "prop_1"}]


@pytest.mark.asyncio
async def test_ignore_preserves_current_permissive_behavior() -> None:
    handler = _RecorderHandler()
    caller = create_tool_caller(
        handler,
        "get_products",
        validation=ValidationHookConfig(unknown_fields=UnknownFieldPolicy.IGNORE),
    )

    await caller({"buying_mode": "brief", "brief": "ads", "nonsense_field": "bar"})

    assert handler.received[0]["nonsense_field"] == "bar"


@pytest.mark.asyncio
async def test_unknown_field_policy_runs_after_pre_validation_hooks() -> None:
    def normalize(_tool_name: str, args: dict[str, Any]) -> dict[str, Any]:
        legacy_brief = args.pop("legacy_brief")
        return {**args, "buying_mode": "brief", "brief": legacy_brief}

    handler = _RecorderHandler()
    caller = create_tool_caller(
        handler,
        "get_products",
        validation=ValidationHookConfig(unknown_fields="reject"),
        pre_validation_hook=normalize,
    )

    await caller({"legacy_brief": "ads"})

    assert handler.received == [{"buying_mode": "brief", "brief": "ads"}]


@pytest.mark.asyncio
async def test_a2a_executor_uses_same_unknown_field_policy() -> None:
    handler = _RecorderHandler()
    executor = ADCPAgentExecutor(
        handler,
        validation=ValidationHookConfig(unknown_fields=UnknownFieldPolicy.REJECT),
    )

    with pytest.raises(ADCPTaskError) as exc_info:
        await executor._tool_callers["create_media_buy"](
            {"proposal_id": "prop_1", "nonsense_field": "bar"}
        )

    assert handler.received == []
    assert exc_info.value.errors[0].field == "nonsense_field"
