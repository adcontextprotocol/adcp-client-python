"""Static public-surface checks for optional OpenTelemetry helpers."""

from typing import Any

from adcp import get_tracer, inject_trace_headers, is_tracing_available

available: bool = is_tracing_available()
tracer: Any | None = get_tracer()
headers: dict[str, str] = inject_trace_headers({"x-request-id": "request-1"})

assert isinstance(available, bool)
assert headers["x-request-id"] == "request-1"
