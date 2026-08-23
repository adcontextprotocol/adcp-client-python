"""Real-PostgreSQL conformance tests for the terminal task webhook outbox.

Set ``ADCP_PG_TEST_URL`` to enable this module. Each test uses isolated table
names so parallel jobs cannot share leases or terminal task state.
"""

from __future__ import annotations

import asyncio
import json
import os
import secrets
import uuid
from collections.abc import AsyncIterator
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

psycopg = pytest.importorskip("psycopg")
psycopg_pool = pytest.importorskip("psycopg_pool")

TEST_URL = os.environ.get("ADCP_PG_TEST_URL")
if not TEST_URL:
    pytest.skip(
        "ADCP_PG_TEST_URL not set — skipping task webhook outbox conformance tests",
        allow_module_level=True,
    )

from adcp.decisioning.pg import PgTaskRegistry, PgTaskWebhookOutbox  # noqa: E402
from adcp.webhook_sender import PreparedWebhook, WebhookDeliveryResult  # noqa: E402


def _sender() -> MagicMock:
    sender = MagicMock()
    sender._owns_client = True
    sender._allow_private_destinations = False
    sender._timeout = 10.0
    sender.signs_with_rfc9421 = True

    def prepare_mcp(**kwargs: Any) -> PreparedWebhook:
        key = f"whk_{uuid.uuid4().hex}"
        body = json.dumps(
            {
                "idempotency_key": key,
                "task_id": kwargs["task_id"],
                "task_type": kwargs["task_type"],
                "status": kwargs["status"],
                "operation_id": kwargs["operation_id"],
                "result": kwargs["result"],
                "token": kwargs["token"],
            }
        ).encode()
        return PreparedWebhook(url=kwargs["url"], idempotency_key=key, body=body)

    sender.prepare_mcp.side_effect = prepare_mcp
    sender.send_prepared = AsyncMock(
        side_effect=lambda prepared: WebhookDeliveryResult(
            status_code=200,
            idempotency_key=prepared.idempotency_key,
            url=prepared.url,
            response_headers={},
            response_body=b"{}",
            sent_body=prepared.body,
        )
    )
    return sender


@pytest.fixture()
async def stack() -> AsyncIterator[tuple[Any, PgTaskRegistry, PgTaskWebhookOutbox, MagicMock]]:
    suffix = secrets.token_hex(6)
    task_table = f"test_dtasks_{suffix}"
    outbox_table = f"test_task_outbox_{suffix}"
    async with psycopg_pool.AsyncConnectionPool(
        TEST_URL,
        min_size=2,
        max_size=8,
        open=False,
    ) as pool:
        await pool.open()
        sender = _sender()
        outbox = PgTaskWebhookOutbox(
            pool=pool,
            sender=sender,
            encryption_key=b"e" * 32,
            delivery_retry_horizon_seconds=86_400,
            table=outbox_table,
        )
        registry = PgTaskRegistry(
            pool=pool,
            task_webhook_outbox=outbox,
            _table=task_table,
        )
        await registry.create_schema()
        await outbox.create_schema()
        try:
            yield pool, registry, outbox, sender
        finally:
            async with pool.connection() as conn:
                await conn.execute(f"DROP TABLE IF EXISTS {outbox_table}")  # noqa: S608
                await conn.execute(f"DROP TABLE IF EXISTS {task_table}")  # noqa: S608


@pytest.mark.asyncio
async def test_terminal_state_and_encrypted_outbox_commit_together(stack) -> None:
    pool, registry, outbox, _sender_mock = stack
    task_id = await registry.issue(
        account_id="acct_1",
        task_type="create_media_buy",
        webhook_url="https://buyer.example/webhook",
        webhook_operation_id="op_1",
        webhook_token="buyer-secret",
    )
    await registry.complete(task_id, {"media_buy_id": "mb_1"})

    async with pool.connection() as conn:
        task_row = await (
            await conn.execute(
                f"SELECT state, webhook_registration, webhook_registration_nonce "  # noqa: S608
                f"FROM {registry._table} WHERE task_id = %s",
                (task_id,),
            )
        ).fetchone()
        outbox_row = await (
            await conn.execute(
                f"SELECT encrypted_body, first_attempt_at, retry_until "  # noqa: S608
                f"FROM {outbox._table} WHERE task_id = %s",
                (task_id,),
            )
        ).fetchone()
    assert task_row == ("completed", None, None)
    assert outbox_row is not None
    assert b"buyer-secret" not in bytes(outbox_row[0])
    assert outbox_row[1:] == (None, None)

    assert await outbox.process_one() is True
    async with pool.connection() as conn:
        delivered = await (
            await conn.execute(
                f"SELECT state, first_attempt_at, retry_until "  # noqa: S608
                f"FROM {outbox._table} WHERE task_id = %s",
                (task_id,),
            )
        ).fetchone()
    assert delivered is not None
    assert delivered[0] == "delivered"
    assert delivered[1] is not None
    assert delivered[2] is not None


@pytest.mark.asyncio
async def test_concurrent_claims_deliver_one_attempt(stack) -> None:
    _pool, registry, outbox, sender = stack
    task_id = await registry.issue(
        account_id="acct_1",
        task_type="create_media_buy",
        webhook_url="https://buyer.example/webhook",
        webhook_operation_id="op_1",
    )
    await registry.complete(task_id, {"media_buy_id": "mb_1"})

    processed = await asyncio.gather(outbox.process_one(), outbox.process_one())
    assert sorted(processed) == [False, True]
    sender.send_prepared.assert_awaited_once()


@pytest.mark.asyncio
async def test_autocommit_pool_rolls_back_terminal_transition_when_enqueue_fails() -> None:
    suffix = secrets.token_hex(6)
    task_table = f"test_dtasks_{suffix}"
    outbox_table = f"test_task_outbox_{suffix}"
    async with psycopg_pool.AsyncConnectionPool(
        TEST_URL,
        kwargs={"autocommit": True},
        open=False,
    ) as pool:
        await pool.open()
        async with pool.connection() as conn:
            assert conn.autocommit is True
        outbox = PgTaskWebhookOutbox(
            pool=pool,
            sender=_sender(),
            encryption_key=b"e" * 32,
            delivery_retry_horizon_seconds=86_400,
            table=outbox_table,
        )
        registry = PgTaskRegistry(
            pool=pool,
            task_webhook_outbox=outbox,
            _table=task_table,
        )
        await registry.create_schema()
        try:
            task_id = await registry.issue(
                account_id="acct_1",
                task_type="create_media_buy",
                webhook_url="https://buyer.example/webhook",
                webhook_operation_id="op_1",
            )
            with pytest.raises(psycopg.errors.UndefinedTable):
                await registry.complete(task_id, {"media_buy_id": "mb_1"})
            record = await registry.get(task_id)
            assert record is not None
            assert record["state"] == "submitted"
        finally:
            async with pool.connection() as conn:
                await conn.execute(f"DROP TABLE IF EXISTS {task_table}")  # noqa: S608
