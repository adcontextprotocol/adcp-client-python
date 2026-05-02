"""PostgreSQL-backed :class:`WebhookDeliverySupervisor` for multi-worker durability.

Gives multi-instance AdCP sellers a shared circuit-breaker state and a
durable retry queue so webhook deliveries survive process crashes and are
never double-sent across concurrent workers.

.. rubric:: REQUIRED: run_worker()

Unlike :class:`~adcp.webhook_supervisor.InMemoryWebhookDeliverySupervisor`,
:meth:`PgWebhookDeliverySupervisor.send_mcp` does **not** deliver
immediately — it enqueues the job and returns ``None``. You **MUST** start
:meth:`run_worker` somewhere in your application; without it enqueued jobs
accumulate and are never sent:

::

    from psycopg_pool import AsyncConnectionPool
    from adcp.webhook_supervisor_pg import PgWebhookDeliverySupervisor

    pool = AsyncConnectionPool("postgresql://...", min_size=4, max_size=20)
    supervisor = PgWebhookDeliverySupervisor(pool=pool, sender=my_sender)
    await supervisor.create_schema()          # idempotent; call once per boot

    asyncio.create_task(supervisor.run_worker())   # REQUIRED before serve()

    serve(my_handler, webhook_supervisor=supervisor)

.. rubric:: Observability

``send_mcp`` always returns ``None``. For per-attempt outcomes configure a
:class:`~adcp.webhook_supervisor.DeliveryLogSink` or query the
``adcp_webhook_delivery_log`` table directly.

.. rubric:: Cross-process guarantees

* **Circuit breaker state** — shared via ``adcp_webhook_circuit_state`` (one
  row per *breaker_key*). Half-open probe atomicity is enforced via
  ``UPDATE … RETURNING`` so two concurrent workers can't both transition to
  ``closed`` from a single success.
* **Sequence numbers** — the ``BIGSERIAL`` queue-row id is the sequence
  number (monotonically increasing, crash-durable across restarts).
* **Worker leasing** — ``SELECT … FOR UPDATE SKIP LOCKED LIMIT 1`` prevents
  double-delivery when multiple workers poll the same queue. The lock is
  held for the duration of the HTTP send; a crashed worker automatically
  releases the job (transaction rollback).

.. rubric:: DDL for migration tools

::

    -- adcp_webhook_circuit_state
    CREATE TABLE adcp_webhook_circuit_state (
        breaker_key   TEXT        COLLATE "C" NOT NULL PRIMARY KEY,
        state         TEXT        NOT NULL DEFAULT 'closed',
        failure_count INT         NOT NULL DEFAULT 0,
        success_count INT         NOT NULL DEFAULT 0,
        opened_at     TIMESTAMPTZ,
        updated_at    TIMESTAMPTZ NOT NULL DEFAULT now()
    );

    -- adcp_webhook_delivery_queue
    CREATE TABLE adcp_webhook_delivery_queue (
        id              BIGSERIAL   PRIMARY KEY,
        breaker_key     TEXT        NOT NULL,
        url             TEXT        NOT NULL,
        task_id         TEXT        NOT NULL,
        task_type       TEXT,
        status_str      TEXT        NOT NULL DEFAULT 'pending',
        result_json     TEXT,
        token           TEXT,
        sequence_key    TEXT,
        attempt_count   INT         NOT NULL DEFAULT 0,
        max_attempts    INT         NOT NULL DEFAULT 3,
        scheduled_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
        created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
        idempotency_key TEXT,
        sent_body       BYTEA,
        notification_type TEXT
    );
    CREATE INDEX adcp_webhook_delivery_queue_work_idx
        ON adcp_webhook_delivery_queue (status_str, scheduled_at)
        WHERE status_str IN ('pending', 'retry');

    -- adcp_webhook_delivery_log
    CREATE TABLE adcp_webhook_delivery_log (
        id                BIGSERIAL   PRIMARY KEY,
        queue_id          BIGINT,
        breaker_key       TEXT        NOT NULL,
        url               TEXT        NOT NULL,
        task_id           TEXT        NOT NULL,
        sequence_key      TEXT,
        sequence_number   BIGINT,
        attempt_number    INT         NOT NULL,
        max_attempts      INT         NOT NULL,
        outcome           TEXT        NOT NULL,
        http_status_code  INT,
        error_message     TEXT,
        response_time_ms  INT         NOT NULL DEFAULT 0,
        occurred_at       TIMESTAMPTZ NOT NULL,
        will_retry        BOOLEAN     NOT NULL DEFAULT false,
        next_retry_at     TIMESTAMPTZ,
        task_type         TEXT,
        payload_size_bytes INT,
        notification_type TEXT
    );
    CREATE INDEX adcp_webhook_delivery_log_queue_id_idx
        ON adcp_webhook_delivery_log (queue_id);
    CREATE INDEX adcp_webhook_delivery_log_task_id_idx
        ON adcp_webhook_delivery_log (task_id);

.. rubric:: Stranded-job note

Jobs remain in ``status_str = 'pending'`` (via rollback) if a worker
process crashes mid-send. No separate recovery sweep is needed — the next
worker poll picks them up automatically. Jobs that raised a Python exception
on the final attempt are deleted from the queue and logged with
``outcome = 'failure'``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from adcp.types import GeneratedTaskStatus
    from adcp.webhook_sender import WebhookDeliveryResult, WebhookSender
    from adcp.webhook_supervisor import DeliveryLogSink

try:
    import psycopg  # noqa: F401
    import psycopg_pool  # noqa: F401

    PG_AVAILABLE = True
except ImportError:
    PG_AVAILABLE = False

from adcp.webhook_supervisor import (
    CircuitBreakerPolicy,
    DeliveryAttempt,
    RetryPolicy,
)

UTC = timezone.utc
logger = logging.getLogger(__name__)

# Byte-level ASCII identifier guard — same rationale as PgReplayStore.
# str.islower() accepts non-ASCII Unicode letters (é, µ, etc.) which would
# format verbatim into SQL as a different table than configured.
_SAFE_IDENTIFIER_RE = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")

_INSTALL_HINT = (
    "PgWebhookDeliverySupervisor requires psycopg3 and psycopg-pool. "
    "Install the 'pg' extra: `pip install 'adcp[pg]'` "
    "(Poetry: `poetry add 'adcp[pg]'`)."
)

DEFAULT_CIRCUIT_TABLE = "adcp_webhook_circuit_state"
DEFAULT_QUEUE_TABLE = "adcp_webhook_delivery_queue"
DEFAULT_LOG_TABLE = "adcp_webhook_delivery_log"


def _safe_identifier(name: str) -> str:
    if not _SAFE_IDENTIFIER_RE.fullmatch(name):
        raise ValueError(
            f"Table name must match [a-z_][a-z0-9_]{{0,62}} (ASCII only), got {name!r}"
        )
    return name


class PgWebhookDeliverySupervisor:
    """Postgres-backed :class:`~adcp.webhook_supervisor.WebhookDeliverySupervisor`.

    Parameters
    ----------
    pool:
        A ``psycopg_pool.AsyncConnectionPool`` owned by the caller. Each
        operation acquires a short-lived connection (or holds one for the
        duration of a delivery). We don't open, own, or close the pool.
    sender:
        The underlying :class:`~adcp.webhook_sender.WebhookSender` used for
        the actual HTTP-Signatures POST. Must be non-None.
    retry:
        Retry policy (default: 3 attempts, exponential backoff with jitter).
    circuit:
        Circuit-breaker tuning (default: 5 failures open, 60s recovery,
        2 successes close).
    log_sink:
        Optional :class:`~adcp.webhook_supervisor.DeliveryLogSink` called
        after each attempt. Failures are swallowed. Use for custom
        observability pipelines in addition to the built-in log table.
    circuit_table / queue_table / log_table:
        Override table names (ASCII only, 1–63 chars). Useful for multi-
        tenant schemas. Defaults are ``adcp_webhook_circuit_state``,
        ``adcp_webhook_delivery_queue``, and ``adcp_webhook_delivery_log``.

    Concurrency
    -----------
    Safe to share across asyncio tasks in a single process. Multiple
    processes/pods each running :meth:`run_worker` are explicitly supported
    via ``FOR UPDATE SKIP LOCKED``.
    """

    def __init__(
        self,
        pool: Any,  # psycopg_pool.AsyncConnectionPool; Any avoids import at runtime
        sender: WebhookSender,
        *,
        retry: RetryPolicy | None = None,
        circuit: CircuitBreakerPolicy | None = None,
        log_sink: DeliveryLogSink | None = None,
        circuit_table: str = DEFAULT_CIRCUIT_TABLE,
        queue_table: str = DEFAULT_QUEUE_TABLE,
        log_table: str = DEFAULT_LOG_TABLE,
    ) -> None:
        if not PG_AVAILABLE:
            raise ImportError(_INSTALL_HINT)
        if sender is None:
            raise ValueError(
                "PgWebhookDeliverySupervisor requires a non-None WebhookSender. "
                "Construct one via WebhookSender.from_jwk(...) or "
                "WebhookSender.from_pem(...) and pass it as the first positional argument."
            )
        self._pool = pool
        self._sender = sender
        self._retry = retry or RetryPolicy()
        self._circuit_policy = circuit or CircuitBreakerPolicy()
        self._log_sink = log_sink

        ct = _safe_identifier(circuit_table)
        qt = _safe_identifier(queue_table)
        lt = _safe_identifier(log_table)
        self._circuit_t = ct
        self._queue_t = qt
        self._log_t = lt

        # Pre-format SQL at construction time to avoid per-call f-string overhead
        # and to bake in the validated table names once.
        self._sql_circuit_get = (
            f"SELECT state, opened_at FROM {ct} WHERE breaker_key = %s"  # noqa: S608
        )
        self._sql_circuit_set_half_open = (
            f"UPDATE {ct} SET state = 'half_open', success_count = 0, "  # noqa: S608
            f"updated_at = now() WHERE breaker_key = %s AND state = 'open'"
        )
        # Atomic upsert: increment failure_count; open circuit when threshold crossed.
        # {ct}.column_name in DO UPDATE SET refers to the *existing* row's value
        # (before the update), which is what Postgres requires for self-referential
        # ON CONFLICT expressions.
        # Coerce thresholds via ``int()`` before f-string interpolation —
        # an adopter passing an ``IntEnum`` / ``bool`` / custom ``__index__``
        # subclass for ``failure_threshold`` would format verbatim into
        # SQL otherwise. Coercion produces a plain int whose ``str()``
        # is byte-safe for static SQL emission.
        failure_t = int(self._circuit_policy.failure_threshold)
        self._sql_circuit_failure = (
            f"INSERT INTO {ct} "  # noqa: S608
            f"(breaker_key, state, failure_count, success_count, updated_at) "
            f"VALUES (%s, 'closed', 1, 0, now()) "
            f"ON CONFLICT (breaker_key) DO UPDATE SET "
            f"  failure_count = {ct}.failure_count + 1, "
            f"  success_count = 0, "
            f"  state = CASE "
            f"    WHEN {ct}.failure_count + 1 >= {failure_t} OR {ct}.state = 'half_open' "
            f"    THEN 'open' ELSE {ct}.state END, "
            f"  opened_at = CASE "
            f"    WHEN ({ct}.failure_count + 1 >= {failure_t} OR {ct}.state = 'half_open') "
            f"      AND {ct}.state != 'open' THEN now() "
            f"    ELSE {ct}.opened_at END, "
            f"  updated_at = now()"
        )
        # Atomic upsert: increment success_count; close circuit when threshold crossed.
        # RETURNING lets us read the post-update state without a second query, which
        # eliminates the half-open race: two concurrent workers see different final
        # counts and only the worker whose UPDATE produces count >= threshold
        # transitions to 'closed'.
        success_t = int(self._circuit_policy.success_threshold)
        self._sql_circuit_success = (
            f"INSERT INTO {ct} "  # noqa: S608
            f"(breaker_key, state, failure_count, success_count, updated_at) "
            f"VALUES (%s, 'closed', 0, 0, now()) "
            f"ON CONFLICT (breaker_key) DO UPDATE SET "
            f"  failure_count = 0, "
            f"  success_count = CASE "
            f"    WHEN {ct}.state = 'half_open' THEN {ct}.success_count + 1 ELSE 0 END, "
            f"  state = CASE "
            f"    WHEN {ct}.state = 'half_open' AND {ct}.success_count + 1 >= {success_t} "
            f"    THEN 'closed' ELSE {ct}.state END, "
            f"  updated_at = now() "
            f"RETURNING state, success_count"
        )
        self._sql_enqueue = (
            f"INSERT INTO {qt} "  # noqa: S608
            f"(breaker_key, url, task_id, task_type, status_str, result_json, "
            f"token, sequence_key, max_attempts, notification_type) "
            f"VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id"
        )
        self._sql_poll = (
            f"SELECT id, breaker_key, url, task_id, task_type, status_str, "  # noqa: S608
            f"result_json, token, sequence_key, attempt_count, max_attempts, "
            f"idempotency_key, sent_body, notification_type "
            f"FROM {qt} "
            f"WHERE status_str IN ('pending', 'retry') AND scheduled_at <= now() "
            f"ORDER BY scheduled_at LIMIT 1 FOR UPDATE SKIP LOCKED"
        )
        self._sql_delete_job = f"DELETE FROM {qt} WHERE id = %s"  # noqa: S608
        self._sql_reschedule = (
            f"UPDATE {qt} SET "  # noqa: S608
            f"  status_str = 'retry', "
            f"  attempt_count = %s, "
            f"  scheduled_at = %s, "
            f"  sent_body = %s, "
            f"  idempotency_key = %s "
            f"WHERE id = %s"
        )
        self._sql_log_insert = (
            f"INSERT INTO {lt} "  # noqa: S608
            f"(queue_id, breaker_key, url, task_id, sequence_key, sequence_number, "
            f"attempt_number, max_attempts, outcome, http_status_code, error_message, "
            f"response_time_ms, occurred_at, will_retry, next_retry_at, task_type, "
            f"payload_size_bytes, notification_type) "
            f"VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)"
        )

        self._worker_started = False
        self._worker_warned = False

    # ------------------------------------------------------------------ schema

    async def create_schema(self) -> None:
        """Bootstrap all three tables and their indexes. Idempotent.

        Safe to call on every app boot. Each DDL statement is executed
        separately — psycopg does not split on ``;``.

        ::

            pool = AsyncConnectionPool("postgresql://...")
            supervisor = PgWebhookDeliverySupervisor(pool=pool, sender=sender)
            await supervisor.create_schema()
        """
        ct, qt, lt = self._circuit_t, self._queue_t, self._log_t
        statements = [
            f"""CREATE TABLE IF NOT EXISTS {ct} (
                breaker_key   TEXT        COLLATE "C" NOT NULL PRIMARY KEY,
                state         TEXT        NOT NULL DEFAULT 'closed',
                failure_count INT         NOT NULL DEFAULT 0,
                success_count INT         NOT NULL DEFAULT 0,
                opened_at     TIMESTAMPTZ,
                updated_at    TIMESTAMPTZ NOT NULL DEFAULT now()
            )""",
            f"""CREATE TABLE IF NOT EXISTS {qt} (
                id              BIGSERIAL   PRIMARY KEY,
                breaker_key     TEXT        NOT NULL,
                url             TEXT        NOT NULL,
                task_id         TEXT        NOT NULL,
                task_type       TEXT,
                status_str      TEXT        NOT NULL DEFAULT 'pending',
                result_json     TEXT,
                token           TEXT,
                sequence_key    TEXT,
                attempt_count   INT         NOT NULL DEFAULT 0,
                max_attempts    INT         NOT NULL DEFAULT 3,
                scheduled_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
                created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
                idempotency_key TEXT,
                sent_body       BYTEA,
                notification_type TEXT
            )""",
            # Partial index on work-eligible rows; avoids scanning completed/in-flight rows.
            f"""CREATE INDEX IF NOT EXISTS {qt}_work_idx
                ON {qt} (status_str, scheduled_at)
                WHERE status_str IN ('pending', 'retry')""",
            f"""CREATE TABLE IF NOT EXISTS {lt} (
                id                BIGSERIAL   PRIMARY KEY,
                queue_id          BIGINT,
                breaker_key       TEXT        NOT NULL,
                url               TEXT        NOT NULL,
                task_id           TEXT        NOT NULL,
                sequence_key      TEXT,
                sequence_number   BIGINT,
                attempt_number    INT         NOT NULL,
                max_attempts      INT         NOT NULL,
                outcome           TEXT        NOT NULL,
                http_status_code  INT,
                error_message     TEXT,
                response_time_ms  INT         NOT NULL DEFAULT 0,
                occurred_at       TIMESTAMPTZ NOT NULL,
                will_retry        BOOLEAN     NOT NULL DEFAULT false,
                next_retry_at     TIMESTAMPTZ,
                task_type         TEXT,
                payload_size_bytes INT,
                notification_type TEXT
            )""",
            f"CREATE INDEX IF NOT EXISTS {lt}_queue_id_idx ON {lt} (queue_id)",
            f"CREATE INDEX IF NOT EXISTS {lt}_task_id_idx ON {lt} (task_id)",
        ]
        async with self._pool.connection() as conn:
            for stmt in statements:
                await conn.execute(stmt)

    # ----------------------------------------------------------------- send

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
        breaker_key: str | None = None,
        notification_type: str | None = None,
    ) -> WebhookDeliveryResult | None:
        """Enqueue one MCP-style webhook delivery; **always returns None**.

        The actual HTTP send happens in :meth:`run_worker`. This method
        writes the job to ``adcp_webhook_delivery_queue`` and returns
        immediately. ``None`` is returned whether the circuit is open
        (delivery skipped) or the job was successfully enqueued.

        For delivery outcomes use a
        :class:`~adcp.webhook_supervisor.DeliveryLogSink` or query the
        ``adcp_webhook_delivery_log`` table directly.

        :param breaker_key: Override the circuit-breaker lookup key (default:
            ``url``). Multi-tenant sellers whose buyers share a SaaS receiver
            URL MUST pass a tenant-scoped key (e.g. ``f"{tenant_id}:{url}"``).
        :param notification_type: Passed through to the delivery log for
            delivery-report webhooks (``scheduled`` / ``final`` / etc.).
        """
        if not self._worker_started and not self._worker_warned:
            self._worker_warned = True
            logger.warning(
                "[adcp.webhook_supervisor_pg] send_mcp() called before run_worker() "
                "has been started. Deliveries will be queued but not sent until a worker "
                "is running. Call asyncio.create_task(supervisor.run_worker()) at startup."
            )

        bkey = breaker_key or url

        # Check circuit state; reject immediately if OPEN within the timeout window.
        async with self._pool.connection() as conn:
            cur = await conn.execute(self._sql_circuit_get, (bkey,))
            row = await cur.fetchone()

        if row is not None:
            state: str = row[0]
            opened_at: datetime | None = row[1]
            if state == "open" and opened_at is not None:
                if opened_at.tzinfo is None:
                    opened_at = opened_at.replace(tzinfo=UTC)
                elapsed = (datetime.now(UTC) - opened_at).total_seconds()
                if elapsed < self._circuit_policy.open_timeout_seconds:
                    occurred_at = datetime.now(UTC)
                    attempt = DeliveryAttempt(
                        url=url,
                        sequence_key=sequence_key,
                        sequence_number=None,
                        attempt_number=0,
                        max_attempts=self._retry.max_attempts,
                        outcome="circuit_open",
                        http_status_code=None,
                        error_message=f"circuit breaker OPEN for {bkey} — skipped delivery",
                        response_time_ms=0,
                        occurred_at=occurred_at,
                        will_retry=False,
                        next_retry_at=None,
                        task_type=task_type,
                        task_id=task_id,
                        payload_size_bytes=None,
                        notification_type=notification_type,
                    )
                    await self._log_circuit_open(bkey, attempt)
                    await self._call_sink(attempt)
                    logger.warning(
                        "[adcp.webhook_supervisor_pg] circuit OPEN for %s — skipped %s",
                        bkey,
                        task_type or "webhook",
                    )
                    return None
                # Open timeout elapsed; transition to half_open so next worker probes.
                async with self._pool.connection() as conn:
                    await conn.execute(self._sql_circuit_set_half_open, (bkey,))

        status_str = status if isinstance(status, str) else str(status)
        result_json = json.dumps(result) if result is not None else None

        async with self._pool.connection() as conn:
            cur = await conn.execute(
                self._sql_enqueue,
                (
                    bkey,
                    url,
                    task_id,
                    task_type,
                    status_str,
                    result_json,
                    token,
                    sequence_key,
                    self._retry.max_attempts,
                    notification_type,
                ),
            )
            enqueue_row = await cur.fetchone()

        queue_id = enqueue_row[0] if enqueue_row else None
        logger.debug(
            "[adcp.webhook_supervisor_pg] enqueued %s → %s (queue_id=%s)",
            task_id,
            url,
            queue_id,
        )
        return None

    # ----------------------------------------------------------------- worker

    async def run_worker(self, *, poll_interval: float = 0.5) -> None:
        """Poll the delivery queue with ``FOR UPDATE SKIP LOCKED``; runs forever.

        **REQUIRED** — start at app startup::

            asyncio.create_task(supervisor.run_worker())

        Multiple processes or coroutines can run concurrently; ``SKIP LOCKED``
        ensures each job is processed by exactly one worker. The DB connection
        is held for the duration of the HTTP send so a crashed worker
        automatically releases the job back to the queue via transaction
        rollback (no separate recovery sweep needed).

        :param poll_interval: Seconds to sleep when the queue is empty.
        """
        self._worker_started = True
        logger.info(
            "[adcp.webhook_supervisor_pg] worker started (poll_interval=%.2fs)",
            poll_interval,
        )
        while True:
            try:
                delivered = await self._poll_and_process()
                if not delivered:
                    await asyncio.sleep(poll_interval)
            except asyncio.CancelledError:
                logger.info("[adcp.webhook_supervisor_pg] worker cancelled — shut down")
                raise
            except Exception:
                logger.exception(
                    "[adcp.webhook_supervisor_pg] worker error; will retry after %.2fs",
                    poll_interval,
                )
                await asyncio.sleep(poll_interval)

    # ---------------------------------------------------------- internal helpers

    async def _poll_and_process(self) -> bool:
        """Lease one job from the queue, send it, update state. Returns True if processed."""
        # The connection stays open for the duration of the HTTP send so the
        # FOR UPDATE lock is held throughout. On crash (or CancelledError),
        # the transaction rolls back and the job returns to 'pending'.
        async with self._pool.connection() as conn:
            cur = await conn.execute(self._sql_poll)
            row = await cur.fetchone()
            if row is None:
                return False

            (
                queue_id,
                bkey,
                url,
                task_id,
                task_type,
                status_str,
                result_json,
                token,
                sequence_key,
                attempt_count,
                max_attempts,
                idempotency_key,
                sent_body,
                notification_type,
            ) = row

            attempt_number = attempt_count + 1
            will_retry = attempt_number < max_attempts

            # Deferred import avoids circular module dependency at package init.
            from adcp.webhook_sender import WebhookDeliveryResult  # noqa: PLC0415

            t0 = time.monotonic()
            occurred_at = datetime.now(UTC)
            delivery_result: WebhookDeliveryResult | None = None
            exc_caught: BaseException | None = None

            try:
                if sent_body:
                    # Spec-compliant retry: replay the exact bytes so the receiver
                    # can dedup via the same idempotency_key (per mcp-webhook-payload.json:
                    # "Publishers MUST … reuse the same key on every retry").
                    prev = WebhookDeliveryResult(
                        status_code=0,
                        idempotency_key=idempotency_key or "",
                        url=url,
                        response_headers={},
                        response_body=b"",
                        sent_body=sent_body,
                    )
                    delivery_result = await self._sender.resend(prev)
                else:
                    result_obj = json.loads(result_json) if result_json else None
                    delivery_result = await self._sender.send_mcp(
                        url=url,
                        task_id=task_id,
                        status=status_str,
                        task_type=task_type,
                        result=result_obj,
                        token=token,
                    )
            except Exception as exc:
                exc_caught = exc

            response_time_ms = int((time.monotonic() - t0) * 1000)
            success = delivery_result is not None and delivery_result.ok

            if success:
                assert delivery_result is not None  # narrowing for mypy
                await conn.execute(self._sql_delete_job, (queue_id,))
                await conn.execute(self._sql_circuit_success, (bkey,))
                attempt = DeliveryAttempt(
                    url=url,
                    sequence_key=sequence_key,
                    sequence_number=queue_id,
                    attempt_number=attempt_number,
                    max_attempts=max_attempts,
                    outcome="success",
                    http_status_code=delivery_result.status_code,
                    error_message=None,
                    response_time_ms=response_time_ms,
                    occurred_at=occurred_at,
                    will_retry=False,
                    next_retry_at=None,
                    task_type=task_type,
                    task_id=task_id,
                    payload_size_bytes=(
                        len(delivery_result.sent_body) if delivery_result.sent_body else None
                    ),
                    notification_type=notification_type,
                )
            else:
                await conn.execute(self._sql_circuit_failure, (bkey,))

                next_delay = (
                    self._retry.delay_for_attempt(attempt_number + 1) if will_retry else None
                )
                next_retry_at = (
                    occurred_at + timedelta(seconds=next_delay) if next_delay is not None else None
                )

                if will_retry:
                    stored_body = delivery_result.sent_body if delivery_result is not None else None
                    stored_ikey = (
                        delivery_result.idempotency_key if delivery_result is not None else None
                    )
                    await conn.execute(
                        self._sql_reschedule,
                        (
                            attempt_number,
                            next_retry_at or occurred_at,
                            stored_body,
                            stored_ikey,
                            queue_id,
                        ),
                    )
                else:
                    await conn.execute(self._sql_delete_job, (queue_id,))

                if delivery_result is not None:
                    err_msg: str | None = (
                        f"HTTP {delivery_result.status_code}: "
                        f"{delivery_result.response_body[:200]!r}"
                    )
                    http_status: int | None = delivery_result.status_code
                    psize: int | None = (
                        len(delivery_result.sent_body) if delivery_result.sent_body else None
                    )
                elif exc_caught is not None:
                    err_msg = f"{type(exc_caught).__name__}: {exc_caught}"
                    http_status = None
                    psize = None
                else:
                    err_msg = None
                    http_status = None
                    psize = None

                attempt = DeliveryAttempt(
                    url=url,
                    sequence_key=sequence_key,
                    sequence_number=queue_id,
                    attempt_number=attempt_number,
                    max_attempts=max_attempts,
                    outcome="failure",
                    http_status_code=http_status,
                    error_message=err_msg,
                    response_time_ms=response_time_ms,
                    occurred_at=occurred_at,
                    will_retry=will_retry,
                    next_retry_at=next_retry_at,
                    task_type=task_type,
                    task_id=task_id,
                    payload_size_bytes=psize,
                    notification_type=notification_type,
                )

            await self._log_attempt_via_conn(conn, queue_id, bkey, attempt)
            # connection __aexit__ commits here; lock released

        # Sink is called outside the transaction so a slow/broken sink
        # cannot interfere with the queue update.
        await self._call_sink(attempt)

        if exc_caught is not None and not will_retry:
            raise exc_caught  # propagate to run_worker's exception logger

        return True

    async def _log_attempt_via_conn(
        self,
        conn: Any,
        queue_id: int | None,
        bkey: str,
        attempt: DeliveryAttempt,
    ) -> None:
        """Write a delivery attempt row using an already-open connection."""
        try:
            await conn.execute(
                self._sql_log_insert,
                (
                    queue_id,
                    bkey,
                    attempt.url,
                    attempt.task_id,
                    attempt.sequence_key,
                    attempt.sequence_number,
                    attempt.attempt_number,
                    attempt.max_attempts,
                    attempt.outcome,
                    attempt.http_status_code,
                    attempt.error_message,
                    attempt.response_time_ms,
                    attempt.occurred_at,
                    attempt.will_retry,
                    attempt.next_retry_at,
                    attempt.task_type,
                    attempt.payload_size_bytes,
                    attempt.notification_type,
                ),
            )
        except Exception:
            logger.warning(
                "[adcp.webhook_supervisor_pg] failed to log attempt for %s — delivery unaffected",
                attempt.url,
                exc_info=True,
            )

    async def _log_circuit_open(self, bkey: str, attempt: DeliveryAttempt) -> None:
        """Write a circuit_open attempt row using a fresh connection."""
        try:
            async with self._pool.connection() as conn:
                await self._log_attempt_via_conn(conn, None, bkey, attempt)
        except Exception:
            logger.warning(
                "[adcp.webhook_supervisor_pg] failed to log circuit_open for %s",
                attempt.url,
                exc_info=True,
            )

    async def _call_sink(self, attempt: DeliveryAttempt) -> None:
        if self._log_sink is None:
            return
        try:
            await asyncio.wait_for(
                self._log_sink.record(attempt),
                timeout=self._retry.sink_timeout_seconds,
            )
        except asyncio.TimeoutError:
            logger.warning(
                "[adcp.webhook_supervisor_pg] DeliveryLogSink timed out for %s",
                attempt.url,
            )
        except Exception:
            logger.warning(
                "[adcp.webhook_supervisor_pg] DeliveryLogSink raised for %s",
                attempt.url,
                exc_info=True,
            )


__all__ = [
    "DEFAULT_CIRCUIT_TABLE",
    "DEFAULT_LOG_TABLE",
    "DEFAULT_QUEUE_TABLE",
    "PG_AVAILABLE",
    "PgWebhookDeliverySupervisor",
]
