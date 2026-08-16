"""Tests for adcp.testing.SellerA2AClient."""

from __future__ import annotations

from typing import Any

import pytest

from adcp.decisioning import (
    DecisioningCapabilities,
    DecisioningPlatform,
    SingletonAccounts,
)
from adcp.decisioning.types import AdcpError
from adcp.testing import AdcpErrorPayload, SellerA2AClient, ToolInvokeResult


class _SuccessPlatform(DecisioningPlatform):
    capabilities = DecisioningCapabilities(
        specialisms=["sales-non-guaranteed"],
        supported_billing=["operator"],
    )
    accounts = SingletonAccounts(account_id="test")

    def get_products(self, req: Any, ctx: Any) -> dict[str, Any]:
        return {"products": [{"id": "p1", "name": "Banner"}]}

    def create_media_buy(self, req: Any, ctx: Any) -> dict[str, Any]:
        return {"media_buy_id": "mb_1", "status": "active"}

    def update_media_buy(self, mid: Any, p: Any, ctx: Any) -> dict[str, Any]:
        return {"media_buy_id": mid, "status": "active"}

    def sync_creatives(self, req: Any, ctx: Any) -> dict[str, Any]:
        return {"creatives": []}

    def get_media_buy_delivery(self, req: Any, ctx: Any) -> dict[str, Any]:
        return {"media_buy_deliveries": []}

    def get_media_buys(self, req: Any, ctx: Any) -> dict[str, Any]:
        return {"media_buys": []}

    def list_creative_formats_legacy(self, req: Any, ctx: Any) -> dict[str, Any]:
        return {"creative_formats": []}

    def list_creatives(self, req: Any, ctx: Any) -> dict[str, Any]:
        return {"creatives": []}

    def provide_performance_feedback(self, req: Any, ctx: Any) -> dict[str, Any]:
        return {"acknowledged": True}


class _ErrorPlatform(_SuccessPlatform):
    def get_products(self, req: Any, ctx: Any) -> dict[str, Any]:
        raise AdcpError(
            "PRODUCT_NOT_FOUND",
            message="no products available",
            recovery="terminal",
            field="brief",
        )


class _BurstExecutor:
    def __init__(self, *, non_terminal_events: int) -> None:
        self.non_terminal_events = non_terminal_events

    async def execute(self, request_ctx: Any, queue: Any) -> None:
        from a2a import types as pb

        from adcp.server.a2a_server import _make_task

        for _ in range(self.non_terminal_events):
            await queue.enqueue_event(
                _make_task(
                    request_ctx,
                    state=pb.TaskState.TASK_STATE_WORKING,
                    message="working",
                )
            )
        await queue.enqueue_event(
            _make_task(
                request_ctx,
                state=pb.TaskState.TASK_STATE_COMPLETED,
                data={"products": []},
                message="done",
            )
        )


_GET_PRODUCTS_PAYLOAD = {"buying_mode": "brief"}


# ---- happy path ---------------------------------------------------------


async def test_a2a_invoke_success_returns_ok_result() -> None:
    client = SellerA2AClient(_SuccessPlatform())
    result = await client.invoke("get_products", _GET_PRODUCTS_PAYLOAD)
    assert result.ok
    assert result.adcp_error is None


async def test_a2a_invoke_success_data_populated() -> None:
    client = SellerA2AClient(_SuccessPlatform())
    result = await client.invoke("get_products", _GET_PRODUCTS_PAYLOAD)
    assert result.data is not None
    assert "products" in result.data
    assert result.data["products"][0]["id"] == "p1"


# ---- structured-error path (AdcpError raised by handler) ----------------


async def test_a2a_invoke_structured_error_marks_not_ok() -> None:
    client = SellerA2AClient(_ErrorPlatform())
    result = await client.invoke("get_products", _GET_PRODUCTS_PAYLOAD)
    assert not result.ok


async def test_a2a_invoke_structured_error_code_extracted() -> None:
    client = SellerA2AClient(_ErrorPlatform())
    result = await client.invoke("get_products", _GET_PRODUCTS_PAYLOAD)
    assert result.adcp_error is not None
    assert result.adcp_error.code == "PRODUCT_NOT_FOUND"
    assert result.adcp_error.message == "no products available"
    assert result.adcp_error.recovery == "terminal"
    assert result.adcp_error.field == "brief"


async def test_a2a_invoke_structured_error_data_is_none() -> None:
    client = SellerA2AClient(_ErrorPlatform())
    result = await client.invoke("get_products", _GET_PRODUCTS_PAYLOAD)
    assert result.data is None


# ---- unstructured-error path (unknown skill) ----------------------------


async def test_a2a_invoke_unknown_skill_surfaces_failure() -> None:
    """An unknown skill produces a FAILED Task with no `adcp_error` DataPart.
    The harness synthesizes an AdcpErrorPayload so `result.ok` is uniform."""
    client = SellerA2AClient(_SuccessPlatform())
    result = await client.invoke("nonexistent_skill", {})
    assert not result.ok
    assert result.adcp_error is not None
    # Synthesized envelope code for unstructured A2A failures.
    assert result.adcp_error.code == "INTERNAL_ERROR"


# ---- shape + caching ----------------------------------------------------


async def test_a2a_invoke_none_payload_uses_empty_dict() -> None:
    client = SellerA2AClient(_SuccessPlatform())
    result = await client.invoke("get_products", None)
    assert isinstance(result, ToolInvokeResult)


async def test_a2a_invoke_structured_content_populated() -> None:
    client = SellerA2AClient(_SuccessPlatform())
    result = await client.invoke("get_products", _GET_PRODUCTS_PAYLOAD)
    assert isinstance(result.structured_content, dict)


# ---- event drain budget -------------------------------------------------


async def test_a2a_invoke_default_event_cap_exhausts_before_terminal_task() -> None:
    client = SellerA2AClient(_SuccessPlatform())
    client._executor = _BurstExecutor(non_terminal_events=33)

    with pytest.raises(RuntimeError) as exc_info:
        await client.invoke("get_products", _GET_PRODUCTS_PAYLOAD, timeout_seconds=0.01)

    message = str(exc_info.value)
    assert "produced no terminal Task" in message
    assert "within 0.01s x 32 events" in message


async def test_a2a_invoke_custom_max_events_reaches_terminal_task() -> None:
    client = SellerA2AClient(_SuccessPlatform())
    client._executor = _BurstExecutor(non_terminal_events=33)

    result = await client.invoke(
        "get_products",
        _GET_PRODUCTS_PAYLOAD,
        timeout_seconds=0.01,
        max_events=128,
    )

    assert result.ok
    assert result.data == {"products": []}


# ---- validation wiring round-trip --------------------------------------


async def test_a2a_invoke_with_server_default_validation_round_trip() -> None:
    """Proves the `validation=` parameter actually engages the validation
    hook chain — not just that the parameter is accepted by `__init__`.

    The stub `_SuccessPlatform.list_creative_formats` returns
    ``{"creative_formats": []}`` while the AdCP spec response key is
    ``formats``. With validation OFF (the default for tests), this
    mismatch passes through silently. With SERVER_DEFAULT_VALIDATION
    enabled, the response-side validator rejects the wire shape and
    the harness surfaces a structured ``VALIDATION_ERROR``. This
    asserts the hook actually fires end-to-end on the A2A path.
    """
    from adcp.validation.client_hooks import SERVER_DEFAULT_VALIDATION

    client = SellerA2AClient(
        _SuccessPlatform(),
        validation=SERVER_DEFAULT_VALIDATION,
    )
    result = await client.invoke("list_creative_formats", {})
    assert not result.ok
    assert result.adcp_error is not None
    assert result.adcp_error.code == "VALIDATION_ERROR"
    # The validator names the response side and the missing field, so
    # the round-trip surface is visible to adopters reading the result.
    assert result.adcp_error.details is not None
    assert result.adcp_error.details.get("side") == "response"


# ---- public import path -------------------------------------------------


def test_a2a_public_import_from_adcp_testing() -> None:
    import adcp.testing as pkg

    assert pkg.SellerA2AClient is SellerA2AClient
    assert pkg.AdcpErrorPayload is AdcpErrorPayload
    assert pkg.ToolInvokeResult is ToolInvokeResult
