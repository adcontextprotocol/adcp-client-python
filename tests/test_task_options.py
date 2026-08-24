"""Per-call TaskOptions deadline and mutation-recovery contracts."""

from __future__ import annotations

import asyncio
import inspect
import time
import warnings
from typing import Any, get_type_hints
from unittest.mock import patch

import pytest
from pydantic import BaseModel, TypeAdapter

from adcp import ADCPClient, TaskOptions, TaskRecoveryMetadata
from adcp.exceptions import ADCPTimeoutError
from adcp.protocols.base import ProtocolAdapter
from adcp.protocols.mcp import MCPAdapter
from adcp.task_options import mark_task_dispatched
from adcp.types import GetProductsRequest
from adcp.types.core import ActivityType, AgentConfig, Protocol, TaskResult, TaskStatus


class _Request(BaseModel):
    idempotency_key: str = "0123456789abcdef-task-options"


class _NoKeyRequest(BaseModel):
    value: str = "test"


def _client(**kwargs: Any) -> ADCPClient:
    return ADCPClient(
        AgentConfig(
            id=kwargs.pop("agent_id", "seller"),
            agent_uri="https://seller.example/mcp",
            protocol=Protocol.MCP,
            timeout=17.0,
        ),
        **kwargs,
    )


def _get_products_request() -> GetProductsRequest:
    return TypeAdapter(GetProductsRequest).validate_python(
        {"buying_mode": "brief", "brief": "deadline test"}
    )


def _completed(data: dict[str, Any] | None = None) -> TaskResult[Any]:
    return TaskResult[Any](
        status=TaskStatus.COMPLETED,
        success=True,
        data=data or {"products": []},
    )


@pytest.mark.parametrize("timeout", [0, -1, float("inf"), float("nan"), True])
def test_task_options_requires_finite_positive_timeout(timeout: Any) -> None:
    with pytest.raises(ValueError, match="finite positive"):
        TaskOptions(timeout=timeout)


def test_task_options_public_exports_are_immutable() -> None:
    options = TaskOptions(timeout=1.0)
    with pytest.raises((AttributeError, TypeError)):
        options.timeout = 2.0  # type: ignore[misc]
    assert TaskRecoveryMetadata.__module__ == "adcp.task_options"
    assert get_type_hints(ADCPTimeoutError.__init__)["recovery"] == (TaskRecoveryMetadata | None)


@pytest.mark.asyncio
async def test_effectively_expired_deadline_does_not_leak_coroutine() -> None:
    client = _client()
    with warnings.catch_warnings():
        warnings.simplefilter("error", RuntimeWarning)
        with pytest.raises(ADCPTimeoutError):
            await client.get_products(
                _get_products_request(),
                options=TaskOptions(timeout=1e-15),
            )


@pytest.mark.asyncio
async def test_read_deadline_preserves_activity_operation_id() -> None:
    activities = []
    client = _client(on_activity=activities.append)

    async def delayed(_params: dict[str, Any]) -> TaskResult[Any]:
        await asyncio.sleep(1)
        return _completed()

    with patch.object(client.adapter, "get_products", new=delayed):
        with pytest.raises(ADCPTimeoutError) as caught:
            await client.get_products(_get_products_request(), options=TaskOptions(timeout=0.01))

    error = caught.value
    request_activity = next(a for a in activities if a.type == ActivityType.PROTOCOL_REQUEST)
    assert error.operation_id == request_activity.operation_id
    assert error.task_name == "get_products"
    assert error.recovery is None
    assert client.agent_config.timeout == 17.0


@pytest.mark.asyncio
async def test_inner_transport_timeout_is_not_relabelled() -> None:
    client = _client()
    inner = TimeoutError("transport read timed out")

    async def fail(_params: dict[str, Any]) -> TaskResult[Any]:
        raise inner

    with patch.object(client.adapter, "get_products", new=fail):
        with pytest.raises(TimeoutError) as caught:
            await client.get_products(_get_products_request(), options=TaskOptions(timeout=1.0))
    assert caught.value is inner


@pytest.mark.asyncio
async def test_external_cancellation_is_not_relabelled() -> None:
    client = _client()
    entered = asyncio.Event()

    async def delayed(_params: dict[str, Any]) -> TaskResult[Any]:
        entered.set()
        await asyncio.sleep(10)
        return _completed()

    with patch.object(client.adapter, "get_products", new=delayed):
        task = asyncio.create_task(
            client.get_products(_get_products_request(), options=TaskOptions(timeout=5.0))
        )
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


@pytest.mark.asyncio
async def test_mutation_timeout_carries_secret_safe_recovery() -> None:
    activities = []
    client = _client(on_activity=activities.append)
    key = "0123456789abcdef-recovery-key"

    async def dispatched(_params: dict[str, Any]) -> TaskResult[Any]:
        mark_task_dispatched(
            client.adapter.task_options_client_token,
            "build_creative",
            mutating=True,
            idempotency_key=key,
        )
        await asyncio.sleep(1)
        return _completed()

    with (
        warnings.catch_warnings(),
        patch.object(client.adapter, "build_creative", new=dispatched),
    ):
        warnings.simplefilter("ignore", DeprecationWarning)
        with pytest.raises(ADCPTimeoutError) as caught:
            await client.build_creative_legacy(
                _Request(idempotency_key=key),
                options=TaskOptions(timeout=0.01),  # type: ignore[arg-type]
            )

    error = caught.value
    assert error.recovery is not None
    assert error.recovery.idempotency_key == key
    assert error.recovery.task_name == "build_creative"
    request_activity = next(a for a in activities if a.type == ActivityType.PROTOCOL_REQUEST)
    assert error.recovery.operation_id == request_activity.operation_id == error.operation_id
    assert key not in str(error)
    assert key not in repr(error)
    assert key not in repr(error.recovery)


@pytest.mark.asyncio
async def test_real_mcp_dispatch_reports_the_exact_pinned_wire_key() -> None:
    class BlockingSession:
        sent: dict[str, Any] | None = None

        async def call_tool(self, _name: str, params: dict[str, Any]) -> Any:
            self.sent = params
            await asyncio.sleep(1)

    session = BlockingSession()
    client = ADCPClient.from_mcp_client(session, agent_id="in-process")  # type: ignore[arg-type]
    client.adapter.request_validation_mode = "off"
    key = "0123456789abcdef-pinned-wire-key"

    with warnings.catch_warnings(), client.use_idempotency_key(key):
        warnings.simplefilter("ignore", DeprecationWarning)
        with pytest.raises(ADCPTimeoutError) as caught:
            await client.build_creative_legacy(  # type: ignore[arg-type]
                _NoKeyRequest(), options=TaskOptions(timeout=0.05)
            )

    assert session.sent is not None
    assert session.sent["idempotency_key"] == key
    assert caught.value.recovery is not None
    assert caught.value.recovery.idempotency_key == key


@pytest.mark.asyncio
async def test_expired_synchronous_preflight_never_marks_dispatch() -> None:
    client = _client()
    dispatched = False

    async def blocked(_params: dict[str, Any]) -> TaskResult[Any]:
        nonlocal dispatched
        time.sleep(0.02)
        mark_task_dispatched(
            client.adapter.task_options_client_token,
            "build_creative",
            mutating=True,
            idempotency_key="0123456789abcdef-late",
        )
        dispatched = True
        return _completed()

    with (
        warnings.catch_warnings(),
        patch.object(client.adapter, "build_creative", new=blocked),
    ):
        warnings.simplefilter("ignore", DeprecationWarning)
        with pytest.raises(ADCPTimeoutError) as caught:
            await client.build_creative_legacy(  # type: ignore[arg-type]
                _Request(), options=TaskOptions(timeout=0.005)
            )

    assert dispatched is False
    assert caught.value.recovery is None


@pytest.mark.asyncio
async def test_synchronous_postflight_overrun_is_rejected() -> None:
    client = _client()
    raw = _completed()

    async def immediate(_params: dict[str, Any]) -> TaskResult[Any]:
        return raw

    def slow_postflight(_raw: TaskResult[Any]) -> TaskResult[Any]:
        time.sleep(0.02)
        return raw

    with (
        patch.object(client.adapter, "get_products", new=immediate),
        patch.object(client, "_canonicalize_get_products_result", new=slow_postflight),
    ):
        with pytest.raises(ADCPTimeoutError):
            await client.get_products(_get_products_request(), options=TaskOptions(timeout=0.005))


@pytest.mark.asyncio
async def test_strict_preflight_cancellation_resets_verification_guard() -> None:
    client = _client(strict_idempotency=True)

    async def delayed_capabilities() -> Any:
        await asyncio.sleep(1)

    with (
        warnings.catch_warnings(),
        patch.object(client, "fetch_capabilities", new=delayed_capabilities),
    ):
        warnings.simplefilter("ignore", DeprecationWarning)
        with pytest.raises(ADCPTimeoutError):
            await client.build_creative_legacy(  # type: ignore[arg-type]
                _Request(), options=TaskOptions(timeout=0.01)
            )
    assert client._idempotency_capability_verified is False


@pytest.mark.asyncio
async def test_mcp_connection_cancellation_does_not_try_fallback_urls() -> None:
    class CancelledStack:
        enter_calls = 0
        closed = False

        async def enter_async_context(self, _context: Any) -> Any:
            self.enter_calls += 1
            raise asyncio.CancelledError

        async def aclose(self) -> None:
            self.closed = True

    client = _client()
    assert isinstance(client.adapter, MCPAdapter)
    stack = CancelledStack()
    with (
        patch("adcp.protocols.mcp.AsyncExitStack", return_value=stack),
        patch("adcp.protocols.mcp.streamablehttp_client", return_value=object()),
        patch.object(client.adapter, "_streamable_http_client_factory", return_value=object()),
    ):
        with pytest.raises(asyncio.CancelledError):
            await client.adapter._get_session()

    assert stack.enter_calls == 1
    assert stack.closed is True


@pytest.mark.asyncio
async def test_nested_generic_call_uses_one_deadline_and_root_name() -> None:
    client = _client()

    async def delayed(_params: dict[str, Any]) -> TaskResult[Any]:
        await asyncio.sleep(1)
        return _completed()

    with (
        warnings.catch_warnings(),
        patch.object(client.adapter, "build_creative", new=delayed),
    ):
        warnings.simplefilter("ignore", DeprecationWarning)
        with pytest.raises(ADCPTimeoutError) as caught:
            await client.execute_task_legacy(
                "build_creative",
                _Request(),
                options=TaskOptions(timeout=0.01),
            )
    assert caught.value.task_name == "build_creative"


@pytest.mark.asyncio
async def test_concurrent_deadlines_keep_recovery_keys_isolated() -> None:
    client = _client()

    async def dispatched(params: dict[str, Any]) -> TaskResult[Any]:
        mark_task_dispatched(
            client.adapter.task_options_client_token,
            "build_creative",
            mutating=True,
            idempotency_key=params["idempotency_key"],
        )
        await asyncio.sleep(1)
        return _completed()

    keys = ("0123456789abcdef-concurrent-a", "0123456789abcdef-concurrent-b")
    with (
        warnings.catch_warnings(),
        patch.object(client.adapter, "build_creative", new=dispatched),
    ):
        warnings.simplefilter("ignore", DeprecationWarning)
        outcomes = await asyncio.gather(
            *(
                client.build_creative_legacy(  # type: ignore[arg-type]
                    _Request(idempotency_key=key),
                    options=TaskOptions(timeout=0.01),
                )
                for key in keys
            ),
            return_exceptions=True,
        )

    assert all(isinstance(outcome, ADCPTimeoutError) for outcome in outcomes)
    recovered = {
        outcome.recovery.idempotency_key
        for outcome in outcomes
        if isinstance(outcome, ADCPTimeoutError) and outcome.recovery is not None
    }
    assert recovered == set(keys)


@pytest.mark.asyncio
async def test_cross_client_dispatch_cannot_stamp_outer_recovery() -> None:
    client_a = _client(agent_id="seller-a")
    client_b = _client(agent_id="seller-b")

    async def b_dispatched(_params: dict[str, Any]) -> TaskResult[Any]:
        mark_task_dispatched(
            client_b.adapter.task_options_client_token,
            "build_creative",
            mutating=True,
            idempotency_key="0123456789abcdef-seller-b",
        )
        await asyncio.sleep(1)
        return _completed()

    async def a_read(_params: dict[str, Any]) -> TaskResult[Any]:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            return await client_b.build_creative_legacy(_Request())  # type: ignore[arg-type]

    with (
        patch.object(client_a.adapter, "get_products", new=a_read),
        patch.object(client_b.adapter, "build_creative", new=b_dispatched),
    ):
        with pytest.raises(ADCPTimeoutError) as caught:
            await client_a.get_products(_get_products_request(), options=TaskOptions(timeout=0.01))
    assert caught.value.agent_id == "seller-a"
    assert caught.value.recovery is None


def test_all_single_agent_protocol_tasks_expose_keyword_only_options() -> None:
    exclusions = {
        "close",
        "get_agent_info",
        "list_tools",
    }
    adapter_tasks = {
        name
        for name, member in inspect.getmembers(ProtocolAdapter, inspect.iscoroutinefunction)
        if not name.startswith("_") and name not in exclusions
    }
    workflow_tasks = {
        "execute_task",
        "execute_task_legacy",
        "refine_proposals_verified",
        "wait_for_refinement_verified",
    }
    legacy_only = {"build_creative", "list_creative_formats", "preview_creative"}
    methods = workflow_tasks | {
        f"{name}_legacy" if name in legacy_only else name for name in adapter_tasks
    }
    # Canonical creative tasks retain explicit legacy escape hatches too.
    methods |= {
        "create_media_buy_legacy",
        "get_creative_delivery_legacy",
        "get_media_buy_delivery_legacy",
        "get_media_buys_legacy",
        "get_products_legacy",
        "list_creatives_legacy",
        "sync_creatives_legacy",
        "update_media_buy_legacy",
    }

    missing: list[str] = []
    for name in sorted(methods):
        method = getattr(ADCPClient, name, None)
        if method is None:
            missing.append(name)
            continue
        parameter = inspect.signature(method).parameters.get("options")
        if parameter is None or parameter.kind is not inspect.Parameter.KEYWORD_ONLY:
            missing.append(name)
    assert missing == []


def test_multi_agent_client_does_not_claim_task_options_support() -> None:
    from adcp import ADCPMultiAgentClient

    assert "options" not in inspect.signature(ADCPMultiAgentClient.get_products).parameters
