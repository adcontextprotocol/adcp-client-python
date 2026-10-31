"""Real PostgreSQL evidence for ``PgWebhookDeliverySupervisor`` (#1606).

Through 9.2.0 ``send_mcp`` stored the task status in ``status_str`` while the
worker only claimed rows whose ``status_str`` was ``pending`` or ``retry``, so
no task webhook was ever sent. These tests drive a full enqueue, claim, sign
and POST cycle against a real database, then upgrade a 9.2.0 queue holding
stuck rows and deliver each of them exactly once.

ADCP_PG_TEST_URL must point to a private test database, as in the CI pg lane.
"""

from __future__ import annotations

import asyncio
import json
import os
import secrets
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from typing import Any

import httpx
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

psycopg_pool = pytest.importorskip("psycopg_pool")
TEST_URL = os.environ.get("ADCP_PG_TEST_URL")
if not TEST_URL:
    pytest.skip("ADCP_PG_TEST_URL not set", allow_module_level=True)

from adcp.types import GeneratedTaskStatus  # noqa: E402
from adcp.webhook_supervisor import RetryPolicy  # noqa: E402
from adcp.webhook_supervisor_pg import PgWebhookDeliverySupervisor  # noqa: E402
from adcp.webhooks import WebhookSender  # noqa: E402

URL = "https://buyer.example/hooks"

# The queue DDL and enqueue statement exactly as 9.2.0 (and every release back
# to 9.0.0b4) shipped them, so the upgrade test starts from a real old table.
LEGACY_QUEUE_DDL = """CREATE TABLE {qt} (
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
    notification_type TEXT,
    operation_id    TEXT
)"""
LEGACY_WORK_INDEX = """CREATE INDEX {qt}_work_idx
    ON {qt} (status_str, scheduled_at)
    WHERE status_str IN ('pending', 'retry')"""
LEGACY_ENQUEUE = (
    "INSERT INTO {qt} "
    "(breaker_key, url, task_id, task_type, status_str, result_json, "
    "token, sequence_key, max_attempts, notification_type, operation_id) "
    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id"
)
LEGACY_POLL = (
    "SELECT id FROM {qt} "
    "WHERE status_str IN ('pending', 'retry') AND scheduled_at <= now() "
    "ORDER BY scheduled_at LIMIT 1 FOR UPDATE SKIP LOCKED"
)

ALL_TASK_STATUSES = list(GeneratedTaskStatus)


@dataclass
class Receiver:
    """Buyer endpoint stand-in that records every POST body it is sent."""

    bodies: list[dict[str, Any]] = field(default_factory=list)
    responses: list[Callable[[], httpx.Response]] = field(default_factory=list)

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.bodies.append(json.loads(request.content))
        if self.responses:
            return self.responses.pop(0)()
        return httpx.Response(200, json={})


@dataclass
class Stack:
    pool: Any
    supervisor: PgWebhookDeliverySupervisor
    receiver: Receiver
    qt: str
    lt: str
    ct: str

    async def rows(self, sql: str, params: tuple[Any, ...] = ()) -> list[tuple[Any, ...]]:
        async with self.pool.connection() as conn:
            cur = await conn.execute(sql.format(qt=self.qt, lt=self.lt), params)
            return list(await cur.fetchall())

    async def execute(self, sql: str, params: tuple[Any, ...] = ()) -> None:
        async with self.pool.connection() as conn:
            await conn.execute(sql.format(qt=self.qt, lt=self.lt), params)

    async def drain(self) -> int:
        processed = 0
        while await self.supervisor._poll_and_process():
            processed += 1
        return processed


@pytest.fixture
async def stack(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[Stack]:
    receiver = Receiver()
    monkeypatch.setattr(
        "adcp.webhook_sender.build_async_ip_pinned_transport",
        lambda *a, **k: httpx.MockTransport(receiver.handle),
    )
    sender = WebhookSender(private_key=Ed25519PrivateKey.generate(), key_id="k1", alg="ed25519")
    suffix = secrets.token_hex(6)
    qt, lt, ct = (f"test_wh_{part}_{suffix}" for part in ("queue", "log", "circuit"))
    async with psycopg_pool.AsyncConnectionPool(
        TEST_URL, min_size=1, max_size=8, open=False
    ) as pool:
        supervisor = PgWebhookDeliverySupervisor(
            pool,
            sender,
            retry=RetryPolicy(base_delay_seconds=0, max_delay_seconds=0, jitter=False),
            queue_table=qt,
            log_table=lt,
            circuit_table=ct,
        )
        supervisor._worker_started = True
        try:
            yield Stack(pool, supervisor, receiver, qt, lt, ct)
        finally:
            async with pool.connection() as conn:
                for table in (qt, lt, ct):
                    await conn.execute(f"DROP TABLE IF EXISTS {table}")
            await sender.aclose()


async def send(stack: Stack, status: GeneratedTaskStatus | str, task_id: str = "task_1") -> None:
    await stack.supervisor.send_mcp(
        url=URL,
        task_id=task_id,
        status=status,
        task_type="create_media_buy",
        result={"media_buy_id": "mb_1"},
        operation_id="op_1",
        token="buyer-token-0123456789",
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ALL_TASK_STATUSES, ids=lambda s: s.value)
async def test_send_cycle_delivers_each_task_status(
    stack: Stack, status: GeneratedTaskStatus
) -> None:
    await stack.supervisor.create_schema()

    await send(stack, status)

    assert await stack.rows("SELECT status_str, delivery_state FROM {qt}") == [
        (status.value, "pending")
    ]
    assert await stack.supervisor._poll_and_process() is True
    assert await stack.supervisor._poll_and_process() is False

    [body] = stack.receiver.bodies
    assert body["status"] == status.value
    assert body["task_id"] == "task_1"
    assert body["task_type"] == "create_media_buy"
    assert body["operation_id"] == "op_1"
    assert body["token"] == "buyer-token-0123456789"
    # AdCP 3.3.0-beta.3: the result carries its task status (adcp#8547).
    assert body["result"] == {"status": status.value, "media_buy_id": "mb_1"}
    assert await stack.rows("SELECT id FROM {qt}") == []
    assert await stack.rows("SELECT outcome, attempt_number FROM {lt}") == [("success", 1)]


@pytest.mark.asyncio
async def test_failed_attempt_replays_the_same_bytes(stack: Stack) -> None:
    await stack.supervisor.create_schema()
    stack.receiver.responses.append(lambda: httpx.Response(503, text="busy"))

    await send(stack, "completed")

    assert await stack.supervisor._poll_and_process() is True
    assert await stack.rows("SELECT status_str, delivery_state, attempt_count FROM {qt}") == [
        ("completed", "retry", 1)
    ]
    assert await stack.drain() == 1

    first, second = stack.receiver.bodies
    assert first == second
    assert second["status"] == "completed"
    assert await stack.rows("SELECT id FROM {qt}") == []
    assert await stack.rows("SELECT outcome FROM {lt} ORDER BY id") == [
        ("failure",),
        ("success",),
    ]


@pytest.mark.asyncio
async def test_raised_attempt_retries_with_task_status_and_same_key(stack: Stack) -> None:
    """A transport error leaves no bytes to replay; the retry must not send ``retry``."""
    await stack.supervisor.create_schema()

    def reset() -> httpx.Response:
        raise httpx.ReadError("connection reset after the receiver read the body")

    stack.receiver.responses.append(reset)

    await send(stack, "failed")

    assert await stack.supervisor._poll_and_process() is True
    assert await stack.rows(
        "SELECT status_str, delivery_state, sent_body IS NULL, idempotency_key FROM {qt}"
    ) == [("failed", "retry", True, stack.receiver.bodies[0]["idempotency_key"])]
    assert await stack.drain() == 1

    first, second = stack.receiver.bodies
    assert second["status"] == "failed"
    assert second["idempotency_key"] == first["idempotency_key"]
    assert await stack.rows("SELECT id FROM {qt}") == []


@pytest.mark.asyncio
async def test_upgrade_delivers_rows_stuck_by_9_2_0_exactly_once(stack: Stack) -> None:
    await stack.execute(LEGACY_QUEUE_DDL)
    await stack.execute(LEGACY_WORK_INDEX)
    for index, status in enumerate(ALL_TASK_STATUSES):
        await stack.execute(
            LEGACY_ENQUEUE,
            (
                URL,
                URL,
                f"task_{index}",
                "create_media_buy",
                status.value,
                json.dumps({"media_buy_id": f"mb_{index}"}),
                None,
                None,
                3,
                None,
                f"op_{index}",
            ),
        )
    # What every 9.2.0 worker saw: nothing to claim.
    assert await stack.rows(LEGACY_POLL) == []

    await stack.supervisor.create_schema()
    await stack.supervisor.create_schema()

    rows = await stack.rows("SELECT task_id, status_str, delivery_state FROM {qt} ORDER BY id")
    assert rows == [
        (f"task_{index}", status.value, "pending") for index, status in enumerate(ALL_TASK_STATUSES)
    ]
    indexes = {
        name
        for (name,) in await stack.rows(
            "SELECT indexname FROM pg_indexes WHERE tablename = %s", (stack.qt,)
        )
    }
    assert f"{stack.qt}_delivery_idx" in indexes
    assert f"{stack.qt}_work_idx" not in indexes

    # Several workers drain the recovered rows concurrently; SKIP LOCKED must
    # hand each row to exactly one of them.
    async def worker() -> int:
        return await stack.drain()

    processed = await asyncio.gather(*(worker() for _ in range(4)))

    assert sum(processed) == len(ALL_TASK_STATUSES)
    delivered = sorted(
        (b["task_id"], b["status"], b["operation_id"]) for b in stack.receiver.bodies
    )
    assert delivered == sorted(
        (f"task_{index}", status.value, f"op_{index}")
        for index, status in enumerate(ALL_TASK_STATUSES)
    )
    assert len({b["idempotency_key"] for b in stack.receiver.bodies}) == len(ALL_TASK_STATUSES)
    assert await stack.rows("SELECT id FROM {qt}") == []

    # A later boot and poll find nothing left to send.
    await stack.supervisor.create_schema()
    assert await stack.supervisor._poll_and_process() is False
    assert len(stack.receiver.bodies) == len(ALL_TASK_STATUSES)


@pytest.mark.asyncio
async def test_rolling_deploy_mixes_old_and_new_processes_safely(stack: Stack) -> None:
    await stack.supervisor.create_schema()

    # A 9.2.0 process still enqueuing after the migration omits delivery_state.
    await stack.execute(
        LEGACY_ENQUEUE,
        (
            URL,
            URL,
            "task_old",
            "create_media_buy",
            "completed",
            None,
            None,
            None,
            3,
            None,
            "op_old",
        ),
    )
    await send(stack, "working", task_id="task_new")

    # A 9.2.0 worker claims on status_str and so never takes either row.
    assert await stack.rows(LEGACY_POLL) == []

    assert await stack.drain() == 2
    assert sorted((b["task_id"], b["status"]) for b in stack.receiver.bodies) == [
        ("task_new", "working"),
        ("task_old", "completed"),
    ]
