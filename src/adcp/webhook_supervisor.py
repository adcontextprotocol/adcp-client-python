"""Webhook delivery supervisor — retry, circuit breaker, attempt audit.

:class:`~adcp.webhook_sender.WebhookSender` is the *transport* layer:
HTTP-Signatures-aware POST, one attempt, returns a result. It does not
retry, does not isolate failing endpoints, and does not record attempts.

Production sellers wrap that transport with reliability logic — the
salesagent reference adopter ships ~1,000 LOC of circuit breaker,
exponential backoff, and persisted delivery log around it. This module
is the SDK home for that pattern so adopters don't have to write it
from scratch.

Two seams:

* :class:`WebhookDeliverySupervisor` — Protocol. Adopters with their
  own infra-side retry (Celery, Kafka, durable outbox) implement this
  against their queue and pass it to
  :func:`adcp.decisioning.serve.serve` instead of (or alongside) a
  bare ``WebhookSender``.

* :class:`InMemoryWebhookDeliverySupervisor` — reference impl. Wraps a
  :class:`WebhookSender` with per-endpoint :class:`CircuitBreaker` and
  retry-with-jitter. Single-process, in-memory state — adequate for
  single-instance servers; multi-instance deployments that need shared
  breaker state should implement the Protocol against Redis or
  similar.

Audit trail: every attempt produces a :class:`DeliveryAttempt` record
that an optional :class:`DeliveryLogSink` can persist. Adopters with an
existing ``webhook_delivery_log`` table wire a sink that maps the
record to their schema; adopters without one can ignore the seam.

Sequence numbers: the supervisor maintains a per-``sequence_key``
monotonic counter so adopters that need it (e.g., delivery report
webhooks where the buyer correlates by sequence #) can request one via
:meth:`InMemoryWebhookDeliverySupervisor.next_sequence`. Sellers who
need durable cross-restart sequence numbers implement the Protocol
against their persistence layer.
"""

from __future__ import annotations

import asyncio
import logging
import random
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

UTC = timezone.utc
from enum import Enum
from typing import TYPE_CHECKING, Any, Literal, Protocol, runtime_checkable

if TYPE_CHECKING:
    from adcp.types import GeneratedTaskStatus
    from adcp.webhook_sender import WebhookDeliveryResult, WebhookSender

logger = logging.getLogger(__name__)


# ----- Policy dataclasses -----


@dataclass(frozen=True)
class RetryPolicy:
    """Retry behavior for a single :meth:`send_mcp` call.

    Defaults match the salesagent reference adopter (3 attempts,
    exponential backoff with jitter, 1s base, 30s cap). Adopters with
    different SLAs override per-supervisor.
    """

    max_attempts: int = 3
    base_delay_seconds: float = 1.0
    max_delay_seconds: float = 30.0
    jitter: bool = True

    def delay_for_attempt(self, attempt_number: int) -> float:
        """Compute the sleep before ``attempt_number`` (1-indexed).

        Attempt 1 has no preceding delay; attempts 2+ use exponential
        backoff capped at ``max_delay_seconds`` with optional jitter.
        """
        if attempt_number <= 1:
            return 0.0
        delay: float = min(
            self.base_delay_seconds * (2 ** (attempt_number - 2)),
            self.max_delay_seconds,
        )
        if self.jitter:
            delay = delay * (0.5 + random.random() * 0.5)
        return delay


@dataclass(frozen=True)
class CircuitBreakerPolicy:
    """Per-endpoint circuit breaker tuning.

    Defaults match the salesagent reference (5 consecutive failures
    open, 60s recovery test, 2 successes close).
    """

    failure_threshold: int = 5
    success_threshold: int = 2
    open_timeout_seconds: float = 60.0


# ----- Attempt record + sink -----


class CircuitState(Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


AttemptOutcome = Literal["success", "failure", "circuit_open"]


@dataclass(frozen=True)
class DeliveryAttempt:
    """One record passed to :class:`DeliveryLogSink` per delivery attempt.

    Sellers wire a sink that maps these to their existing
    ``webhook_delivery_log`` schema. The supervisor itself doesn't
    persist anything — the sink is the BYO storage seam.
    """

    url: str
    sequence_key: str | None
    sequence_number: int | None
    attempt_number: int
    max_attempts: int
    outcome: AttemptOutcome
    http_status_code: int | None
    error_message: str | None
    response_time_ms: int
    occurred_at: datetime
    will_retry: bool
    next_retry_at: datetime | None
    task_type: str | None
    task_id: str | None
    payload_size_bytes: int | None


@runtime_checkable
class DeliveryLogSink(Protocol):
    """Optional persistence hook for delivery attempts.

    Called once per attempt. Failures inside ``record`` are
    logged-and-swallowed by the supervisor — a broken sink must not
    cascade into webhook delivery loss.

    Adopters typically wire this to a ``webhook_delivery_log`` table in
    their existing schema. No-op implementations are valid for adopters
    who don't need attempt-level audit.
    """

    async def record(self, attempt: DeliveryAttempt) -> None: ...


# ----- Supervisor Protocol -----


@runtime_checkable
class WebhookDeliverySupervisor(Protocol):
    """Reliable webhook delivery surface.

    Conforms to the ``send_mcp`` shape used by F12 sync-completion
    webhooks. Adopters that route deliveries through a durable queue
    (Celery, Kafka, outbox-pattern) implement this Protocol against
    their queue's enqueue API; the SDK's call site is identical.

    The reference :class:`InMemoryWebhookDeliverySupervisor` wraps a
    :class:`~adcp.webhook_sender.WebhookSender` with circuit breaker +
    retry; sellers with their own infra implement the Protocol
    directly without using the reference.
    """

    async def send_mcp(
        self,
        *,
        url: str,
        task_id: str,
        status: GeneratedTaskStatus | str,
        task_type: str | None = None,
        result: Any = None,
        token: str | None = None,
        sequence_key: str | None = None,
    ) -> WebhookDeliveryResult | None: ...


# ----- Reference implementation -----


class _CircuitBreaker:
    """Per-endpoint breaker. Internal — sellers tune via :class:`CircuitBreakerPolicy`."""

    def __init__(self, policy: CircuitBreakerPolicy) -> None:
        self._policy = policy
        self._state = CircuitState.CLOSED
        self._failure_count = 0
        self._success_count = 0
        self._opened_at: datetime | None = None
        self._lock = threading.Lock()

    def can_attempt(self) -> bool:
        with self._lock:
            if self._state is CircuitState.CLOSED:
                return True
            if self._state is CircuitState.OPEN:
                if self._opened_at is None:
                    return True
                elapsed = (datetime.now(UTC) - self._opened_at).total_seconds()
                if elapsed >= self._policy.open_timeout_seconds:
                    self._state = CircuitState.HALF_OPEN
                    self._success_count = 0
                    return True
                return False
            return True  # HALF_OPEN allows one test attempt at a time

    def record_success(self) -> None:
        with self._lock:
            self._failure_count = 0
            if self._state is CircuitState.HALF_OPEN:
                self._success_count += 1
                if self._success_count >= self._policy.success_threshold:
                    self._state = CircuitState.CLOSED
                    self._success_count = 0
            elif self._state is CircuitState.OPEN:
                # Defensive: a success while OPEN means the breaker raced
                # past its timeout. Reset cleanly.
                self._state = CircuitState.CLOSED

    def record_failure(self) -> None:
        with self._lock:
            self._failure_count += 1
            if self._state is CircuitState.HALF_OPEN:
                self._state = CircuitState.OPEN
                self._opened_at = datetime.now(UTC)
                self._success_count = 0
            elif (
                self._state is CircuitState.CLOSED
                and self._failure_count >= self._policy.failure_threshold
            ):
                self._state = CircuitState.OPEN
                self._opened_at = datetime.now(UTC)

    @property
    def state(self) -> CircuitState:
        return self._state


class InMemoryWebhookDeliverySupervisor:
    """Reference :class:`WebhookDeliverySupervisor` with in-process state.

    Single-instance — circuit breaker counters live in the running
    process. Multi-instance deployments that need shared breaker state
    implement the Protocol against a distributed cache (Redis,
    Memcached) instead of using this class.

    Wraps an underlying :class:`~adcp.webhook_sender.WebhookSender` for
    actual HTTP-Signatures send. Per-endpoint state isolation: each
    URL gets its own breaker so one buyer's outage doesn't quarantine
    another.

    Sequence numbers: per-``sequence_key`` monotonic counters live in
    memory. Sellers that need cross-restart sequence numbers (e.g., to
    persist delivery report sequence # in their database) implement the
    Protocol directly against their persistence layer instead.
    """

    def __init__(
        self,
        sender: WebhookSender,
        *,
        retry: RetryPolicy | None = None,
        circuit: CircuitBreakerPolicy | None = None,
        log_sink: DeliveryLogSink | None = None,
    ) -> None:
        self._sender = sender
        self._retry = retry or RetryPolicy()
        self._circuit_policy = circuit or CircuitBreakerPolicy()
        self._log_sink = log_sink
        self._breakers: dict[str, _CircuitBreaker] = {}
        self._sequence_numbers: dict[str, int] = {}
        self._state_lock = threading.Lock()

    def next_sequence(self, key: str) -> int:
        """Allocate the next sequence number for ``key`` (1-indexed).

        Sellers who manage sequence numbers themselves (e.g., persisted
        in a database column) ignore this and pass their own value via
        wherever it's used downstream.
        """
        with self._state_lock:
            current = self._sequence_numbers.get(key, 0) + 1
            self._sequence_numbers[key] = current
            return current

    def _breaker_for(self, url: str) -> _CircuitBreaker:
        with self._state_lock:
            breaker = self._breakers.get(url)
            if breaker is None:
                breaker = _CircuitBreaker(self._circuit_policy)
                self._breakers[url] = breaker
            return breaker

    async def send_mcp(
        self,
        *,
        url: str,
        task_id: str,
        status: GeneratedTaskStatus | str,
        task_type: str | None = None,
        result: Any = None,
        token: str | None = None,
        sequence_key: str | None = None,
    ) -> WebhookDeliveryResult | None:
        """Deliver one MCP-style webhook with retry + circuit-breaker.

        Returns the final attempt's :class:`WebhookDeliveryResult` on
        success or the last failed attempt; returns ``None`` when the
        breaker is OPEN and no attempt is made (the audit log records
        a ``circuit_open`` row regardless).

        Each attempt is logged to the :class:`DeliveryLogSink` if one
        is configured. Sink failures are swallowed.
        """
        breaker = self._breaker_for(url)
        sequence_number = self.next_sequence(sequence_key) if sequence_key is not None else None

        if not breaker.can_attempt():
            await self._record(
                DeliveryAttempt(
                    url=url,
                    sequence_key=sequence_key,
                    sequence_number=sequence_number,
                    attempt_number=0,
                    max_attempts=self._retry.max_attempts,
                    outcome="circuit_open",
                    http_status_code=None,
                    error_message=(f"circuit breaker OPEN for {url} — skipped delivery"),
                    response_time_ms=0,
                    occurred_at=datetime.now(UTC),
                    will_retry=False,
                    next_retry_at=None,
                    task_type=task_type,
                    task_id=task_id,
                    payload_size_bytes=None,
                )
            )
            logger.warning(
                "[adcp.webhook_supervisor] circuit OPEN for %s — skipped %s",
                url,
                task_type or "webhook",
            )
            return None

        last_result: WebhookDeliveryResult | None = None
        for attempt_number in range(1, self._retry.max_attempts + 1):
            delay = self._retry.delay_for_attempt(attempt_number)
            if delay > 0:
                await asyncio.sleep(delay)

            attempt_started = datetime.now(UTC)
            try:
                last_result = await self._sender.send_mcp(
                    url=url,
                    task_id=task_id,
                    status=status,
                    task_type=task_type,
                    result=result,
                    token=token,
                )
                response_time_ms = int((datetime.now(UTC) - attempt_started).total_seconds() * 1000)
                if last_result.ok:
                    breaker.record_success()
                    await self._record(
                        DeliveryAttempt(
                            url=url,
                            sequence_key=sequence_key,
                            sequence_number=sequence_number,
                            attempt_number=attempt_number,
                            max_attempts=self._retry.max_attempts,
                            outcome="success",
                            http_status_code=last_result.status_code,
                            error_message=None,
                            response_time_ms=response_time_ms,
                            occurred_at=attempt_started,
                            will_retry=False,
                            next_retry_at=None,
                            task_type=task_type,
                            task_id=task_id,
                            payload_size_bytes=len(last_result.sent_body),
                        )
                    )
                    return last_result
                # Non-2xx — count as failure for breaker, retry
                will_retry = attempt_number < self._retry.max_attempts
                next_delay = (
                    self._retry.delay_for_attempt(attempt_number + 1) if will_retry else None
                )
                next_retry_at = (
                    attempt_started + timedelta(seconds=next_delay)
                    if next_delay is not None
                    else None
                )
                breaker.record_failure()
                await self._record(
                    DeliveryAttempt(
                        url=url,
                        sequence_key=sequence_key,
                        sequence_number=sequence_number,
                        attempt_number=attempt_number,
                        max_attempts=self._retry.max_attempts,
                        outcome="failure",
                        http_status_code=last_result.status_code,
                        error_message=(
                            f"HTTP {last_result.status_code}: "
                            f"{last_result.response_body[:200]!r}"
                        ),
                        response_time_ms=response_time_ms,
                        occurred_at=attempt_started,
                        will_retry=will_retry,
                        next_retry_at=next_retry_at,
                        task_type=task_type,
                        task_id=task_id,
                        payload_size_bytes=len(last_result.sent_body),
                    )
                )
            except Exception as exc:
                response_time_ms = int((datetime.now(UTC) - attempt_started).total_seconds() * 1000)
                will_retry = attempt_number < self._retry.max_attempts
                next_delay = (
                    self._retry.delay_for_attempt(attempt_number + 1) if will_retry else None
                )
                next_retry_at = (
                    attempt_started + timedelta(seconds=next_delay)
                    if next_delay is not None
                    else None
                )
                breaker.record_failure()
                await self._record(
                    DeliveryAttempt(
                        url=url,
                        sequence_key=sequence_key,
                        sequence_number=sequence_number,
                        attempt_number=attempt_number,
                        max_attempts=self._retry.max_attempts,
                        outcome="failure",
                        http_status_code=None,
                        error_message=f"{type(exc).__name__}: {exc}",
                        response_time_ms=response_time_ms,
                        occurred_at=attempt_started,
                        will_retry=will_retry,
                        next_retry_at=next_retry_at,
                        task_type=task_type,
                        task_id=task_id,
                        payload_size_bytes=None,
                    )
                )
                if not will_retry:
                    raise

        return last_result

    async def _record(self, attempt: DeliveryAttempt) -> None:
        if self._log_sink is None:
            return
        try:
            await self._log_sink.record(attempt)
        except Exception:
            logger.warning(
                "[adcp.webhook_supervisor] DeliveryLogSink raised on attempt "
                "for %s — log dropped, delivery unaffected",
                attempt.url,
                exc_info=True,
            )


# ----- Helpers -----


def supervisor_or_sender(
    *,
    sender: WebhookSender | None,
    supervisor: WebhookDeliverySupervisor | None,
) -> WebhookDeliverySupervisor | WebhookSender | None:
    """Resolve the F12-callsite delivery target.

    Supervisor takes precedence when both are passed. Either alone is
    valid; both ``None`` opts out of auto-emit. Used by
    :mod:`adcp.decisioning.webhook_emit` to keep the call site
    polymorphic over the two surfaces (both expose ``send_mcp`` with
    compatible signatures).
    """
    return supervisor or sender


__all__ = [
    "AttemptOutcome",
    "CircuitBreakerPolicy",
    "CircuitState",
    "DeliveryAttempt",
    "DeliveryLogSink",
    "InMemoryWebhookDeliverySupervisor",
    "RetryPolicy",
    "WebhookDeliverySupervisor",
    "supervisor_or_sender",
]
