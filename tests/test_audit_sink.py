"""Tests for :mod:`adcp.audit_sink` — AuditSink Protocol, reference impls,
and middleware composition.

Behavior under test:

* :class:`LoggingAuditSink` emits structured JSON via stdlib logging.
* :class:`SlackAlertSink` rejects non-HTTPS URLs at construction.
* :class:`SlackAlertSink.__repr__` does not leak the webhook URL.
* :class:`SlackAlertSink` filters ``details`` through ``allowed_fields``.
* :class:`SlackAlertSink` skips events outside ``sensitive_operations``.
* :class:`SlackAlertSink` routes through the IP-pinned transport with
  ``trust_env=False``.
* :class:`AuditEvent` is a frozen Pydantic model: assignment raises,
  unknown fields raise.
* :func:`make_audit_middleware` records on success AND on exception,
  re-raises exceptions to the dispatcher, and isolates sink timeouts /
  raises so they cannot wedge the dispatch hot path.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any
from unittest.mock import patch

import httpx
import pytest

from adcp.audit_sink import (
    AuditEvent,
    AuditSink,
    LoggingAuditSink,
    SlackAlertSink,
    make_audit_middleware,
)
from adcp.server import ToolContext

UTC = timezone.utc


# ----- helpers -----


def _ctx(
    *,
    caller: str | None = "principal-1",
    tenant: str | None = "tenant-a",
    request_id: str | None = "req-1",
) -> ToolContext:
    return ToolContext(
        request_id=request_id,
        caller_identity=caller,
        tenant_id=tenant,
    )


@dataclass
class _RecordingSink:
    """In-memory :class:`AuditSink` for assertions."""

    calls: list[AuditEvent] = field(default_factory=list)

    async def record(self, event: AuditEvent) -> None:
        self.calls.append(event)


@dataclass
class _BrokenSink:
    """Sink that always raises — middleware must swallow."""

    async def record(self, event: AuditEvent) -> None:
        raise RuntimeError("sink down")


@dataclass
class _SlowSink:
    """Sink that hangs longer than the middleware's timeout."""

    delay_seconds: float = 1.0

    async def record(self, event: AuditEvent) -> None:
        await asyncio.sleep(self.delay_seconds)


# ----- AuditEvent -----


def test_audit_event_is_frozen_with_field_defaults() -> None:
    event = AuditEvent(
        operation="create_media_buy",
        success=True,
        occurred_at=datetime.now(UTC),
    )
    assert event.caller_identity is None
    assert event.tenant_id is None
    assert event.request_id is None
    assert event.error_type is None
    assert event.error_message is None
    assert event.details == {}
    # Frozen Pydantic model: assignment raises ValidationError.
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        event.success = False  # type: ignore[misc]


def test_audit_event_rejects_unknown_fields() -> None:
    """``extra="forbid"`` so a typo'd field name fails fast, not silently
    drops to a sink that wouldn't have received it."""
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        AuditEvent(  # type: ignore[call-arg]
            operation="op",
            success=True,
            occurred_at=datetime.now(UTC),
            principal_id="oops",  # was renamed to caller_identity
        )


def test_audit_event_serializes_to_json_natively() -> None:
    """Pydantic gives us ``.model_dump_json()`` — sinks that need a JSON
    payload don't need a custom serializer with ``default=str``."""
    event = AuditEvent(
        operation="create_media_buy",
        success=True,
        occurred_at=datetime(2026, 5, 2, 12, 0, 0, tzinfo=UTC),
        caller_identity="principal-1",
        details={"media_buy_id": "mb-42"},
    )
    payload = json.loads(event.model_dump_json())
    assert payload["operation"] == "create_media_buy"
    assert payload["occurred_at"] == "2026-05-02T12:00:00Z"
    assert payload["details"] == {"media_buy_id": "mb-42"}


def test_audit_event_satisfies_protocol_with_recording_sink() -> None:
    # ``AuditSink`` is runtime-checkable.
    sink: AuditSink = _RecordingSink()
    assert isinstance(sink, AuditSink)


# ----- LoggingAuditSink -----


@pytest.mark.asyncio
async def test_logging_audit_sink_emits_structured_json(
    caplog: pytest.LogCaptureFixture,
) -> None:
    sink = LoggingAuditSink()
    event = AuditEvent(
        operation="acquire_rights",
        success=True,
        occurred_at=datetime(2026, 5, 2, 12, 0, 0, tzinfo=UTC),
        caller_identity="principal-1",
        tenant_id="tenant-a",
        request_id="req-42",
        details={"rights_id": "r-9", "amount_usd": 100},
    )

    with caplog.at_level(logging.INFO, logger="adcp.audit"):
        await sink.record(event)

    assert len(caplog.records) == 1
    payload = json.loads(caplog.records[0].message)
    assert payload["operation"] == "acquire_rights"
    assert payload["success"] is True
    assert payload["caller_identity"] == "principal-1"
    assert payload["tenant_id"] == "tenant-a"
    assert payload["request_id"] == "req-42"
    assert payload["details"] == {"rights_id": "r-9", "amount_usd": 100}
    assert payload["occurred_at"] == "2026-05-02T12:00:00Z"


@pytest.mark.asyncio
async def test_logging_audit_sink_records_failure_fields(
    caplog: pytest.LogCaptureFixture,
) -> None:
    sink = LoggingAuditSink()
    event = AuditEvent(
        operation="create_media_buy",
        success=False,
        occurred_at=datetime.now(UTC),
        error_type="IdempotencyConflictError",
        error_message="duplicate key",
    )
    with caplog.at_level(logging.INFO, logger="adcp.audit"):
        await sink.record(event)
    payload = json.loads(caplog.records[0].message)
    assert payload["success"] is False
    assert payload["error_type"] == "IdempotencyConflictError"
    assert payload["error_message"] == "duplicate key"


@pytest.mark.asyncio
async def test_logging_audit_sink_uses_custom_logger_and_level(
    caplog: pytest.LogCaptureFixture,
) -> None:
    custom = logging.getLogger("test.custom_audit")
    sink = LoggingAuditSink(logger_=custom, level=logging.DEBUG)
    event = AuditEvent(operation="get_products", success=True, occurred_at=datetime.now(UTC))
    with caplog.at_level(logging.DEBUG, logger="test.custom_audit"):
        await sink.record(event)
    assert len(caplog.records) == 1
    assert caplog.records[0].name == "test.custom_audit"
    assert caplog.records[0].levelno == logging.DEBUG


# ----- SlackAlertSink -----


def test_slack_alert_sink_rejects_non_https() -> None:
    with pytest.raises(ValueError, match="must be HTTPS"):
        SlackAlertSink("http://hooks.slack.com/services/x/y/z")


def test_slack_alert_sink_repr_redacts_webhook_url() -> None:
    sink = SlackAlertSink(
        "https://hooks.slack.com/services/T000/B000/SECRET-TOKEN-AAA",
        sensitive_operations=frozenset({"create_media_buy", "acquire_rights"}),
    )
    rendered = repr(sink)
    assert "SECRET-TOKEN-AAA" not in rendered
    assert "hooks.slack.com" not in rendered
    assert "SlackAlertSink" in rendered


def test_slack_alert_sink_repr_with_no_filter() -> None:
    sink = SlackAlertSink(
        "https://hooks.slack.com/services/T000/B000/SECRET-TOKEN-BBB",
    )
    rendered = repr(sink)
    assert "SECRET-TOKEN-BBB" not in rendered
    # sentinel signaling "no filter applied"
    assert "all" in rendered


@pytest.mark.asyncio
async def test_slack_alert_sink_skips_non_sensitive_operations() -> None:
    """``sensitive_operations`` whitelist gates the HTTP call entirely."""
    sink = SlackAlertSink(
        "https://hooks.slack.com/services/T000/B000/x",
        sensitive_operations=frozenset({"create_media_buy"}),
    )
    event = AuditEvent(
        operation="get_products",
        success=True,
        occurred_at=datetime.now(UTC),
    )
    with patch("httpx.AsyncClient") as mock_client_cls:
        await sink.record(event)
    mock_client_cls.assert_not_called()


@pytest.mark.asyncio
async def test_slack_alert_sink_posts_to_webhook_with_pinned_transport() -> None:
    """Happy path: HTTPS POST through IP-pinned transport, ``trust_env=False``."""
    captured: dict[str, Any] = {}

    def _handler(request: httpx.Request) -> httpx.Response:
        captured["method"] = request.method
        captured["url"] = str(request.url)
        captured["body"] = json.loads(request.content.decode())
        return httpx.Response(200, text="ok")

    mock_transport = httpx.MockTransport(_handler)

    sink = SlackAlertSink(
        "https://hooks.slack.com/services/T000/B000/x",
        sensitive_operations=frozenset({"create_media_buy"}),
    )
    event = AuditEvent(
        operation="create_media_buy",
        success=True,
        occurred_at=datetime(2026, 5, 2, 12, 0, 0, tzinfo=UTC),
        caller_identity="principal-1",
        tenant_id="tenant-a",
    )

    with patch(
        "adcp.signing.ip_pinned_transport.build_async_ip_pinned_transport",
        return_value=mock_transport,
    ) as mock_build:
        await sink.record(event)

    mock_build.assert_called_once()
    # SSRF-safe defaults flow through.
    _, kwargs = mock_build.call_args
    assert kwargs["allow_private"] is False
    assert kwargs["allowed_ports"] is None

    assert captured["method"] == "POST"
    assert captured["url"] == "https://hooks.slack.com/services/T000/B000/x"
    assert "create_media_buy" in captured["body"]["text"]
    assert "principal-1" in captured["body"]["text"]
    assert "tenant-a" in captured["body"]["text"]


@pytest.mark.asyncio
async def test_slack_alert_sink_default_allowlist_drops_all_details() -> None:
    """Default ``allowed_fields=frozenset()`` — no ``details`` reaches Slack."""
    captured: dict[str, Any] = {}

    def _handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content.decode())
        return httpx.Response(200, text="ok")

    sink = SlackAlertSink("https://hooks.slack.com/services/T/B/x")
    event = AuditEvent(
        operation="create_media_buy",
        success=True,
        occurred_at=datetime.now(UTC),
        details={
            "budget_usd": 50000,
            "credit_limit": 100000,
            "buyer_email": "ops@buyer.example",
        },
    )

    with patch(
        "adcp.signing.ip_pinned_transport.build_async_ip_pinned_transport",
        return_value=httpx.MockTransport(_handler),
    ):
        await sink.record(event)

    text = captured["body"]["text"]
    assert "budget_usd" not in text
    assert "credit_limit" not in text
    assert "buyer_email" not in text
    assert "ops@buyer.example" not in text


@pytest.mark.asyncio
async def test_slack_alert_sink_omits_exception_message_by_default() -> None:
    captured: dict[str, Any] = {}

    def _handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content.decode())
        return httpx.Response(200, text="ok")

    sink = SlackAlertSink("https://hooks.slack.com/services/T/B/x")
    event = AuditEvent(
        operation="create_media_buy",
        success=False,
        occurred_at=datetime.now(UTC),
        error_type="RuntimeError",
        error_message="Authorization=Bearer secret-token",
    )

    with patch(
        "adcp.signing.ip_pinned_transport.build_async_ip_pinned_transport",
        return_value=httpx.MockTransport(_handler),
    ):
        await sink.record(event)

    text = captured["body"]["text"]
    assert "RuntimeError" in text
    assert "secret-token" not in text
    assert "Authorization" not in text


@pytest.mark.asyncio
async def test_slack_alert_sink_emits_only_allowlisted_details() -> None:
    captured: dict[str, Any] = {}

    def _handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content.decode())
        return httpx.Response(200, text="ok")

    sink = SlackAlertSink(
        "https://hooks.slack.com/services/T/B/x",
        allowed_fields=frozenset({"media_buy_id"}),
    )
    event = AuditEvent(
        operation="create_media_buy",
        success=True,
        occurred_at=datetime.now(UTC),
        details={
            "media_buy_id": "mb-42",
            "budget_usd": 50000,  # filtered out
        },
    )

    with patch(
        "adcp.signing.ip_pinned_transport.build_async_ip_pinned_transport",
        return_value=httpx.MockTransport(_handler),
    ):
        await sink.record(event)

    text = captured["body"]["text"]
    assert "mb-42" in text
    assert "budget_usd" not in text
    assert "50000" not in text


@pytest.mark.asyncio
async def test_slack_alert_sink_raises_on_non_2xx() -> None:
    """Non-2xx surfaces as an exception so the middleware's swallow path
    can log it (rate-limit 429, revoked-webhook 404 must reach operator
    logs even though they can't fail dispatch)."""

    def _handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, text="rate_limited")

    sink = SlackAlertSink("https://hooks.slack.com/services/T/B/x")
    event = AuditEvent(
        operation="create_media_buy",
        success=True,
        occurred_at=datetime.now(UTC),
    )

    with patch(
        "adcp.signing.ip_pinned_transport.build_async_ip_pinned_transport",
        return_value=httpx.MockTransport(_handler),
    ):
        with pytest.raises(httpx.HTTPStatusError):
            await sink.record(event)


# ----- make_audit_middleware -----


@pytest.mark.asyncio
async def test_middleware_records_on_success() -> None:
    sink = _RecordingSink()
    middleware = make_audit_middleware([sink], include_error_message=True)

    async def handler() -> dict[str, str]:
        return {"ok": "yes"}

    result = await middleware("create_media_buy", {"foo": "bar"}, _ctx(), handler)

    assert result == {"ok": "yes"}
    assert len(sink.calls) == 1
    event = sink.calls[0]
    assert event.operation == "create_media_buy"
    assert event.success is True
    assert event.caller_identity == "principal-1"
    assert event.tenant_id == "tenant-a"
    assert event.request_id == "req-1"
    assert event.error_type is None
    assert event.error_message is None


@pytest.mark.asyncio
async def test_middleware_records_failure_and_reraises() -> None:
    sink = _RecordingSink()
    middleware = make_audit_middleware([sink], include_error_message=True)

    class _BoomError(RuntimeError):
        pass

    async def handler() -> Any:
        raise _BoomError("kapow")

    with pytest.raises(_BoomError, match="kapow"):
        await middleware("create_media_buy", {}, _ctx(), handler)

    assert len(sink.calls) == 1
    event = sink.calls[0]
    assert event.success is False
    assert event.error_type == "_BoomError"
    assert event.error_message == "kapow"


@pytest.mark.asyncio
async def test_middleware_truncates_long_error_messages() -> None:
    sink = _RecordingSink()
    middleware = make_audit_middleware([sink], include_error_message=True)

    async def handler() -> Any:
        raise RuntimeError("x" * 500)

    with pytest.raises(RuntimeError):
        await middleware("op", {}, _ctx(), handler)

    assert len(sink.calls[0].error_message or "") == 200


@pytest.mark.asyncio
async def test_middleware_isolates_sink_timeout(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A hung sink must not wedge dispatch."""
    slow = _SlowSink(delay_seconds=10.0)
    middleware = make_audit_middleware([slow], sink_timeout_seconds=0.05)

    async def handler() -> str:
        return "ok"

    with caplog.at_level(logging.WARNING, logger="adcp.audit_sink"):
        result = await asyncio.wait_for(
            middleware("op", {}, _ctx(), handler),
            timeout=2.0,
        )

    assert result == "ok"
    assert any("timed out" in r.message for r in caplog.records)


@pytest.mark.asyncio
async def test_middleware_isolates_sink_raise(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A raising sink must not wedge dispatch — handler return is preserved."""
    middleware = make_audit_middleware([_BrokenSink()])

    async def handler() -> str:
        return "ok"

    with caplog.at_level(logging.WARNING, logger="adcp.audit_sink"):
        result = await middleware("op", {}, _ctx(), handler)

    assert result == "ok"
    assert any("raised" in r.message for r in caplog.records)


@pytest.mark.asyncio
async def test_middleware_continues_after_one_bad_sink() -> None:
    """A broken sink must not block sibling sinks."""
    good = _RecordingSink()
    middleware = make_audit_middleware([_BrokenSink(), good])

    async def handler() -> str:
        return "ok"

    result = await middleware("op", {}, _ctx(), handler)
    assert result == "ok"
    assert len(good.calls) == 1


@pytest.mark.asyncio
async def test_middleware_failure_path_isolates_sink_raise() -> None:
    """Even on the exception path, a broken sink doesn't swallow the
    handler's exception or block sibling sinks."""
    good = _RecordingSink()
    middleware = make_audit_middleware([_BrokenSink(), good])

    async def handler() -> Any:
        raise ValueError("nope")

    with pytest.raises(ValueError, match="nope"):
        await middleware("op", {}, _ctx(), handler)

    assert len(good.calls) == 1
    assert good.calls[0].success is False


@pytest.mark.asyncio
async def test_middleware_with_empty_sinks_is_noop_passthrough() -> None:
    middleware = make_audit_middleware([])

    async def handler() -> str:
        return "ok"

    assert await middleware("op", {}, _ctx(), handler) == "ok"


@pytest.mark.asyncio
async def test_middleware_handles_unauthenticated_context() -> None:
    """An unauthenticated request (caller_identity=None) still produces
    an audit event — silent gaps in the audit trail are worse than null
    fields."""
    sink = _RecordingSink()
    middleware = make_audit_middleware([sink])

    async def handler() -> str:
        return "ok"

    await middleware(
        "get_adcp_capabilities",
        {},
        _ctx(caller=None, tenant=None, request_id=None),
        handler,
    )
    assert len(sink.calls) == 1
    event = sink.calls[0]
    assert event.caller_identity is None
    assert event.tenant_id is None
    assert event.request_id is None
