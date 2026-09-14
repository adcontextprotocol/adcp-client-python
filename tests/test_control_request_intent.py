"""Operational controls must preserve exactly the buyer's requested mutation."""

from __future__ import annotations

from typing import Any

import anyio
import pytest
from mcp import ClientSession
from mcp.shared.memory import create_client_server_memory_streams
from pydantic import ValidationError

from adcp import ADCPClient
from adcp.server import ADCPHandler, create_mcp_server
from adcp.server.base import ToolContext
from adcp.types import ControlMediaBuyRequest
from adcp.validation import ValidationHookConfig, validate_request


def _request(**patch: Any) -> ControlMediaBuyRequest:
    return ControlMediaBuyRequest.model_validate(
        {
            "idempotency_key": "control-intent-0001",
            "account": {"account_id": "account-1"},
            "media_buy_id": "buy-1",
            "revision": 1,
            **patch,
        }
    )


def test_pause_does_not_add_media_buy_or_package_cancellation() -> None:
    request = _request(paused=True, packages=[{"package_id": "package-1", "paused": False}])
    payload = request.model_dump(mode="json")
    assert request.canceled is None
    assert "canceled" not in payload
    assert payload["packages"] == [{"package_id": "package-1", "paused": False}]
    assert validate_request("control_media_buy", payload).valid


@pytest.mark.parametrize("package", [False, True])
def test_cancellation_requires_explicit_true(package: bool) -> None:
    def construct(value: bool) -> Any:
        if package:
            return _request(packages=[{"package_id": "package-1", "canceled": value}]).packages[0]
        return _request(canceled=value)

    assert construct(True).model_dump()["canceled"] is True
    with pytest.raises(ValidationError):
        construct(False)


class _ControlSeller(ADCPHandler):
    advertised_tools = {"control_media_buy"}

    def __init__(self) -> None:
        self.received: list[ControlMediaBuyRequest] = []

    async def control_media_buy(
        self, params: ControlMediaBuyRequest, context: ToolContext | None = None
    ) -> dict[str, Any]:
        self.received.append(params)
        return {"status": "completed", "media_buy_id": params.media_buy_id, "revision": 2}


@pytest.mark.asyncio
async def test_control_intent_survives_real_client_and_typed_mcp_handler() -> None:
    seller = _ControlSeller()
    server = create_mcp_server(
        seller, validation=ValidationHookConfig(requests="strict", responses="strict")
    )
    patches = [
        {"paused": True, "packages": [{"package_id": "package-1", "paused": False}]},
        {
            "daily_budget_cap": None,
            "budget_cap_timezone": None,
            "packages": [{"package_id": "package-1", "daily_budget_cap": None}],
        },
        {
            "daily_budget_cap": 0,
            "budget_cap_timezone": "UTC",
            "packages": [{"package_id": "package-1", "daily_budget_cap": 0}],
        },
        {"canceled": True},
        {"packages": [{"package_id": "package-1", "canceled": True}]},
    ]
    async with create_client_server_memory_streams() as (client_streams, server_streams):
        async with anyio.create_task_group() as task_group:
            task_group.start_soon(
                server._lowlevel_server.run,
                *server_streams,
                server._lowlevel_server.create_initialization_options(),
                True,
            )
            async with ClientSession(*client_streams) as session:
                await session.initialize()
                client = ADCPClient.from_mcp_client(
                    session, validation=ValidationHookConfig(requests="strict", responses="strict")
                )
                for index, patch in enumerate(patches):
                    result = await client.control_media_buy(
                        _request(idempotency_key=f"control-intent-{index:04}", **patch)
                    )
                    assert result.success, result.error
                    assert len(seller.received) == index + 1
                    received = seller.received[-1]
                    # Compare intent after actual transport and typed server parsing.
                    # fields_set distinguishes a clear from a missing optional value.
                    actual = received.model_dump(
                        mode="json", exclude_unset=True, exclude_none=False
                    )
                    for key, value in patch.items():
                        assert actual[key] == value
                    assert received.canceled is patch.get("canceled")
                    if "daily_budget_cap" not in patch:
                        assert "daily_budget_cap" not in received.model_fields_set
                    if "budget_cap_timezone" not in patch:
                        assert "budget_cap_timezone" not in received.model_fields_set
                    for package in received.packages or []:
                        if "canceled" not in patch.get("packages", [{}])[0]:
                            assert package.canceled is None
            task_group.cancel_scope.cancel()
