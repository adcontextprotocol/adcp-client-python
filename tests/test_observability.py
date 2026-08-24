"""Optional OpenTelemetry span and propagation contracts."""

from __future__ import annotations

import re
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from pydantic import TypeAdapter

import adcp
from adcp import ADCPClient
from adcp import observability as obs
from adcp.protocols.a2a import A2AAdapter
from adcp.protocols.mcp import MCPAdapter
from adcp.signing.autosign import current_operation
from adcp.types import GetAdcpCapabilitiesResponse, GetProductsRequest, SyncCatalogsRequest
from adcp.types.core import AgentConfig, Protocol, TaskResult, TaskStatus


def _config(protocol: Protocol = Protocol.MCP) -> AgentConfig:
    path = "/mcp" if protocol is Protocol.MCP else "/agent"
    return AgentConfig(
        id="seller-safe-id",
        agent_uri=f"https://seller.example{path}",
        protocol=protocol,
    )


def _request() -> GetProductsRequest:
    return TypeAdapter(GetProductsRequest).validate_python(
        {"buying_mode": "brief", "brief": "observability test"}
    )


@pytest.fixture
def span_exporter(monkeypatch: pytest.MonkeyPatch) -> InMemorySpanExporter:
    """Route this module's tracer through an isolated in-memory provider."""

    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    bindings = obs._load_bindings()
    assert bindings is not None
    monkeypatch.setattr(bindings.trace, "get_tracer", provider.get_tracer)
    return exporter


@pytest.mark.asyncio
async def test_client_task_creates_one_bounded_span(
    span_exporter: InMemorySpanExporter,
) -> None:
    client = ADCPClient(_config())

    async def completed(_params: dict[str, Any]) -> TaskResult[Any]:
        return TaskResult(
            status=TaskStatus.COMPLETED,
            success=True,
            data={"products": []},
        )

    with patch.object(client.adapter, "get_products", new=completed):
        result = await client.get_products(_request())

    assert result.success
    spans = span_exporter.get_finished_spans()
    assert len(spans) == 1
    span = spans[0]
    assert span.name == "adcp.mcp.call_tool"
    assert dict(span.attributes or {}) == {
        "rpc.system.name": "adcp",
        "rpc.method": "get_products",
        "adcp.tool": "get_products",
        "adcp.tool.name": "get_products",
        "adcp.agent.id": "seller-safe-id",
        "adcp.protocol": "mcp",
        "adcp.task.success": True,
        "adcp.task.status": "completed",
    }
    assert span.events == ()


@pytest.mark.asyncio
async def test_failed_result_records_no_remote_prose(
    span_exporter: InMemorySpanExporter,
) -> None:
    client = ADCPClient(_config())
    secret = "remote-secret-shaped-error"

    async def failed(_params: dict[str, Any]) -> TaskResult[Any]:
        return TaskResult(
            status=TaskStatus.FAILED,
            success=False,
            error=secret,
        )

    with patch.object(client.adapter, "get_products", new=failed):
        result = await client.get_products(_request())

    assert not result.success
    span = span_exporter.get_finished_spans()[0]
    assert span.status.status_code is trace.StatusCode.ERROR
    assert span.attributes is not None
    assert span.attributes["error.type"] == "adcp.task.failed"
    rendered = repr(span)
    assert secret not in rendered
    assert span.events == ()


@pytest.mark.asyncio
async def test_nested_execute_task_does_not_duplicate_spans(
    span_exporter: InMemorySpanExporter,
) -> None:
    client = ADCPClient(_config())

    async def completed(_params: dict[str, Any]) -> TaskResult[Any]:
        return TaskResult(
            status=TaskStatus.COMPLETED,
            success=True,
            data={"products": []},
        )

    with patch.object(client.adapter, "get_products", new=completed):
        result = await client.execute_task("get_products", _request())

    assert result.success
    spans = span_exporter.get_finished_spans()
    assert [span.name for span in spans] == ["adcp.mcp.call_tool"]
    assert spans[0].attributes is not None
    assert spans[0].attributes["adcp.tool.name"] == "get_products"


@pytest.mark.asyncio
async def test_cold_strict_idempotency_preflight_gets_distinct_wire_span(
    span_exporter: InMemorySpanExporter,
) -> None:
    client = ADCPClient(_config(), strict_idempotency=True)
    capabilities = GetAdcpCapabilitiesResponse.model_validate(
        {
            "adcp": {
                "major_versions": [3],
                "idempotency": {"supported": True, "replay_ttl_seconds": 86400},
            },
            "supported_protocols": ["media_buy"],
        }
    )

    async def get_capabilities(_params: dict[str, Any]) -> TaskResult[Any]:
        return TaskResult(
            status=TaskStatus.COMPLETED,
            success=True,
            data=capabilities.model_dump(mode="json", exclude_none=True),
        )

    async def sync_catalogs(_params: dict[str, Any]) -> TaskResult[Any]:
        assert client.adapter.idempotency_capability_check is not None
        await client.adapter.idempotency_capability_check()
        return TaskResult(status=TaskStatus.FAILED, success=False, error="expected test result")

    with (
        patch.object(client.adapter, "get_adcp_capabilities", new=get_capabilities),
        patch.object(client.adapter, "sync_catalogs", new=sync_catalogs),
    ):
        result = await client.sync_catalogs(SyncCatalogsRequest.model_construct())

    assert not result.success
    capability_span, target_span = span_exporter.get_finished_spans()
    assert capability_span.attributes is not None
    assert capability_span.attributes["rpc.method"] == "get_adcp_capabilities"
    assert target_span.attributes is not None
    assert target_span.attributes["rpc.method"] == "sync_catalogs"
    assert capability_span.parent is not None
    assert capability_span.parent.span_id == target_span.context.span_id


@pytest.mark.asyncio
async def test_invalid_generic_name_and_agent_id_are_not_exported(
    span_exporter: InMemorySpanExporter,
) -> None:
    secret_task = "secret_" + "x" * 500
    secret_agent = "agent secret " + "y" * 500
    client = ADCPClient(
        AgentConfig(
            id=secret_agent,
            agent_uri="https://seller.example/mcp",
            protocol=Protocol.MCP,
        )
    )

    with pytest.raises(ValueError, match="Unknown canonical"):
        await client.execute_task(secret_task, _request())

    span = span_exporter.get_finished_spans()[0]
    assert span.attributes is not None
    assert span.attributes["rpc.method"] == "unknown"
    assert span.attributes["adcp.tool"] == "unknown"
    assert span.attributes["adcp.agent.id"] == "unknown"
    assert secret_task not in repr(span)
    assert secret_agent not in repr(span)


def test_workflow_span_uses_internal_schema_and_child_wire_spans(
    span_exporter: InMemorySpanExporter,
) -> None:
    token = object()
    with obs.client_task_span(
        token,
        protocol="mcp",
        task_name="wait_for_refinement_verified",
        agent_id="seller-safe-id",
        workflow=True,
    ):
        with obs.client_task_span(
            token,
            protocol="mcp",
            task_name="get_task_status",
            agent_id="seller-safe-id",
        ):
            pass

    child, workflow = span_exporter.get_finished_spans()
    assert child.name == "adcp.mcp.call_tool"
    assert child.attributes is not None
    assert child.attributes["rpc.method"] == "get_task_status"
    assert workflow.name == "adcp.client.workflow"
    assert workflow.kind is trace.SpanKind.INTERNAL
    assert workflow.attributes is not None
    assert workflow.attributes["adcp.workflow.name"] == "wait_for_refinement_verified"
    assert "adcp.tool" not in workflow.attributes


def test_trace_headers_are_w3c_only_and_replace_stale_case(
    span_exporter: InMemorySpanExporter,
) -> None:
    del span_exporter
    tracer = obs.get_tracer()
    assert tracer is not None

    with tracer.start_as_current_span("parent"):
        headers = adcp.inject_trace_headers(
            {
                "Traceparent": "00-00000000000000000000000000000000-0000000000000000-00",
                "TraceState": "secret_vendor=stale",
                "baggage": "tenant=must-not-be-generated-by-adcp",
            }
        )

    assert "Traceparent" not in headers
    assert re.fullmatch(r"00-[0-9a-f]{32}-[0-9a-f]{16}-[0-9a-f]{2}", headers["traceparent"])
    assert not any(name.lower() == "tracestate" for name in headers)
    assert "baggage" not in headers


@pytest.mark.asyncio
async def test_transport_hook_skips_discovery_and_injects_tool_requests(
    span_exporter: InMemorySpanExporter,
) -> None:
    del span_exporter
    client = ADCPClient(_config())
    tracer = obs.get_tracer()
    assert tracer is not None

    discovery = httpx.Request("GET", "https://seller.example/.well-known/agent-card.json")
    with tracer.start_as_current_span("parent"):
        trace_meta = obs.mcp_trace_meta()
        await client._inject_outgoing_trace_context(discovery)
    assert trace_meta is not None

    # The hook runs later in MCP's long-lived writer task, outside the caller's
    # ContextVar snapshot.  The bounded `_meta` bridge retains the exact parent.
    tool = httpx.Request(
        "POST",
        "https://seller.example/mcp",
        headers={"TraceState": "secret_vendor=stale"},
        json={
            "jsonrpc": "2.0",
            "method": "tools/call",
            "params": {"name": "get_products", "_meta": trace_meta},
        },
    )
    await client._inject_outgoing_trace_context(tool)

    assert "traceparent" not in discovery.headers
    assert "traceparent" in tool.headers
    assert tool.headers["traceparent"] == trace_meta[obs.MCP_TRACEPARENT_META_KEY]
    assert "tracestate" not in tool.headers
    assert "baggage" not in tool.headers


@pytest.mark.asyncio
async def test_a2a_operation_scope_enables_trace_injection(
    span_exporter: InMemorySpanExporter,
) -> None:
    del span_exporter
    client = ADCPClient(_config(Protocol.A2A))
    tracer = obs.get_tracer()
    assert tracer is not None
    request = httpx.Request("POST", "https://seller.example/agent")

    token = current_operation.set("get_products")
    try:
        with tracer.start_as_current_span("parent"):
            await client._inject_outgoing_trace_context(request)
    finally:
        current_operation.reset(token)

    assert "traceparent" in request.headers


@pytest.mark.asyncio
async def test_mcp_dispatch_captures_trace_for_writer_task(
    span_exporter: InMemorySpanExporter,
) -> None:
    del span_exporter
    adapter = MCPAdapter(_config())
    captured_meta: dict[str, str] | None = None

    async def call_tool(
        _name: str,
        _params: dict[str, Any],
        *,
        meta: dict[str, str] | None = None,
    ) -> Any:
        nonlocal captured_meta
        captured_meta = meta
        result = MagicMock()
        result.isError = False
        result.content = []
        result.structuredContent = None
        return result

    session = MagicMock()
    session.call_tool = call_tool
    adapter._get_session = AsyncMock(return_value=session)  # type: ignore[method-assign]
    tracer = obs.get_tracer()
    assert tracer is not None

    with tracer.start_as_current_span("parent"):
        await adapter._call_mcp_tool(
            "get_products", _request().model_dump(mode="json", exclude_none=True)
        )

    assert captured_meta is not None
    assert obs.MCP_TRACEPARENT_META_KEY in captured_meta


@pytest.mark.asyncio
async def test_protocol_clients_install_trace_hook_before_signing() -> None:
    async def tracing(_request: Any) -> None:
        return None

    async def signing(_request: Any) -> None:
        return None

    mcp = MCPAdapter(_config())
    mcp.tracing_request_hook = tracing
    mcp.signing_request_hook = signing
    mcp_client = mcp._streamable_http_client_factory()()
    try:
        assert mcp_client.event_hooks["request"] == [tracing, signing]
    finally:
        await mcp_client.aclose()

    a2a = A2AAdapter(_config(Protocol.A2A))
    a2a.tracing_request_hook = tracing
    a2a.signing_request_hook = signing
    a2a_client = await a2a._get_httpx_client()
    try:
        assert a2a_client.event_hooks["request"] == [tracing, signing]
    finally:
        await a2a.close()


def test_no_op_behavior_without_optional_api(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(obs, "_bindings_checked", True)
    monkeypatch.setattr(obs, "_bindings", None)
    assert not obs.is_tracing_available()
    assert obs.get_tracer() is None
    assert obs.inject_trace_headers({"x-existing": "value"}) == {"x-existing": "value"}


def test_concurrent_first_load_never_observes_partial_bindings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    expected = obs._import_bindings()
    assert expected is not None
    entered = threading.Event()
    release = threading.Event()
    second_started = threading.Event()
    second_returned = threading.Event()
    import_count = 0

    def blocked_import() -> obs._OpenTelemetryBindings | None:
        nonlocal import_count
        import_count += 1
        entered.set()
        assert release.wait(2)
        return expected

    def second_load() -> obs._OpenTelemetryBindings | None:
        second_started.set()
        result = obs._load_bindings()
        second_returned.set()
        return result

    monkeypatch.setattr(obs, "_bindings", None)
    monkeypatch.setattr(obs, "_bindings_checked", False)
    monkeypatch.setattr(obs, "_import_bindings", blocked_import)

    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(obs._load_bindings)
        assert entered.wait(1)
        second = executor.submit(second_load)
        assert second_started.wait(1)
        try:
            assert not second_returned.wait(0.1)
        finally:
            release.set()
        assert first.result(timeout=1) is expected
        assert second.result(timeout=1) is expected
    assert import_count == 1


@pytest.mark.parametrize(
    "traceparent,tracestate",
    [
        ("00-00000000000000000000000000000000-1111111111111111-01", "vendor=value"),
        ("not-a-traceparent", "vendor=value"),
        ("ff-11111111111111111111111111111111-1111111111111111-01", "vendor=value"),
        ("00-11111111111111111111111111111111-1111111111111111-01", "bad\r\nheader"),
    ],
)
def test_mcp_trace_bridge_rejects_invalid_or_unsafe_values(
    traceparent: str,
    tracestate: str,
) -> None:
    headers = obs.mcp_trace_headers_from_payload(
        {
            "params": {
                "_meta": {
                    obs.MCP_TRACEPARENT_META_KEY: traceparent,
                    obs.MCP_TRACESTATE_META_KEY: tracestate,
                }
            }
        }
    )
    if traceparent.startswith("00-111"):
        assert headers == {"traceparent": traceparent}
    else:
        assert headers == {}


def test_public_observability_exports() -> None:
    assert adcp.get_tracer is obs.get_tracer
    assert adcp.inject_trace_headers is obs.inject_trace_headers
    assert adcp.is_tracing_available is obs.is_tracing_available
    assert set(obs.__all__) == {
        "get_tracer",
        "inject_trace_headers",
        "is_tracing_available",
    }
