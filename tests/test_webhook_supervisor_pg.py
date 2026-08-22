"""Tests for :mod:`adcp.webhook_supervisor_pg` — unit tests with mock psycopg pool.

psycopg3 is an optional dependency (``adcp[pg]``); these tests mock the pool
and connection entirely so they pass without a real Postgres instance or the
psycopg package installed.

Behaviour under test:

* :class:`PgWebhookDeliverySupervisor` raises on construction when pg deps
  are absent, sender is None, or a table name is invalid.
* ``send_mcp`` emits a warning when called before ``run_worker`` is started.
* ``send_mcp`` checks circuit state from the DB and rejects OPEN circuits.
* ``send_mcp`` enqueues to the delivery queue and returns ``None``.
* ``_poll_and_process`` (worker core) handles success, failure, and retry.
* Retry path uses ``sender.resend()`` to replay the same wire bytes
  (spec-compliant idempotency-key reuse).
* Circuit success uses RETURNING to get post-update state (half-open atomicity).
* Sink timeouts and exceptions are swallowed; a broken sink must not cascade.
* ``create_schema`` executes exactly one statement per DDL item (psycopg
  does not split on semicolons; each must be a separate ``execute`` call).
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from adcp.webhook_sender import WebhookDeliveryResult
from adcp.webhook_supervisor import (
    DeliveryAttempt,
    RetryPolicy,
)

UTC = timezone.utc

# --------------------------------------------------------------------------- helpers


def _ok(url: str = "https://buyer.example/wh") -> WebhookDeliveryResult:
    return WebhookDeliveryResult(
        status_code=200,
        idempotency_key="ikey-1",
        url=url,
        response_headers={},
        response_body=b"{}",
        sent_body=b'{"task":"done"}',
    )


def _fail(url: str = "https://buyer.example/wh") -> WebhookDeliveryResult:
    return WebhookDeliveryResult(
        status_code=503,
        idempotency_key="ikey-1",
        url=url,
        response_headers={},
        response_body=b"upstream error",
        sent_body=b'{"task":"done"}',
    )


def _cursor(val: Any = None) -> AsyncMock:
    """Fake async cursor whose fetchone() returns val."""
    cur = AsyncMock()
    cur.fetchone = AsyncMock(return_value=val)
    return cur


def _make_conn(*fetchone_vals: Any) -> AsyncMock:
    """Fake AsyncConnection whose sequential execute() calls return cursors.

    Each positional arg becomes the fetchone() return value for the
    corresponding execute() call.
    """
    cursors = [_cursor(v) for v in fetchone_vals]
    conn = AsyncMock()
    conn.__aenter__ = AsyncMock(return_value=conn)
    conn.__aexit__ = AsyncMock(return_value=False)
    conn.execute = AsyncMock(side_effect=cursors)
    return conn


def _make_pool(*conns: AsyncMock) -> MagicMock:
    """Fake AsyncConnectionPool that yields the given connections in order."""
    ctxs = []
    for conn in conns:
        ctx = AsyncMock()
        ctx.__aenter__ = AsyncMock(return_value=conn)
        ctx.__aexit__ = AsyncMock(return_value=False)
        ctxs.append(ctx)
    pool = MagicMock()
    pool.connection = MagicMock(side_effect=ctxs)
    return pool


def _make_sender(send_result: WebhookDeliveryResult | None = None) -> AsyncMock:
    sender = AsyncMock()
    sender.send_mcp = AsyncMock(return_value=send_result or _ok())
    sender.resend = AsyncMock(return_value=send_result or _ok())
    return sender


def _make_supervisor(pool: Any, sender: Any, **kwargs: Any) -> Any:
    """Construct PgWebhookDeliverySupervisor with PG_AVAILABLE patched to True."""
    from adcp.webhook_supervisor_pg import PgWebhookDeliverySupervisor

    with patch("adcp.webhook_supervisor_pg.PG_AVAILABLE", True):
        sup = PgWebhookDeliverySupervisor.__new__(PgWebhookDeliverySupervisor)
        PgWebhookDeliverySupervisor.__init__(sup, pool, sender, **kwargs)
    return sup


# ---------------------------------------------------------------------- fixtures

_QUEUE_ROW_BASE = (
    # id, breaker_key, url, task_id, task_type, status_str,
    1,
    "https://buyer.example/wh",
    "https://buyer.example/wh",
    "task-123",
    "sync_completion",
    "pending",
    # result_json, token, sequence_key, attempt_count, max_attempts
    None,
    None,
    None,
    0,
    3,
    # idempotency_key, sent_body, notification_type, operation_id
    None,
    None,
    None,
    None,
)


def _queue_row(**overrides: Any) -> tuple:
    row = list(_QUEUE_ROW_BASE)
    _fields = [
        "id",
        "breaker_key",
        "url",
        "task_id",
        "task_type",
        "status_str",
        "result_json",
        "token",
        "sequence_key",
        "attempt_count",
        "max_attempts",
        "idempotency_key",
        "sent_body",
        "notification_type",
        "operation_id",
    ]
    for k, v in overrides.items():
        row[_fields.index(k)] = v
    return tuple(row)


# ----------------------------------------------------------------------- tests


class TestConstruction:
    def test_raises_without_pg(self) -> None:
        from adcp.webhook_supervisor_pg import PgWebhookDeliverySupervisor

        with patch("adcp.webhook_supervisor_pg.PG_AVAILABLE", False):
            with pytest.raises(ImportError, match="pip install 'adcp\\[pg\\]'"):
                PgWebhookDeliverySupervisor(MagicMock(), _make_sender())

    def test_raises_for_none_sender(self) -> None:
        with pytest.raises(ValueError, match="non-None WebhookSender"):
            _make_supervisor(MagicMock(), None)  # type: ignore[arg-type]

    def test_raises_for_invalid_table_name(self) -> None:
        with pytest.raises(ValueError, match="ASCII only"):
            _make_supervisor(MagicMock(), _make_sender(), circuit_table="bad-name!")

    def test_raises_for_unicode_table_name(self) -> None:
        with pytest.raises(ValueError, match="ASCII only"):
            _make_supervisor(MagicMock(), _make_sender(), queue_table="adcp_wébhook")

    def test_custom_table_names_accepted(self) -> None:
        sup = _make_supervisor(
            MagicMock(),
            _make_sender(),
            circuit_table="my_circuit",
            queue_table="my_queue",
            log_table="my_log",
        )
        assert sup._circuit_t == "my_circuit"
        assert sup._queue_t == "my_queue"
        assert sup._log_t == "my_log"

    def test_preformatted_sql_uses_table_names(self) -> None:
        sup = _make_supervisor(
            MagicMock(),
            _make_sender(),
            circuit_table="my_circuit",
            queue_table="my_queue",
        )
        assert "my_circuit" in sup._sql_circuit_get
        assert "my_queue" in sup._sql_enqueue
        assert "my_queue" in sup._sql_poll


class TestCreateSchema:
    @pytest.mark.asyncio
    async def test_executes_all_ddl_statements_separately(self) -> None:
        conn = AsyncMock()
        conn.__aenter__ = AsyncMock(return_value=conn)
        conn.__aexit__ = AsyncMock(return_value=False)
        conn.execute = AsyncMock()
        ctx = AsyncMock()
        ctx.__aenter__ = AsyncMock(return_value=conn)
        ctx.__aexit__ = AsyncMock(return_value=False)
        pool = MagicMock()
        pool.connection = MagicMock(return_value=ctx)

        sup = _make_supervisor(pool, _make_sender())
        await sup.create_schema()

        # 7 statements: 3 tables + 1 ALTER (operation_id backfill on the
        # queue table) + 3 indexes (1 partial + 2 standard)
        assert conn.execute.call_count == 7

    @pytest.mark.asyncio
    async def test_each_statement_contains_table_name(self) -> None:
        conn = AsyncMock()
        conn.__aenter__ = AsyncMock(return_value=conn)
        conn.__aexit__ = AsyncMock(return_value=False)
        conn.execute = AsyncMock()
        ctx = AsyncMock()
        ctx.__aenter__ = AsyncMock(return_value=conn)
        ctx.__aexit__ = AsyncMock(return_value=False)
        pool = MagicMock()
        pool.connection = MagicMock(return_value=ctx)

        sup = _make_supervisor(pool, _make_sender())
        await sup.create_schema()

        sqls = [call.args[0] for call in conn.execute.call_args_list]
        assert any("adcp_webhook_circuit_state" in s for s in sqls)
        assert any("adcp_webhook_delivery_queue" in s for s in sqls)
        assert any("adcp_webhook_delivery_log" in s for s in sqls)


class TestSendMcp:
    @pytest.mark.asyncio
    async def test_returns_none_on_success(self) -> None:
        conn_circuit = _make_conn(None)  # no circuit row (first send)
        conn_enqueue = _make_conn((42,))  # queue_id = 42
        pool = _make_pool(conn_circuit, conn_enqueue)

        sup = _make_supervisor(pool, _make_sender())
        sup._worker_started = True  # suppress the warning

        result = await sup.send_mcp(
            url="https://b.example/wh",
            task_id="t1",
            status="completed",
            task_type="create_media_buy",
            operation_id="op-pg-supervisor-test",
        )
        assert result is None

    @pytest.mark.asyncio
    async def test_warning_emitted_before_worker_starts(self, caplog: Any) -> None:
        conn_circuit = _make_conn(None)
        conn_enqueue = _make_conn((1,))
        pool = _make_pool(conn_circuit, conn_enqueue)

        sup = _make_supervisor(pool, _make_sender())
        # Do NOT set _worker_started

        import logging

        with caplog.at_level(logging.WARNING, logger="adcp.webhook_supervisor_pg"):
            await sup.send_mcp(
                url="https://b.example/wh",
                task_id="t1",
                status="completed",
                task_type="create_media_buy",
                operation_id="op-pg-supervisor-test",
            )

        assert any("run_worker" in r.message for r in caplog.records)

    @pytest.mark.asyncio
    async def test_warning_emitted_only_once(self, caplog: Any) -> None:
        import logging

        # Two calls on same supervisor → one warning
        conn_c1 = _make_conn(None)
        conn_e1 = _make_conn((1,))
        conn_c2 = _make_conn(None)
        conn_e2 = _make_conn((2,))
        pool = _make_pool(conn_c1, conn_e1, conn_c2, conn_e2)
        sup = _make_supervisor(pool, _make_sender())
        with caplog.at_level(logging.WARNING, logger="adcp.webhook_supervisor_pg"):
            await sup.send_mcp(
                url="u",
                task_id="t",
                status="s",
                task_type="create_media_buy",
                operation_id="op-pg-supervisor-test",
            )
            await sup.send_mcp(
                url="u",
                task_id="t",
                status="s",
                task_type="create_media_buy",
                operation_id="op-pg-supervisor-test",
            )

        warn_msgs = [r.message for r in caplog.records if "run_worker" in r.message]
        assert len(warn_msgs) == 1

    @pytest.mark.asyncio
    async def test_circuit_open_rejects_and_returns_none(self) -> None:
        from datetime import timedelta

        opened_at = datetime.now(UTC) - timedelta(seconds=5)  # opened 5s ago, timeout=60s

        # 1st pool.connection: circuit_get → OPEN row
        conn_circuit = _make_conn(("open", opened_at))
        # 2nd: log_circuit_open → no fetchone needed
        conn_log = _make_conn(None)
        pool = _make_pool(conn_circuit, conn_log)

        sup = _make_supervisor(pool, _make_sender())
        sup._worker_started = True

        result = await sup.send_mcp(
            url="https://b.example/wh",
            task_id="t",
            status="s",
            task_type="create_media_buy",
            operation_id="op-pg-supervisor-test",
        )
        assert result is None
        # Should NOT have called enqueue
        enqueue_calls = [
            c
            for c in conn_circuit.execute.call_args_list
            if "INSERT INTO" in (c.args[0] if c.args else "")
            and "delivery_queue" in (c.args[0] if c.args else "")
        ]
        assert len(enqueue_calls) == 0

    @pytest.mark.asyncio
    async def test_circuit_open_timeout_transitions_to_half_open(self) -> None:
        from datetime import timedelta

        opened_at = datetime.now(UTC) - timedelta(seconds=90)  # 90s > 60s timeout

        conn_circuit = _make_conn(("open", opened_at))
        conn_half_open = _make_conn(None)  # set_half_open
        conn_enqueue = _make_conn((7,))
        pool = _make_pool(conn_circuit, conn_half_open, conn_enqueue)

        sup = _make_supervisor(pool, _make_sender())
        sup._worker_started = True

        result = await sup.send_mcp(
            url="https://b.example/wh",
            task_id="t",
            status="s",
            task_type="create_media_buy",
            operation_id="op-pg-supervisor-test",
        )
        assert result is None  # always None from send_mcp

        # set_half_open was called
        half_open_sql = conn_half_open.execute.call_args_list[0].args[0]
        assert "half_open" in half_open_sql

    @pytest.mark.asyncio
    async def test_breaker_key_used_as_circuit_lookup_key(self) -> None:
        conn_circuit = _make_conn(None)
        conn_enqueue = _make_conn((1,))
        pool = _make_pool(conn_circuit, conn_enqueue)

        sup = _make_supervisor(pool, _make_sender())
        sup._worker_started = True

        await sup.send_mcp(
            url="https://shared.example/wh",
            task_id="t",
            task_type="create_media_buy",
            operation_id="op-pg-supervisor-test",
            status="s",
            breaker_key="tenant-42:https://shared.example/wh",
        )

        circuit_params = conn_circuit.execute.call_args_list[0].args[1]
        assert circuit_params[0] == "tenant-42:https://shared.example/wh"


class TestWorkerSuccess:
    @pytest.mark.asyncio
    async def test_success_deletes_job_and_updates_circuit(self) -> None:
        sender = _make_sender(_ok())
        poll_row = _queue_row()

        # All within one connection (worker keeps it open)
        conn = _make_conn(
            poll_row,  # poll
            None,  # delete_job
            ("closed", 0),  # circuit_success RETURNING
            None,  # log_insert
        )
        pool = _make_pool(conn)

        sup = _make_supervisor(pool, sender)
        sup._worker_started = True

        delivered = await sup._poll_and_process()
        assert delivered is True

        sql_calls = [c.args[0] for c in conn.execute.call_args_list]
        assert any("FOR UPDATE SKIP LOCKED" in s for s in sql_calls)
        assert any("DELETE FROM" in s for s in sql_calls)
        assert any("circuit_success" in s or "success_count" in s for s in sql_calls)

    @pytest.mark.asyncio
    async def test_success_calls_sender_send_mcp(self) -> None:
        sender = _make_sender(_ok())
        conn = _make_conn(
            _queue_row(),
            None,
            ("closed", 0),
            None,
        )
        pool = _make_pool(conn)
        sup = _make_supervisor(pool, sender)

        await sup._poll_and_process()

        sender.send_mcp.assert_awaited_once()
        sender.resend.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_operation_id_persisted_on_enqueue(self) -> None:
        """The buyer-supplied operation_id is bound as the last enqueue
        parameter so it lands on the queue row for the worker to replay."""
        conn_circuit = _make_conn(None)
        conn_enqueue = _make_conn((7,))
        pool = _make_pool(conn_circuit, conn_enqueue)
        sup = _make_supervisor(pool, _make_sender())
        sup._worker_started = True

        await sup.send_mcp(
            url="https://b.example/wh",
            task_id="t1",
            status="completed",
            task_type="get_products",
            operation_id="op-xyz-789",
        )
        enqueue_params = conn_enqueue.execute.call_args.args[1]
        assert enqueue_params[-1] == "op-xyz-789"

    @pytest.mark.asyncio
    async def test_worker_replays_operation_id_to_sender(self) -> None:
        """The worker reads operation_id off the queue row and echoes it
        into the underlying WebhookSender.send_mcp call."""
        sender = _make_sender(_ok())
        conn = _make_conn(
            _queue_row(operation_id="op-replay-123"),
            None,
            ("closed", 0),
            None,
        )
        pool = _make_pool(conn)
        sup = _make_supervisor(pool, sender)

        await sup._poll_and_process()

        sender.send_mcp.assert_awaited_once()
        assert sender.send_mcp.await_args.kwargs["operation_id"] == "op-replay-123"

    @pytest.mark.asyncio
    async def test_empty_queue_returns_false(self) -> None:
        conn = _make_conn(None)  # poll returns None
        pool = _make_pool(conn)
        sup = _make_supervisor(pool, _make_sender())

        delivered = await sup._poll_and_process()
        assert delivered is False


class TestWorkerFailureAndRetry:
    @pytest.mark.asyncio
    async def test_failure_reschedules_when_retries_remain(self) -> None:
        sender = _make_sender(_fail())
        conn = _make_conn(
            _queue_row(attempt_count=0, max_attempts=3),  # poll
            None,  # circuit_failure
            None,  # reschedule (not delete)
            None,  # log_insert
        )
        pool = _make_pool(conn)
        sup = _make_supervisor(pool, sender)

        await sup._poll_and_process()

        sql_calls = [c.args[0] for c in conn.execute.call_args_list]
        # Should reschedule, not delete
        assert any("status_str = 'retry'" in s for s in sql_calls)
        assert not any("DELETE FROM" in s for s in sql_calls)

    @pytest.mark.asyncio
    async def test_final_failure_deletes_job(self) -> None:
        sender = _make_sender(_fail())
        conn = _make_conn(
            _queue_row(attempt_count=2, max_attempts=3),  # attempt 3/3
            None,  # circuit_failure
            None,  # delete_job
            None,  # log_insert
        )
        pool = _make_pool(conn)
        sup = _make_supervisor(pool, sender)

        await sup._poll_and_process()

        sql_calls = [c.args[0] for c in conn.execute.call_args_list]
        assert any("DELETE FROM" in s for s in sql_calls)
        assert not any("status_str = 'retry'" in s for s in sql_calls)

    @pytest.mark.asyncio
    async def test_retry_uses_resend_when_sent_body_stored(self) -> None:
        """Second attempt must call resend() with stored bytes for idempotency-key parity."""
        sender = _make_sender(_ok())
        stored_body = b'{"original":"payload"}'
        conn = _make_conn(
            _queue_row(
                attempt_count=1,  # this is attempt 2
                max_attempts=3,
                sent_body=stored_body,
                idempotency_key="original-ikey",
            ),
            None,  # delete_job (success)
            ("closed", 0),  # circuit_success
            None,  # log_insert
        )
        pool = _make_pool(conn)
        sup = _make_supervisor(pool, sender)

        await sup._poll_and_process()

        sender.resend.assert_awaited_once()
        sender.send_mcp.assert_not_awaited()

        # The resend call should receive the stored idempotency_key
        resend_arg = sender.resend.call_args.args[0]
        assert resend_arg.idempotency_key == "original-ikey"
        assert resend_arg.sent_body == stored_body

    @pytest.mark.asyncio
    async def test_reschedule_stores_sent_body_for_next_attempt(self) -> None:
        fail_result = _fail()
        sender = _make_sender(fail_result)
        conn = _make_conn(
            _queue_row(attempt_count=0, max_attempts=3),  # attempt 1/3
            None,  # circuit_failure
            None,  # reschedule
            None,  # log_insert
        )
        pool = _make_pool(conn)
        sup = _make_supervisor(pool, sender)

        await sup._poll_and_process()

        sql_calls = conn.execute.call_args_list
        reschedule_call = next((c for c in sql_calls if "status_str = 'retry'" in c.args[0]), None)
        assert reschedule_call is not None
        # sent_body and idempotency_key are positional params 3 and 4 (0-indexed)
        params = reschedule_call.args[1]
        assert params[2] == fail_result.sent_body  # sent_body
        assert params[3] == fail_result.idempotency_key  # idempotency_key


class TestWorkerCircuitState:
    @pytest.mark.asyncio
    async def test_success_circuit_query_uses_returning(self) -> None:
        conn = _make_conn(
            _queue_row(),
            None,
            ("closed", 0),
            None,
        )
        pool = _make_pool(conn)
        sup = _make_supervisor(pool, _make_sender())

        await sup._poll_and_process()

        circuit_success_sql = conn.execute.call_args_list[2].args[0]
        assert "RETURNING" in circuit_success_sql.upper()

    @pytest.mark.asyncio
    async def test_failure_circuit_query_is_upsert(self) -> None:
        conn = _make_conn(
            _queue_row(attempt_count=2, max_attempts=3),
            None,
            None,
            None,
        )
        pool = _make_pool(conn)
        sup = _make_supervisor(pool, _make_sender(_fail()))

        await sup._poll_and_process()

        circuit_fail_sql = conn.execute.call_args_list[1].args[0]
        assert "ON CONFLICT" in circuit_fail_sql.upper()
        assert "failure_count" in circuit_fail_sql


class TestSinkBehavior:
    @pytest.mark.asyncio
    async def test_slow_sink_is_swallowed(self) -> None:
        async def _slow_record(attempt: DeliveryAttempt) -> None:
            await asyncio.sleep(99)

        from adcp.webhook_supervisor import DeliveryLogSink

        class _SlowSink(DeliveryLogSink):
            async def record(self, attempt: DeliveryAttempt) -> None:  # type: ignore[override]
                await asyncio.sleep(99)

        conn = _make_conn(
            _queue_row(),
            None,
            ("closed", 0),
            None,
        )
        pool = _make_pool(conn)
        # Use a very short timeout so the test doesn't actually sleep 99s
        retry = RetryPolicy(sink_timeout_seconds=0.01)
        sup = _make_supervisor(pool, _make_sender(), retry=retry, log_sink=_SlowSink())

        # Must not raise — sink timeout must be swallowed
        delivered = await sup._poll_and_process()
        assert delivered is True

    @pytest.mark.asyncio
    async def test_exploding_sink_is_swallowed(self) -> None:
        from adcp.webhook_supervisor import DeliveryLogSink

        class _ExplodingSink(DeliveryLogSink):
            async def record(self, attempt: DeliveryAttempt) -> None:  # type: ignore[override]
                raise RuntimeError("BOOM")

        conn = _make_conn(
            _queue_row(),
            None,
            ("closed", 0),
            None,
        )
        pool = _make_pool(conn)
        sup = _make_supervisor(pool, _make_sender(), log_sink=_ExplodingSink())

        delivered = await sup._poll_and_process()
        assert delivered is True


class TestLogAttemptFault:
    @pytest.mark.asyncio
    async def test_log_insert_failure_does_not_crash_worker(self) -> None:
        conn = AsyncMock()
        conn.__aenter__ = AsyncMock(return_value=conn)
        conn.__aexit__ = AsyncMock(return_value=False)

        poll_cur = _cursor(_queue_row())
        delete_cur = _cursor(None)
        circuit_cur = _cursor(("closed", 0))

        conn.execute = AsyncMock(
            side_effect=[poll_cur, delete_cur, circuit_cur, RuntimeError("DB gone")]
        )
        ctx = AsyncMock()
        ctx.__aenter__ = AsyncMock(return_value=conn)
        ctx.__aexit__ = AsyncMock(return_value=False)
        pool = MagicMock()
        pool.connection = MagicMock(return_value=ctx)

        sup = _make_supervisor(pool, _make_sender())
        # Should not raise — log errors are swallowed
        delivered = await sup._poll_and_process()
        assert delivered is True


class TestRunWorkerLifecycle:
    @pytest.mark.asyncio
    async def test_run_worker_sets_worker_started(self) -> None:
        conn = _make_conn(None)  # empty queue → sleep → cancel
        pool = _make_pool(conn)
        sup = _make_supervisor(pool, _make_sender())

        assert not sup._worker_started

        async def _run_briefly() -> None:
            task = asyncio.create_task(sup.run_worker(poll_interval=0.01))
            await asyncio.sleep(0.02)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        await _run_briefly()
        assert sup._worker_started

    @pytest.mark.asyncio
    async def test_run_worker_reraises_cancelled_error(self) -> None:
        conn = _make_conn(None)
        pool = _make_pool(conn)
        sup = _make_supervisor(pool, _make_sender())

        task = asyncio.create_task(sup.run_worker(poll_interval=0.01))
        await asyncio.sleep(0.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
