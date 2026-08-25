"""Optional OpenTelemetry tracing for AdCP client calls.

The module imports :mod:`opentelemetry-api` lazily.  Without the optional
dependency every helper is a no-op; with the API but no SDK/provider installed,
OpenTelemetry's own non-recording provider keeps the same no-op behavior.

Only W3C Trace Context is propagated.  Baggage is intentionally excluded from
the cross-agent boundary because application baggage can contain sensitive or
high-cardinality values.
"""

from __future__ import annotations

import re
import threading
from collections.abc import Iterator, Mapping, MutableMapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any

_INSTRUMENTATION_NAME = "adcp"
MCP_TRACEPARENT_META_KEY = "io.adcontextprotocol/traceparent"
MCP_TRACESTATE_META_KEY = "io.adcontextprotocol/tracestate"
_TRACEPARENT_RE = re.compile(
    r"^(?!ff)[0-9a-f]{2}-(?!0{32})[0-9a-f]{32}-(?!0{16})[0-9a-f]{16}-[0-9a-f]{2}$"
)
_SAFE_TOOL_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_SAFE_AGENT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")


@dataclass(frozen=True, slots=True)
class _OpenTelemetryBindings:
    trace: Any
    span_kind: Any
    status: Any
    status_code: Any
    trace_context_propagator: Any


_bindings: _OpenTelemetryBindings | None = None
_bindings_checked = False
_bindings_lock = threading.Lock()


def _import_bindings() -> _OpenTelemetryBindings | None:
    try:
        from opentelemetry import trace
        from opentelemetry.trace import SpanKind, Status, StatusCode
        from opentelemetry.trace.propagation.tracecontext import (
            TraceContextTextMapPropagator,
        )
    except ImportError:
        return None
    return _OpenTelemetryBindings(
        trace=trace,
        span_kind=SpanKind,
        status=Status,
        status_code=StatusCode,
        trace_context_propagator=TraceContextTextMapPropagator,
    )


def _load_bindings() -> _OpenTelemetryBindings | None:
    """Load the OpenTelemetry API once, returning ``None`` when absent."""

    global _bindings, _bindings_checked
    if _bindings_checked:
        return _bindings
    with _bindings_lock:
        if not _bindings_checked:
            # Publish ``checked`` only after the value is complete.  Client
            # construction is synchronous but may occur on several threads.
            _bindings = _import_bindings()
            _bindings_checked = True
    return _bindings


def is_tracing_available() -> bool:
    """Return whether the optional OpenTelemetry API is installed.

    Availability does not imply that spans are exported.  Applications remain
    responsible for installing/configuring an OpenTelemetry SDK and exporter.
    """

    return _load_bindings() is not None


def get_tracer() -> Any | None:
    """Return the AdCP tracer, or ``None`` without OpenTelemetry installed."""

    bindings = _load_bindings()
    if bindings is None:
        return None
    return bindings.trace.get_tracer(_INSTRUMENTATION_NAME)


def inject_trace_headers(
    carrier: MutableMapping[str, str] | None = None,
) -> dict[str, str]:
    """Inject the active W3C trace context into a string header mapping.

    The returned dictionary contains the full resulting carrier.  Existing
    ``traceparent`` and ``tracestate`` values are replaced only when an active,
    valid OpenTelemetry span produces new values.  W3C baggage is never
    propagated by this helper.
    """

    result = {name: value for name, value in (carrier or {}).items() if name.lower() != "baggage"}
    bindings = _load_bindings()
    if bindings is None:
        return result

    injected: dict[str, str] = {}
    bindings.trace_context_propagator().inject(injected)
    if not injected:
        return result
    for existing_name in tuple(result):
        if existing_name.lower() in {"traceparent", "tracestate"}:
            result.pop(existing_name)
    for name in ("traceparent", "tracestate"):
        value = injected.get(name)
        if value is not None:
            result[name] = value
    return result


_active_client_span: ContextVar[tuple[object, str, str] | None] = ContextVar(
    "adcp_active_client_span", default=None
)


def _safe_attributes(attributes: Mapping[str, object]) -> dict[str, str | int | float | bool]:
    allowed: dict[str, str | int | float | bool] = {}
    for key, value in attributes.items():
        if isinstance(value, str):
            allowed[key] = value if len(value) <= 128 and value.isprintable() else "unknown"
        elif isinstance(value, (int, float, bool)):
            allowed[key] = value
    return allowed


def safe_tool_name(value: object) -> str:
    """Return a bounded telemetry-safe wire task name."""

    return value if isinstance(value, str) and _SAFE_TOOL_RE.fullmatch(value) else "unknown"


def _safe_agent_id(value: object) -> str:
    return value if isinstance(value, str) and _SAFE_AGENT_RE.fullmatch(value) else "unknown"


@contextmanager
def client_task_span(
    client_token: object,
    *,
    protocol: str,
    task_name: str,
    agent_id: str,
    workflow: bool = False,
) -> Iterator[Any | None]:
    """Start one safe CLIENT span for an outermost client task.

    Nested public methods on the same client share the existing span.  Internal
    exception recording deliberately stores only the exception type, never its
    message, response body, request parameters, credentials, or remote prose.
    """

    span_type = "workflow" if workflow else "wire"
    safe_tool = safe_tool_name(task_name)
    active = _active_client_span.get()
    if active is not None and active[0] is client_token:
        # Generic execute_task delegates to the same public wire method: retain
        # one span.  Distinct nested tasks are separate logical RPCs (for
        # example a cold capability preflight), so each gets a child CLIENT
        # span.  Workflows remain one INTERNAL parent around their wire calls.
        if workflow or (active[1] == "wire" and active[2] == safe_tool):
            yield None
            return

    bindings = _load_bindings()
    tracer = get_tracer()
    if bindings is None or tracer is None:
        yield None
        return

    token = _active_client_span.set((client_token, span_type, safe_tool))
    safe_agent = _safe_agent_id(agent_id)
    if workflow:
        span_name = "adcp.client.workflow"
        span_kind = bindings.span_kind.INTERNAL
        attributes = _safe_attributes(
            {
                "adcp.agent.id": safe_agent,
                "adcp.protocol": protocol,
                "adcp.workflow.name": safe_tool,
            }
        )
    else:
        span_name = f"adcp.{protocol}.call_tool"
        span_kind = bindings.span_kind.CLIENT
        attributes = _safe_attributes(
            {
                "rpc.system.name": "adcp",
                "rpc.method": safe_tool,
                # `adcp.tool` matches the released JS SDK.  Keep the more
                # explicit Python key as an additive compatibility alias.
                "adcp.tool": safe_tool,
                "adcp.tool.name": safe_tool,
                "adcp.agent.id": safe_agent,
                "adcp.protocol": protocol,
            }
        )
    try:
        with tracer.start_as_current_span(
            span_name,
            kind=span_kind,
            attributes=attributes,
            record_exception=False,
            set_status_on_exception=False,
        ) as span:
            try:
                yield span
            except BaseException as exc:
                span.set_attribute("error.type", safe_tool_name(type(exc).__name__.lower()))
                span.set_status(bindings.status(bindings.status_code.ERROR))
                raise
    finally:
        _active_client_span.reset(token)


def set_task_result_attributes(span: Any | None, result: object) -> None:
    """Annotate a task span from public, bounded result metadata only."""

    if span is None:
        return
    success = getattr(result, "success", None)
    if isinstance(success, bool):
        span.set_attribute("adcp.task.success", success)
    status = getattr(result, "status", None)
    status_value = getattr(status, "value", status)
    if isinstance(status_value, str):
        span.set_attribute("adcp.task.status", status_value)
    if success is False:
        span.set_attribute("error.type", "adcp.task.failed")
        bindings = _load_bindings()
        if bindings is not None:
            span.set_status(bindings.status(bindings.status_code.ERROR))


def set_request_trace_headers(request: Any, carrier: Mapping[str, str]) -> None:
    """Apply validated W3C trace headers to an httpx-compatible request."""

    if "traceparent" not in carrier:
        return
    request.headers.pop("traceparent", None)
    request.headers.pop("tracestate", None)
    for name in ("traceparent", "tracestate"):
        value = carrier.get(name)
        if value is not None:
            request.headers.pop(name, None)
            request.headers[name] = value


def inject_trace_context(request: Any) -> None:
    """Inject active W3C trace headers into an httpx-compatible request."""

    set_request_trace_headers(request, inject_trace_headers())


def mcp_trace_meta() -> dict[str, str] | None:
    """Capture active trace context for MCP's long-lived writer task."""

    headers = inject_trace_headers()
    if "traceparent" not in headers:
        return None
    meta = {MCP_TRACEPARENT_META_KEY: headers["traceparent"]}
    if "tracestate" in headers:
        meta[MCP_TRACESTATE_META_KEY] = headers["tracestate"]
    return meta


def mcp_trace_headers_from_payload(payload: object) -> dict[str, str]:
    """Read the SDK's bounded trace bridge from an MCP JSON-RPC payload."""

    if not isinstance(payload, dict):
        return {}
    params = payload.get("params")
    if not isinstance(params, dict):
        return {}
    meta = params.get("_meta")
    if not isinstance(meta, dict):
        return {}
    traceparent = meta.get(MCP_TRACEPARENT_META_KEY)
    if not isinstance(traceparent, str) or _TRACEPARENT_RE.fullmatch(traceparent) is None:
        return {}
    result = {"traceparent": traceparent}
    tracestate = meta.get(MCP_TRACESTATE_META_KEY)
    if (
        isinstance(tracestate, str)
        and len(tracestate) <= 512
        and all(0x20 <= ord(character) <= 0x7E for character in tracestate)
    ):
        result["tracestate"] = tracestate
    return result


__all__ = ["get_tracer", "inject_trace_headers", "is_tracing_available"]
