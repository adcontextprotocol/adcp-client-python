"""Tests for :mod:`adcp.webhook_supervisor` — retry, circuit breaker,
attempt audit log, F12 wire-through.

Behavior under test (per the Protocol contract):

* :class:`InMemoryWebhookDeliverySupervisor` retries failed deliveries
  per :class:`RetryPolicy` and records each attempt to a configured
  :class:`DeliveryLogSink`.
* :class:`_CircuitBreaker` opens after ``failure_threshold`` consecutive
  failures, tests recovery in HALF_OPEN, and skips delivery when OPEN.
* The F12 auto-emit gate routes through ``webhook_supervisor`` when
  configured, falling back to ``webhook_sender`` otherwise.
* Boot validation accepts either a sender or a supervisor.

Tests use a fake :class:`WebhookSender` (just records calls) — exercising
the supervisor's orchestration without real HTTP. Real-HTTP coverage
lives in :mod:`tests.test_webhook_handling` and is out of scope here.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from adcp.decisioning.webhook_emit import (
    maybe_emit_sync_completion,
    validate_webhook_sender_for_platform,
)
from adcp.webhook_sender import WebhookDeliveryResult

# 3.10 doesn't have ``datetime.UTC`` (added in 3.11); alias to ``timezone.utc``.
UTC = timezone.utc

from adcp.webhook_supervisor import (
    CircuitBreakerPolicy,
    CircuitState,
    DeliveryAttempt,
    InMemoryWebhookDeliverySupervisor,
    RetryPolicy,
    WebhookDeliverySupervisor,
    _CircuitBreaker,
)

# ----- helpers -----


def _ok(idempotency_key: str = "k1", status_code: int = 200) -> WebhookDeliveryResult:
    return WebhookDeliveryResult(
        status_code=status_code,
        idempotency_key=idempotency_key,
        url="https://buyer.example.com/wh",
        response_headers={},
        response_body=b"{}",
        sent_body=b'{"x":1}',
    )


def _fail(status_code: int = 503) -> WebhookDeliveryResult:
    return WebhookDeliveryResult(
        status_code=status_code,
        idempotency_key="k1",
        url="https://buyer.example.com/wh",
        response_headers={},
        response_body=b"upstream",
        sent_body=b'{"x":1}',
    )


@dataclass
class _RecordingSink:
    """In-memory :class:`DeliveryLogSink` for assertions."""

    calls: list[DeliveryAttempt] = field(default_factory=list)

    async def record(self, attempt: DeliveryAttempt) -> None:
        self.calls.append(attempt)


@dataclass
class _BrokenSink:
    """Sink that raises — supervisor must swallow and continue."""

    async def record(self, attempt: DeliveryAttempt) -> None:
        raise RuntimeError("sink down")


def _supervisor(
    sender: Any,
    *,
    retry: RetryPolicy | None = None,
    circuit: CircuitBreakerPolicy | None = None,
    sink: Any = None,
) -> InMemoryWebhookDeliverySupervisor:
    return InMemoryWebhookDeliverySupervisor(
        sender,
        retry=retry or RetryPolicy(max_attempts=3, base_delay_seconds=0.0, jitter=False),
        circuit=circuit,
        log_sink=sink,
    )


def _make_sender(send_mcp_returns=None, resend_returns=None) -> MagicMock:
    """Build a sender mock with both ``send_mcp`` and ``resend`` async-mocked.

    ``resend`` is invoked by the supervisor on attempts 2+ to replay
    the same idempotency_key per spec. Tests that exercise the retry
    path must configure both.
    """
    sender = MagicMock()
    if send_mcp_returns is not None:
        sender.send_mcp = AsyncMock(**send_mcp_returns)
    else:
        sender.send_mcp = AsyncMock(return_value=_ok())
    if resend_returns is not None:
        sender.resend = AsyncMock(**resend_returns)
    else:
        sender.resend = AsyncMock(return_value=_ok())
    return sender


# ----- RetryPolicy.delay_for_attempt -----


def test_retry_policy_no_delay_on_first_attempt() -> None:
    policy = RetryPolicy(base_delay_seconds=2.0, jitter=False)
    assert policy.delay_for_attempt(1) == 0.0


def test_retry_policy_exponential_backoff_without_jitter() -> None:
    policy = RetryPolicy(base_delay_seconds=2.0, max_delay_seconds=60.0, jitter=False)
    # attempt 2 → 2s, attempt 3 → 4s, attempt 4 → 8s, capped at max
    assert policy.delay_for_attempt(2) == 2.0
    assert policy.delay_for_attempt(3) == 4.0
    assert policy.delay_for_attempt(4) == 8.0


def test_retry_policy_caps_at_max_delay() -> None:
    policy = RetryPolicy(base_delay_seconds=10.0, max_delay_seconds=15.0, jitter=False)
    # attempt 3 would be 20s but capped
    assert policy.delay_for_attempt(3) == 15.0
    assert policy.delay_for_attempt(10) == 15.0


def test_retry_policy_jitter_within_half_to_full() -> None:
    policy = RetryPolicy(base_delay_seconds=4.0, jitter=True)
    # Jitter scales delay to [0.5x, 1.0x] — bounded check
    for _ in range(20):
        d = policy.delay_for_attempt(2)
        assert 2.0 <= d <= 4.0


# ----- _CircuitBreaker -----


def test_circuit_breaker_opens_after_failure_threshold() -> None:
    breaker = _CircuitBreaker(CircuitBreakerPolicy(failure_threshold=3, open_timeout_seconds=60))
    assert breaker.state is CircuitState.CLOSED
    breaker.record_failure()
    breaker.record_failure()
    assert breaker.state is CircuitState.CLOSED
    assert breaker.can_attempt()
    breaker.record_failure()
    assert breaker.state is CircuitState.OPEN
    assert not breaker.can_attempt()


def test_circuit_breaker_half_open_after_timeout() -> None:
    breaker = _CircuitBreaker(CircuitBreakerPolicy(failure_threshold=2, open_timeout_seconds=0.001))
    breaker.record_failure()
    breaker.record_failure()
    assert breaker.state is CircuitState.OPEN
    # Force the opened_at to be in the past so timeout has elapsed.
    breaker._opened_at = datetime.fromtimestamp(0, tz=UTC)
    assert breaker.can_attempt()
    assert breaker.state is CircuitState.HALF_OPEN


def test_circuit_breaker_closes_after_success_threshold_in_half_open() -> None:
    breaker = _CircuitBreaker(
        CircuitBreakerPolicy(failure_threshold=2, success_threshold=2, open_timeout_seconds=0.001)
    )
    breaker.record_failure()
    breaker.record_failure()
    breaker._opened_at = datetime.fromtimestamp(0, tz=UTC)
    breaker.can_attempt()  # transition to HALF_OPEN
    breaker.record_success()
    assert breaker.state is CircuitState.HALF_OPEN
    breaker.record_success()
    assert breaker.state is CircuitState.CLOSED


def test_circuit_breaker_failure_in_half_open_reopens() -> None:
    breaker = _CircuitBreaker(
        CircuitBreakerPolicy(failure_threshold=2, success_threshold=2, open_timeout_seconds=0.001)
    )
    breaker.record_failure()
    breaker.record_failure()
    breaker._opened_at = datetime.fromtimestamp(0, tz=UTC)
    breaker.can_attempt()
    breaker.record_failure()
    assert breaker.state is CircuitState.OPEN


# ----- Supervisor: success path -----


@pytest.mark.asyncio
async def test_supervisor_success_first_attempt_records_one_log() -> None:
    sender = MagicMock()
    sender.send_mcp = AsyncMock(return_value=_ok())
    sink = _RecordingSink()
    sup = _supervisor(sender, sink=sink)

    result = await sup.send_mcp(
        url="https://buyer.example.com/wh",
        task_id="t1",
        status="completed",
        task_type="create_media_buy",
        operation_id="op-supervisor-test",
        result={"media_buy_id": "mb_1"},
    )
    assert result is not None and result.ok
    assert sender.send_mcp.await_count == 1
    assert len(sink.calls) == 1
    assert sink.calls[0].outcome == "success"
    assert sink.calls[0].attempt_number == 1
    assert sink.calls[0].http_status_code == 200
    assert sink.calls[0].will_retry is False


@pytest.mark.asyncio
async def test_supervisor_threads_operation_id_to_sender() -> None:
    """The buyer-supplied operation_id is forwarded verbatim to the
    underlying WebhookSender.send_mcp so it is echoed into the payload."""
    sender = MagicMock()
    sender.send_mcp = AsyncMock(return_value=_ok())
    sup = _supervisor(sender)

    await sup.send_mcp(
        url="https://buyer.example.com/wh",
        task_id="t1",
        status="completed",
        task_type="create_media_buy",
        result={"media_buy_id": "mb_1"},
        operation_id="op-supervisor-123",
    )
    assert sender.send_mcp.await_args.kwargs["operation_id"] == "op-supervisor-123"


# ----- Supervisor: retry path -----


@pytest.mark.asyncio
async def test_supervisor_retries_on_5xx_then_succeeds() -> None:
    """Spec-compliant retry: attempt 1 calls ``send_mcp`` (fresh
    idempotency_key), attempts 2+ call ``resend(last_result)``
    (replays bytes including idempotency_key for receiver dedup)."""
    sender = MagicMock()
    sender.send_mcp = AsyncMock(return_value=_fail(503))
    sender.resend = AsyncMock(side_effect=[_fail(502), _ok()])
    sink = _RecordingSink()
    sup = _supervisor(sender, sink=sink)

    result = await sup.send_mcp(
        url="https://buyer.example.com/wh",
        task_id="t1",
        task_type="create_media_buy",
        operation_id="op-supervisor-test",
        status="completed",
    )
    assert result is not None and result.ok
    # Spec: only the first attempt calls ``send_mcp`` fresh; retries
    # call ``resend`` to preserve the idempotency_key.
    assert sender.send_mcp.await_count == 1
    assert sender.resend.await_count == 2
    # The same WebhookDeliveryResult instance is passed to each
    # resend (replays the same idempotency_key bytes).
    first_call = sender.resend.await_args_list[0][0][0]
    assert first_call.idempotency_key == "k1"
    assert [c.outcome for c in sink.calls] == ["failure", "failure", "success"]
    assert [c.attempt_number for c in sink.calls] == [1, 2, 3]
    assert sink.calls[0].will_retry is True
    assert sink.calls[1].will_retry is True
    assert sink.calls[2].will_retry is False


@pytest.mark.asyncio
async def test_supervisor_returns_last_failure_after_max_attempts() -> None:
    sender = MagicMock()
    sender.send_mcp = AsyncMock(return_value=_fail())
    sender.resend = AsyncMock(return_value=_fail())
    sink = _RecordingSink()
    sup = _supervisor(sender, sink=sink)

    result = await sup.send_mcp(
        url="https://buyer.example.com/wh",
        task_id="t1",
        task_type="create_media_buy",
        operation_id="op-supervisor-test",
        status="completed",
    )
    assert result is not None
    assert not result.ok
    assert sender.send_mcp.await_count == 1
    assert sender.resend.await_count == 2
    assert [c.outcome for c in sink.calls] == ["failure", "failure", "failure"]


@pytest.mark.asyncio
async def test_supervisor_records_exception_and_reraises_on_final_attempt() -> None:
    """When ``send_mcp`` raises mid-flight, no result-with-bytes is
    produced, so this best-effort helper calls ``send_mcp`` fresh again.
    The new key is why this helper cannot back beta.5 TaskHandoff push."""
    sender = MagicMock()
    sender.send_mcp = AsyncMock(side_effect=ConnectionError("dns"))
    sender.resend = AsyncMock(side_effect=AssertionError("should not be called"))
    sink = _RecordingSink()
    sup = _supervisor(sender, sink=sink)

    with pytest.raises(ConnectionError):
        await sup.send_mcp(
            url="https://buyer.example.com/wh",
            task_id="t1",
            task_type="create_media_buy",
            operation_id="op-supervisor-test",
            status="completed",
        )
    assert sender.send_mcp.await_count == 3
    assert sender.resend.await_count == 0
    assert len(sink.calls) == 3
    for attempt in sink.calls:
        assert attempt.outcome == "failure"
        assert attempt.http_status_code is None
        assert "ConnectionError" in (attempt.error_message or "")


# ----- Supervisor: circuit breaker -----


@pytest.mark.asyncio
async def test_supervisor_skips_delivery_when_circuit_open() -> None:
    sender = MagicMock()
    sender.send_mcp = AsyncMock(return_value=_fail())
    sender.resend = AsyncMock(return_value=_fail())
    sink = _RecordingSink()
    sup = _supervisor(
        sender,
        circuit=CircuitBreakerPolicy(failure_threshold=2, open_timeout_seconds=60),
        sink=sink,
    )

    # First delivery records 3 failures (max_attempts=3) — that's
    # 3 ≥ failure_threshold=2 so the breaker opens. The 2nd delivery
    # finds the circuit OPEN and skips entirely.
    await sup.send_mcp(
        url="https://buyer.example.com/wh",
        task_id="t1",
        task_type="create_media_buy",
        operation_id="op-supervisor-test",
        status="completed",
    )
    sink.calls.clear()

    result = await sup.send_mcp(
        url="https://buyer.example.com/wh",
        task_id="t2",
        task_type="create_media_buy",
        operation_id="op-supervisor-test",
        status="completed",
    )
    assert result is None
    assert len(sink.calls) == 1
    assert sink.calls[0].outcome == "circuit_open"
    assert sink.calls[0].attempt_number == 0
    # First delivery's attempts: 1 send_mcp + 2 resend (per spec
    # idempotency-key reuse). Second delivery is circuit_open and
    # makes no calls at all.
    assert sender.send_mcp.await_count == 1
    assert sender.resend.await_count == 2


@pytest.mark.asyncio
async def test_supervisor_isolates_breakers_per_endpoint() -> None:
    """Endpoint A's breaker opens (failures); endpoint B unaffected."""

    async def _routed(*, url: str, **_: Any) -> WebhookDeliveryResult:
        if url == "https://A/wh":
            return _fail()
        return _ok()

    sender = MagicMock()
    sender.send_mcp = AsyncMock(side_effect=_routed)
    # Resend always returns _fail() — the retry path on endpoint A.
    sender.resend = AsyncMock(return_value=_fail())
    sup = _supervisor(
        sender,
        circuit=CircuitBreakerPolicy(failure_threshold=2),
    )

    # Endpoint A fails enough to open its breaker (3 attempts ≥ 2).
    await sup.send_mcp(
        url="https://A/wh",
        task_id="t1",
        status="completed",
        task_type="create_media_buy",
        operation_id="op-supervisor-test",
    )
    # Endpoint B should be unaffected — succeeds on first attempt.
    result = await sup.send_mcp(
        url="https://B/wh",
        task_id="t2",
        status="completed",
        task_type="create_media_buy",
        operation_id="op-supervisor-test",
    )
    assert result is not None and result.ok


# ----- Supervisor: sequence numbers -----


def test_supervisor_next_sequence_is_per_key_and_monotonic() -> None:
    sup = _supervisor(MagicMock())
    assert sup.next_sequence("media_buy:1") == 1
    assert sup.next_sequence("media_buy:1") == 2
    assert sup.next_sequence("media_buy:2") == 1
    assert sup.next_sequence("media_buy:1") == 3


@pytest.mark.asyncio
async def test_supervisor_records_sequence_number_when_key_supplied() -> None:
    sender = MagicMock()
    sender.send_mcp = AsyncMock(return_value=_ok())
    sink = _RecordingSink()
    sup = _supervisor(sender, sink=sink)

    await sup.send_mcp(
        url="https://buyer.example.com/wh",
        task_id="t1",
        task_type="create_media_buy",
        operation_id="op-supervisor-test",
        status="completed",
        sequence_key="media_buy:abc",
    )
    await sup.send_mcp(
        url="https://buyer.example.com/wh",
        task_id="t2",
        task_type="create_media_buy",
        operation_id="op-supervisor-test",
        status="completed",
        sequence_key="media_buy:abc",
    )
    assert [c.sequence_number for c in sink.calls] == [1, 2]
    assert all(c.sequence_key == "media_buy:abc" for c in sink.calls)


# ----- Supervisor: sink failure isolation -----


@pytest.mark.asyncio
async def test_supervisor_swallows_sink_exceptions(caplog: pytest.LogCaptureFixture) -> None:
    """A broken sink must not break delivery."""
    sender = MagicMock()
    sender.send_mcp = AsyncMock(return_value=_ok())
    sup = _supervisor(sender, sink=_BrokenSink())

    result = await sup.send_mcp(
        url="https://buyer.example.com/wh",
        task_id="t1",
        task_type="create_media_buy",
        operation_id="op-supervisor-test",
        status="completed",
    )
    assert result is not None and result.ok


# ----- Protocol conformance -----


def test_in_memory_supervisor_satisfies_protocol() -> None:
    """Runtime structural check — the reference impl conforms to
    :class:`WebhookDeliverySupervisor`."""
    sup = _supervisor(MagicMock())
    assert isinstance(sup, WebhookDeliverySupervisor)


# ----- F12 wire-through: maybe_emit_sync_completion routes via supervisor -----


@pytest.mark.asyncio
async def test_maybe_emit_routes_through_supervisor_when_configured() -> None:
    """Neither target receives a synthetic sync-terminal webhook."""

    class _Cfg:
        url = "https://buyer.example.com/wh"
        token = None

    class _Params:
        push_notification_config = _Cfg()

    # Only the supervisor's send_mcp should be awaited.
    sender = MagicMock()
    sender.send_mcp = AsyncMock(return_value=_ok())
    supervisor = MagicMock()
    supervisor.send_mcp = AsyncMock(return_value=_ok())

    with pytest.warns(DeprecationWarning, match="ignored under AdCP 3.2"):
        maybe_emit_sync_completion(
            sender=sender,
            supervisor=supervisor,
            enabled=True,
            method_name="create_media_buy",
            params=_Params(),
            result={"media_buy_id": "mb_1"},
        )
    # Drain background tasks
    await asyncio.sleep(0)
    pending = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)

    assert supervisor.send_mcp.await_count == 0
    assert sender.send_mcp.await_count == 0


@pytest.mark.asyncio
async def test_maybe_emit_uses_sender_when_supervisor_none() -> None:
    """A bare sender also remains silent for an inline result."""

    class _Cfg:
        url = "https://buyer.example.com/wh"
        token = None

    class _Params:
        push_notification_config = _Cfg()

    sender = MagicMock()
    sender.send_mcp = AsyncMock(return_value=_ok())

    with pytest.warns(DeprecationWarning, match="ignored under AdCP 3.2"):
        maybe_emit_sync_completion(
            sender=sender,
            supervisor=None,
            enabled=True,
            method_name="create_media_buy",
            params=_Params(),
            result={"media_buy_id": "mb_1"},
        )
    await asyncio.sleep(0)
    pending = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)

    assert sender.send_mcp.await_count == 0


# ----- Boot validation: supervisor satisfies the gate -----


def test_validate_webhook_sender_passes_when_only_supervisor_wired() -> None:
    """Adopters can wire ONLY a supervisor (with the sender embedded inside)
    and the F12 boot gate must accept that."""
    validate_webhook_sender_for_platform(
        advertised_tools=frozenset({"create_media_buy"}),
        sender=None,
        supervisor=MagicMock(),  # any non-None
        auto_emit=True,
    )  # must not raise


def test_validate_webhook_sender_accepts_retired_flag_without_transport() -> None:
    validate_webhook_sender_for_platform(
        advertised_tools=frozenset({"create_media_buy"}),
        sender=None,
        supervisor=None,
        auto_emit=True,
    )


def test_validate_webhook_sender_passes_when_only_sender_wired() -> None:
    """Backward-compat: legacy adopters wire just a sender — gate
    accepts. Symmetric to the supervisor-only case above."""
    validate_webhook_sender_for_platform(
        advertised_tools=frozenset({"create_media_buy"}),
        sender=MagicMock(),
        supervisor=None,
        auto_emit=True,
    )  # must not raise


# ----- Constructor validation -----


def test_supervisor_init_rejects_none_sender() -> None:
    """Review #6: ``InMemoryWebhookDeliverySupervisor(None)`` would
    later AttributeError on every send. Must fail fast at __init__,
    matching the boot-time gate's fail-fast intent."""
    with pytest.raises(ValueError, match="non-None WebhookSender"):
        InMemoryWebhookDeliverySupervisor(None)  # type: ignore[arg-type]


# ----- breaker_key for cross-tenant isolation -----


@pytest.mark.asyncio
async def test_supervisor_breaker_key_isolates_tenants_on_shared_url() -> None:
    """Two tenants registering the same SaaS receiver URL must NOT
    share a circuit breaker. Tenant A's failures opening the breaker
    must not quarantine tenant B's deliveries to the same URL.

    Review finding M2: bare-URL keying is single-tenant only;
    multi-tenant adopters pass a ``breaker_key=f"{tenant}:{url}"``.
    """
    sender = MagicMock()
    sender.send_mcp = AsyncMock(return_value=_fail())
    sender.resend = AsyncMock(return_value=_fail())
    sup = _supervisor(
        sender,
        circuit=CircuitBreakerPolicy(failure_threshold=2, open_timeout_seconds=60),
    )

    # Tenant A trips the breaker for "tenant_a:https://shared/wh".
    await sup.send_mcp(
        url="https://shared/wh",
        task_id="t1",
        task_type="create_media_buy",
        operation_id="op-supervisor-test",
        status="completed",
        breaker_key="tenant_a:https://shared/wh",
    )
    # Tenant B uses the same URL but a different breaker_key —
    # should NOT be circuit-open (return None); should attempt and
    # fail like tenant A's first delivery.
    sender.send_mcp.reset_mock()
    sender.resend.reset_mock()
    result = await sup.send_mcp(
        url="https://shared/wh",
        task_id="t2",
        task_type="create_media_buy",
        operation_id="op-supervisor-test",
        status="completed",
        breaker_key="tenant_b:https://shared/wh",
    )
    # Tenant B made it through to attempt delivery (not circuit_open).
    assert result is not None
    assert sender.send_mcp.await_count == 1


@pytest.mark.asyncio
async def test_supervisor_breaker_key_defaults_to_url() -> None:
    """When no breaker_key is provided, falls back to URL — preserves
    the v1 default and the JS-parity contract."""
    sender = MagicMock()
    sender.send_mcp = AsyncMock(return_value=_fail())
    sender.resend = AsyncMock(return_value=_fail())
    sup = _supervisor(
        sender,
        circuit=CircuitBreakerPolicy(failure_threshold=2, open_timeout_seconds=60),
    )

    await sup.send_mcp(
        url="https://shared/wh",
        task_id="t1",
        status="completed",
        task_type="create_media_buy",
        operation_id="op-supervisor-test",
    )
    # Same URL → same breaker → second delivery is circuit_open.
    result = await sup.send_mcp(
        url="https://shared/wh",
        task_id="t2",
        status="completed",
        task_type="create_media_buy",
        operation_id="op-supervisor-test",
    )
    assert result is None


# ----- Sequence number after breaker check -----


@pytest.mark.asyncio
async def test_supervisor_does_not_burn_sequence_on_circuit_open() -> None:
    """Review #5: sequence number must NOT be allocated when the
    breaker is OPEN. Burning numbers on circuit-open creates gaps in
    the buyer-facing sequence stream."""
    sender = MagicMock()
    sender.send_mcp = AsyncMock(return_value=_fail())
    sender.resend = AsyncMock(return_value=_fail())
    sup = _supervisor(
        sender,
        circuit=CircuitBreakerPolicy(failure_threshold=2, open_timeout_seconds=60),
    )

    # First delivery: opens breaker; allocates sequence_number=1
    await sup.send_mcp(
        url="https://buyer.example.com/wh",
        task_id="t1",
        task_type="create_media_buy",
        operation_id="op-supervisor-test",
        status="completed",
        sequence_key="media_buy:abc:https://buyer.example.com/wh",
    )
    # Second delivery: circuit_open, MUST NOT allocate a sequence.
    await sup.send_mcp(
        url="https://buyer.example.com/wh",
        task_id="t2",
        task_type="create_media_buy",
        operation_id="op-supervisor-test",
        status="completed",
        sequence_key="media_buy:abc:https://buyer.example.com/wh",
    )
    # Third delivery: still circuit_open if breaker hasn't recovered.
    # When breaker recovers and a delivery succeeds, the next
    # sequence number should be 2 (not 3 — circuit-open didn't burn).
    assert (
        sup.next_sequence("media_buy:abc:https://buyer.example.com/wh") == 2
    ), "sequence skipped circuit-open burns"


# ----- Sink timeout -----


@pytest.mark.asyncio
async def test_supervisor_bounds_slow_sink_with_timeout() -> None:
    """Review M1: a slow sink must NOT freeze the supervisor. The
    sink timeout (default 5s, configurable via
    ``RetryPolicy.sink_timeout_seconds``) bounds wait time."""
    import time as _time

    @dataclass
    class _SlowSink:
        async def record(self, attempt: DeliveryAttempt) -> None:
            await asyncio.sleep(10.0)  # would freeze without timeout

    sender = MagicMock()
    sender.send_mcp = AsyncMock(return_value=_ok())
    sender.resend = AsyncMock(return_value=_ok())
    sup = InMemoryWebhookDeliverySupervisor(
        sender,
        retry=RetryPolicy(
            max_attempts=1,
            base_delay_seconds=0.0,
            jitter=False,
            sink_timeout_seconds=0.05,  # 50ms — well under the sink's 10s
        ),
        log_sink=_SlowSink(),
    )

    started = _time.monotonic()
    result = await sup.send_mcp(
        url="https://buyer.example.com/wh",
        task_id="t1",
        task_type="create_media_buy",
        operation_id="op-supervisor-test",
        status="completed",
    )
    elapsed = _time.monotonic() - started
    # Delivery succeeded despite sink timeout.
    assert result is not None and result.ok
    # Sink shouldn't have blocked the supervisor for the full 10s.
    assert elapsed < 1.0, f"sink timeout didn't bound wait; elapsed={elapsed}"


# ----- Notification-type passthrough -----


@pytest.mark.asyncio
async def test_supervisor_records_notification_type_passthrough() -> None:
    """Review #9: ``notification_type`` (delivery-report concept —
    'scheduled' / 'final' / 'adjusted' / 'delayed' / 'window_update')
    flows through to ``DeliveryAttempt`` for adopter persistence."""
    sender = MagicMock()
    sender.send_mcp = AsyncMock(return_value=_ok())
    sender.resend = AsyncMock(return_value=_ok())
    sink = _RecordingSink()
    sup = _supervisor(sender, sink=sink)

    await sup.send_mcp(
        url="https://buyer.example.com/wh",
        task_id="t1",
        task_type="create_media_buy",
        operation_id="op-supervisor-test",
        status="completed",
        notification_type="scheduled",
    )
    assert sink.calls[0].notification_type == "scheduled"


# ----- Monotonic clock for response_time_ms -----


@pytest.mark.asyncio
async def test_supervisor_response_time_uses_monotonic_clock() -> None:
    """Review #7: ``response_time_ms`` must use ``time.monotonic()``
    to be NTP-step-resilient. Sanity-check by asserting it's a
    non-negative integer (deeper testing would mock time.monotonic)."""
    sender = MagicMock()

    async def _slow_send(**_: Any) -> WebhookDeliveryResult:
        await asyncio.sleep(0.02)
        return _ok()

    sender.send_mcp = AsyncMock(side_effect=_slow_send)
    sender.resend = AsyncMock(side_effect=_slow_send)
    sink = _RecordingSink()
    sup = _supervisor(sender, sink=sink)

    await sup.send_mcp(
        url="https://buyer.example.com/wh",
        task_id="t1",
        task_type="create_media_buy",
        operation_id="op-supervisor-test",
        status="completed",
    )
    assert sink.calls[0].response_time_ms >= 10  # at least 10ms (we slept 20)
    assert sink.calls[0].response_time_ms < 1000  # not absurd
