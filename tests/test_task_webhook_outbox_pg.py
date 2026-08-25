"""Unit coverage for the atomic PostgreSQL task-webhook outbox."""

from __future__ import annotations

import asyncio
import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from adcp.webhook_sender import (
    PreparedWebhook,
    ScopePermanentlyUnknown,
    ScopeTransientlyUnavailable,
    WebhookDeliveryResult,
    WebhookSenderResolution,
)


def _cursor(value: Any = None) -> AsyncMock:
    cursor = AsyncMock()
    cursor.fetchone = AsyncMock(return_value=value)
    return cursor


def _connection(*values: Any) -> AsyncMock:
    conn = AsyncMock()
    conn.execute = AsyncMock(side_effect=[_cursor(value) for value in values])
    transaction_context = AsyncMock()
    transaction_context.__aenter__ = AsyncMock(return_value=None)
    transaction_context.__aexit__ = AsyncMock(return_value=False)
    conn.transaction = MagicMock(return_value=transaction_context)
    return conn


def _pool(*connections: AsyncMock) -> MagicMock:
    contexts = []
    for conn in connections:
        context = AsyncMock()
        context.__aenter__ = AsyncMock(return_value=conn)
        context.__aexit__ = AsyncMock(return_value=False)
        contexts.append(context)
    pool = MagicMock()
    pool.connection = MagicMock(side_effect=contexts)
    return pool


def _sender() -> MagicMock:
    sender = MagicMock()
    sender.signs_with_rfc9421 = True
    sender._owns_client = True
    sender._allow_private_destinations = False
    sender._timeout = 10.0
    sender._auth.alg = "ed25519"
    body = json.dumps(
        {
            "idempotency_key": "whk_1234567890123456",
            "task_id": "task_1",
            "task_type": "create_media_buy",
            "status": "completed",
            "operation_id": "op_1",
        }
    ).encode()
    sender.prepare_mcp = MagicMock(
        return_value=PreparedWebhook(
            url="https://buyer.example/webhook",
            idempotency_key="whk_1234567890123456",
            body=body,
        )
    )
    sender.send_prepared = AsyncMock(
        return_value=WebhookDeliveryResult(
            status_code=200,
            idempotency_key="whk_1234567890123456",
            url="https://buyer.example/webhook",
            response_headers={},
            response_body=b"{}",
            sent_body=body,
        )
    )
    return sender


def _resolution(
    sender: Any,
    algorithms: frozenset[str] = frozenset({"ed25519"}),
) -> WebhookSenderResolution:
    return WebhookSenderResolution(sender=sender, advertised_algorithms=algorithms)


@pytest.mark.parametrize(
    "algorithms",
    [frozenset(), frozenset({"future-secret-algorithm"})],
)
def test_sender_resolution_requires_a_safe_advertised_algorithm_set(
    algorithms: frozenset[str],
) -> None:
    with pytest.raises(ValueError, match="advertised_algorithms"):
        _resolution(_sender(), algorithms)


def _outbox(pool: Any, sender: Any, **kwargs: Any) -> Any:
    from adcp.decisioning.pg.task_webhook_outbox import PgTaskWebhookOutbox

    with patch("adcp.decisioning.pg.task_webhook_outbox.PG_AVAILABLE", True):
        return PgTaskWebhookOutbox(
            pool=pool,
            sender=sender,
            encryption_key=b"e" * 32,
            delivery_retry_horizon_seconds=86_400,
            **kwargs,
        )


def _resolver_outbox(pool: Any, resolver: Any, **kwargs: Any) -> Any:
    from adcp.decisioning.pg.task_webhook_outbox import PgTaskWebhookOutbox

    with patch("adcp.decisioning.pg.task_webhook_outbox.PG_AVAILABLE", True):
        return PgTaskWebhookOutbox(
            pool=pool,
            sender_resolver=resolver,
            encryption_key=b"e" * 32,
            delivery_retry_horizon_seconds=86_400,
            **kwargs,
        )


def test_prepare_mcp_binds_operation_id_key_and_body_before_delivery() -> None:
    from adcp.webhook_sender import WebhookSender

    sender = WebhookSender.from_bearer_token("test-token")
    prepared = sender.prepare_mcp(
        url="https://buyer.example/webhook",
        task_id="task_1",
        task_type="create_media_buy",
        status="completed",
        result={"media_buy_id": "mb_1"},
        operation_id="op_1",
        idempotency_key="whk_1234567890123456",
    )

    payload = json.loads(prepared.body)
    assert prepared.idempotency_key == "whk_1234567890123456"
    assert payload["idempotency_key"] == prepared.idempotency_key
    assert payload["operation_id"] == "op_1"
    assert payload["task_id"] == "task_1"


@pytest.mark.asyncio
async def test_send_prepared_rejects_mismatched_body_binding_before_http() -> None:
    from adcp.webhook_sender import WebhookSender

    sender = WebhookSender.from_bearer_token("test-token")
    with pytest.raises(ValueError, match="immutable binding"):
        await sender.send_prepared(
            PreparedWebhook(
                url="https://buyer.example/webhook",
                idempotency_key="whk_expected_123456",
                body=b'{"idempotency_key":"whk_other_12345678"}',
            )
        )


def test_outbox_rejects_unadvertisable_horizon() -> None:
    from adcp.decisioning.pg.task_webhook_outbox import PgTaskWebhookOutbox

    with (
        patch("adcp.decisioning.pg.task_webhook_outbox.PG_AVAILABLE", True),
        pytest.raises(ValueError, match="86400 through 604800"),
    ):
        PgTaskWebhookOutbox(
            pool=MagicMock(),
            sender=_sender(),
            encryption_key=b"e" * 32,
            delivery_retry_horizon_seconds=3600,
        )


def test_outbox_rejects_sender_without_sdk_pinned_transport() -> None:
    from adcp.decisioning.pg.task_webhook_outbox import PgTaskWebhookOutbox

    sender = _sender()
    sender._owns_client = False
    with (
        patch("adcp.decisioning.pg.task_webhook_outbox.PG_AVAILABLE", True),
        pytest.raises(ValueError, match="IP-pinned transport"),
    ):
        PgTaskWebhookOutbox(
            pool=MagicMock(),
            sender=sender,
            encryption_key=b"e" * 32,
            delivery_retry_horizon_seconds=86_400,
        )


def test_outbox_requires_exactly_one_sender_mode() -> None:
    from adcp.decisioning.pg.task_webhook_outbox import PgTaskWebhookOutbox

    with patch("adcp.decisioning.pg.task_webhook_outbox.PG_AVAILABLE", True):
        with pytest.raises(ValueError, match="exactly one"):
            PgTaskWebhookOutbox(
                pool=MagicMock(),
                encryption_key=b"e" * 32,
                delivery_retry_horizon_seconds=86_400,
            )
        with pytest.raises(ValueError, match="exactly one"):
            PgTaskWebhookOutbox(
                pool=MagicMock(),
                sender=_sender(),
                sender_resolver=MagicMock(),
                encryption_key=b"e" * 32,
                delivery_retry_horizon_seconds=86_400,
            )


def test_scoped_registration_is_encrypted_and_mode_bound() -> None:
    resolver = MagicMock(resolve=AsyncMock())
    outbox = _resolver_outbox(MagicMock(), resolver)
    encrypted, nonce = outbox.protect_registration(
        account_id="acct_1",
        task_id="task_1",
        task_type="create_media_buy",
        url="https://buyer.example/webhook",
        operation_id="op_1",
        token=None,
        signing_scope_id="tenant-key-scope-a",
    )
    assert b"tenant-key-scope-a" not in encrypted
    assert outbox._open_registration_with_scope(
        account_id="acct_1",
        task_id="task_1",
        task_type="create_media_buy",
        encrypted_registration=encrypted,
        nonce=nonce,
    ) == ("https://buyer.example/webhook", "op_1", None, "tenant-key-scope-a")
    assert outbox.open_registration(
        account_id="acct_1",
        task_id="task_1",
        task_type="create_media_buy",
        encrypted_registration=encrypted,
        nonce=nonce,
    ) == ("https://buyer.example/webhook", "op_1", None)

    with pytest.raises(ValueError, match="require a signing_scope_id"):
        outbox.protect_registration(
            account_id="acct_1",
            task_id="task_2",
            task_type="create_media_buy",
            url="https://buyer.example/webhook",
            operation_id="op_2",
            token=None,
        )


@pytest.mark.parametrize("scope", ["", "tenant\nscope", "x" * 256, "é" * 128])
def test_signing_scope_id_is_bounded_and_control_free(scope: str) -> None:
    outbox = _resolver_outbox(MagicMock(), MagicMock(resolve=AsyncMock()))
    with pytest.raises(ValueError, match="signing_scope_id"):
        outbox.protect_registration(
            account_id="acct_1",
            task_id="task_1",
            task_type="create_media_buy",
            url="https://buyer.example/webhook",
            operation_id="op_1",
            token=None,
            signing_scope_id=scope,
        )


def test_fixed_sender_rejects_scoped_registration() -> None:
    outbox = _outbox(MagicMock(), _sender())
    with pytest.raises(ValueError, match="fixed-sender"):
        outbox.protect_registration(
            account_id="acct_1",
            task_id="task_1",
            task_type="create_media_buy",
            url="https://buyer.example/webhook",
            operation_id="op_1",
            token=None,
            signing_scope_id="tenant-a",
        )


def test_registration_is_encrypted_and_bound_at_issue_time() -> None:
    outbox = _outbox(MagicMock(), _sender())
    encrypted, nonce = outbox.protect_registration(
        account_id="acct_1",
        task_id="task_1",
        task_type="create_media_buy",
        url="https://buyer.example/webhook?route=secret",
        operation_id="op_1",
        token="buyer-secret",
    )
    assert b"buyer-secret" not in encrypted
    assert b"route=secret" not in encrypted
    assert outbox.open_registration(
        account_id="acct_1",
        task_id="task_1",
        task_type="create_media_buy",
        encrypted_registration=encrypted,
        nonce=nonce,
    ) == ("https://buyer.example/webhook?route=secret", "op_1", "buyer-secret")

    with pytest.raises(ValueError, match="authenticated decryption"):
        outbox.open_registration(
            account_id="acct_attacker",
            task_id="task_1",
            task_type="create_media_buy",
            encrypted_registration=encrypted,
            nonce=nonce,
        )


@pytest.mark.asyncio
async def test_enqueue_persists_prepared_bytes_and_horizon_on_callers_connection() -> None:
    conn = _connection((41,))
    sender = _sender()
    outbox = _outbox(MagicMock(), sender)

    row_id = await outbox.enqueue_terminal(
        conn,
        task_id="task_1",
        account_id="acct_1",
        task_type="create_media_buy",
        status="completed",
        result={"media_buy_id": "mb_1"},
        url="https://buyer.example/webhook",
        operation_id="op_1",
        token="buyer-token",
    )

    assert row_id == 41
    sender.prepare_mcp.assert_called_once_with(
        url="https://buyer.example/webhook",
        task_id="task_1",
        task_type="create_media_buy",
        status="completed",
        result={"media_buy_id": "mb_1"},
        operation_id="op_1",
        token="buyer-token",
    )
    params = conn.execute.await_args.args[1]
    assert "retry_horizon_seconds" in conn.execute.await_args.args[0]
    assert "retry_until" not in conn.execute.await_args.args[0]
    assert params[5] == "whk_1234567890123456"
    assert params[6] is None
    assert params[7] == "acct_1"
    assert params[8] != sender.prepare_mcp.return_value.body
    assert params[-1] == 86_400


@pytest.mark.asyncio
async def test_worker_recovers_after_transient_iteration_failure() -> None:
    outbox = _outbox(MagicMock(), _sender())
    outbox.purge_expired = AsyncMock()
    outbox.process_one = AsyncMock(
        side_effect=[RuntimeError("database restart"), asyncio.CancelledError()]
    )

    with pytest.raises(asyncio.CancelledError):
        await outbox.run_worker(poll_interval=0.001)

    assert outbox.process_one.await_count == 2
    assert outbox._worker_started is False


@pytest.mark.asyncio
async def test_worker_claims_outside_http_and_acknowledges_success() -> None:
    sender = _sender()
    body = sender.prepare_mcp.return_value.body
    outbox = _outbox(MagicMock(), sender)
    nonce = b"n" * 12
    encrypted = outbox._cipher.encrypt(
        nonce,
        body,
        outbox._envelope_aad(
            account_id="acct_1",
            task_id="task_1",
            task_type="create_media_buy",
            status="completed",
            url="https://buyer.example/webhook",
            operation_id="op_1",
            idempotency_key="whk_1234567890123456",
        ),
    )
    claim_conn = _connection(
        None,
        (
            7,
            "acct_1",
            "task_1",
            "create_media_buy",
            "completed",
            "https://buyer.example/webhook",
            "op_1",
            "whk_1234567890123456",
            None,
            encrypted,
            nonce,
            1,
        ),
    )
    ack_conn = _connection(None)
    outbox._pool = _pool(claim_conn, ack_conn)

    assert await outbox.process_one() is True

    sender.send_prepared.assert_awaited_once()
    prepared = sender.send_prepared.await_args.args[0]
    assert prepared.body == body
    assert prepared.idempotency_key == "whk_1234567890123456"
    assert "state = 'delivered'" in ack_conn.execute.await_args.args[0]


@pytest.mark.asyncio
async def test_resolver_selects_fresh_sender_each_attempt_without_body_drift() -> None:
    first_sender = _sender()
    first_sender.send_prepared.return_value = WebhookDeliveryResult(
        status_code=503,
        idempotency_key="whk_1234567890123456",
        url="https://buyer.example/webhook",
        response_headers={},
        response_body=b"retry",
        sent_body=first_sender.prepare_mcp.return_value.body,
    )
    rotated_sender = _sender()
    resolver = MagicMock(
        resolve=AsyncMock(side_effect=[_resolution(first_sender), _resolution(rotated_sender)])
    )
    outbox = _resolver_outbox(MagicMock(), resolver)
    body = first_sender.prepare_mcp.return_value.body
    nonce = b"s" * 12
    encrypted = outbox._cipher.encrypt(
        nonce,
        body,
        outbox._envelope_aad(
            account_id="acct_1",
            task_id="task_1",
            task_type="create_media_buy",
            status="completed",
            url="https://buyer.example/webhook",
            operation_id="op_1",
            idempotency_key="whk_1234567890123456",
            signing_scope_id="tenant-a",
        ),
    )

    def claim(attempt: int) -> tuple[Any, ...]:
        return (
            7,
            "acct_1",
            "task_1",
            "create_media_buy",
            "completed",
            "https://buyer.example/webhook",
            "op_1",
            "whk_1234567890123456",
            "tenant-a",
            encrypted,
            nonce,
            attempt,
        )

    first_claim = _connection(None, claim(1))
    first_release = _connection(None)
    second_claim = _connection(None, claim(2))
    second_ack = _connection(None)
    outbox._pool = _pool(first_claim, first_release, second_claim, second_ack)

    assert await outbox.process_one() is True
    assert await outbox.process_one() is True

    assert resolver.resolve.await_args_list[0].args == ("tenant-a",)
    assert resolver.resolve.await_args_list[1].args == ("tenant-a",)
    first_prepared = first_sender.send_prepared.await_args.args[0]
    rotated_prepared = rotated_sender.send_prepared.await_args.args[0]
    assert first_prepared.body == rotated_prepared.body == body
    assert first_prepared.idempotency_key == rotated_prepared.idempotency_key
    assert "state = CASE" in first_release.execute.await_args.args[0]
    assert "state = 'delivered'" in second_ack.execute.await_args.args[0]


@pytest.mark.asyncio
async def test_signing_scope_column_is_authenticated_before_resolution() -> None:
    resolver = MagicMock(resolve=AsyncMock(return_value=_resolution(_sender())))
    outbox = _resolver_outbox(MagicMock(), resolver)
    body = _sender().prepare_mcp.return_value.body
    nonce = b"s" * 12
    encrypted = outbox._cipher.encrypt(
        nonce,
        body,
        outbox._envelope_aad(
            account_id="acct_1",
            task_id="task_1",
            task_type="create_media_buy",
            status="completed",
            url="https://buyer.example/webhook",
            operation_id="op_1",
            idempotency_key="whk_1234567890123456",
            signing_scope_id="tenant-a",
        ),
    )
    claim_conn = _connection(
        None,
        (
            7,
            "acct_1",
            "task_1",
            "create_media_buy",
            "completed",
            "https://buyer.example/webhook",
            "op_1",
            "whk_1234567890123456",
            "tenant-b",  # DB-level scope substitution
            encrypted,
            nonce,
            1,
        ),
    )
    quarantine_conn = _connection(None)
    outbox._pool = _pool(claim_conn, quarantine_conn)

    assert await outbox.process_one() is True
    resolver.resolve.assert_not_awaited()
    assert "state = 'invalid'" in quarantine_conn.execute.await_args.args[0]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("resolution_error", "expected_sql"),
    [
        (ScopeTransientlyUnavailable(), "state = CASE"),
        (ScopePermanentlyUnknown(), "state = 'invalid'"),
    ],
)
async def test_scope_resolution_errors_retry_or_quarantine(
    resolution_error: Exception,
    expected_sql: str,
) -> None:
    resolver = MagicMock(resolve=AsyncMock(side_effect=resolution_error))
    outbox = _resolver_outbox(MagicMock(), resolver)
    body = _sender().prepare_mcp.return_value.body
    nonce = b"s" * 12
    encrypted = outbox._cipher.encrypt(
        nonce,
        body,
        outbox._envelope_aad(
            account_id="acct_1",
            task_id="task_1",
            task_type="create_media_buy",
            status="completed",
            url="https://buyer.example/webhook",
            operation_id="op_1",
            idempotency_key="whk_1234567890123456",
            signing_scope_id="tenant-a",
        ),
    )
    claim_conn = _connection(
        None,
        (
            7,
            "acct_1",
            "task_1",
            "create_media_buy",
            "completed",
            "https://buyer.example/webhook",
            "op_1",
            "whk_1234567890123456",
            "tenant-a",
            encrypted,
            nonce,
            1,
        ),
    )
    settle_conn = _connection(None)
    outbox._pool = _pool(claim_conn, settle_conn)

    assert await outbox.process_one() is True
    assert expected_sql in settle_conn.execute.await_args.args[0]


@pytest.mark.asyncio
async def test_sender_resolution_is_bounded_by_the_delivery_lease() -> None:
    async def never_resolves(_scope: str) -> Any:
        await asyncio.Event().wait()

    resolver = MagicMock(resolve=AsyncMock(side_effect=never_resolves))
    outbox = _resolver_outbox(MagicMock(), resolver)
    # Exercise the real aggregate lease timeout without making the test wait.
    outbox._lease_seconds = 1.01
    body = _sender().prepare_mcp.return_value.body
    nonce = b"s" * 12
    encrypted = outbox._cipher.encrypt(
        nonce,
        body,
        outbox._envelope_aad(
            account_id="acct_1",
            task_id="task_1",
            task_type="create_media_buy",
            status="completed",
            url="https://buyer.example/webhook",
            operation_id="op_1",
            idempotency_key="whk_1234567890123456",
            signing_scope_id="tenant-a",
        ),
    )
    claim_conn = _connection(
        None,
        (
            7,
            "acct_1",
            "task_1",
            "create_media_buy",
            "completed",
            "https://buyer.example/webhook",
            "op_1",
            "whk_1234567890123456",
            "tenant-a",
            encrypted,
            nonce,
            1,
        ),
    )
    release_conn = _connection(None)
    outbox._pool = _pool(claim_conn, release_conn)

    assert await asyncio.wait_for(outbox.process_one(), timeout=0.25) is True
    resolver.resolve.assert_awaited_once_with("tenant-a")
    assert "state = CASE" in release_conn.execute.await_args.args[0]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "invalid_mutation",
    [
        lambda sender: setattr(sender, "_owns_client", False),
        lambda sender: setattr(sender, "_allow_private_destinations", True),
        lambda sender: setattr(sender, "signs_with_rfc9421", False),
        lambda sender: setattr(sender, "_timeout", 60.0),
    ],
)
async def test_resolved_sender_is_revalidated_on_every_attempt(invalid_mutation) -> None:
    sender = _sender()
    invalid_mutation(sender)
    outbox = _resolver_outbox(
        MagicMock(), MagicMock(resolve=AsyncMock(return_value=_resolution(sender)))
    )

    with pytest.raises(ScopePermanentlyUnknown):
        await outbox._resolve_delivery_sender("tenant-a")


@pytest.mark.asyncio
async def test_resolved_sender_algorithm_must_match_scope_advertisement() -> None:
    sender = _sender()
    outbox = _resolver_outbox(
        MagicMock(),
        MagicMock(
            resolve=AsyncMock(return_value=_resolution(sender, frozenset({"ecdsa-p256-sha256"})))
        ),
    )

    with pytest.raises(ScopePermanentlyUnknown):
        await outbox._resolve_delivery_sender("tenant-a")
    sender.send_prepared.assert_not_awaited()


@pytest.mark.asyncio
async def test_untyped_resolver_failure_is_sanitized_as_transient() -> None:
    resolver = MagicMock(resolve=AsyncMock(side_effect=RuntimeError("key vault secret")))
    outbox = _resolver_outbox(MagicMock(), resolver)

    with pytest.raises(ScopeTransientlyUnavailable) as exc_info:
        await outbox._resolve_delivery_sender("tenant-a")
    assert "key vault secret" not in str(exc_info.value)


@pytest.mark.asyncio
async def test_worker_releases_failed_delivery_for_horizon_retry() -> None:
    sender = _sender()
    sender.send_prepared.return_value = WebhookDeliveryResult(
        status_code=503,
        idempotency_key="whk_1234567890123456",
        url="https://buyer.example/webhook",
        response_headers={},
        response_body=b"try later",
        sent_body=sender.prepare_mcp.return_value.body,
    )
    outbox = _outbox(MagicMock(), sender)
    nonce = b"n" * 12
    encrypted = outbox._cipher.encrypt(
        nonce,
        sender.prepare_mcp.return_value.body,
        outbox._envelope_aad(
            account_id="acct_1",
            task_id="task_1",
            task_type="create_media_buy",
            status="completed",
            url="https://buyer.example/webhook",
            operation_id="op_1",
            idempotency_key="whk_1234567890123456",
        ),
    )
    claim_conn = _connection(
        None,
        (
            7,
            "acct_1",
            "task_1",
            "create_media_buy",
            "completed",
            "https://buyer.example/webhook",
            "op_1",
            "whk_1234567890123456",
            None,
            encrypted,
            nonce,
            1,
        ),
    )
    release_conn = _connection(None)
    outbox._pool = _pool(claim_conn, release_conn)

    assert await outbox.process_one() is True

    sql, params = release_conn.execute.await_args.args
    assert "state = CASE" in sql
    assert params[1] == 503
    assert params[3] == 7


@pytest.mark.asyncio
async def test_worker_quarantines_body_that_breaks_authenticated_envelope() -> None:
    sender = _sender()
    body = sender.prepare_mcp.return_value.body
    claim_conn = _connection(
        None,
        (
            7,
            "acct_1",
            "task_1",
            "create_media_buy",
            "completed",
            "https://buyer.example/webhook",
            "op_1",
            "whk_1234567890123456",
            None,
            body,
            b"n" * 12,
            1,
        ),
    )
    quarantine_conn = _connection(None)
    outbox = _outbox(_pool(claim_conn, quarantine_conn), sender)

    assert await outbox.process_one() is True

    sender.send_prepared.assert_not_awaited()
    sql, params = quarantine_conn.execute.await_args.args
    assert "state = 'invalid'" in sql
    assert "authenticated binding" in params[0]


@pytest.mark.asyncio
async def test_registry_completion_enqueues_on_same_transaction_connection() -> None:
    from adcp.decisioning.pg.task_registry import PgTaskRegistry

    conn = _connection(
        ("create_media_buy",),
        (
            "task_1",
            "acct_1",
            "create_media_buy",
            b"encrypted-registration",
            b"registration-nonce",
        ),
        None,
    )
    outbox = AsyncMock()
    outbox._sender_resolver = None
    outbox._open_registration_with_scope = MagicMock(
        return_value=(
            "https://buyer.example/webhook",
            "op_1",
            "buyer-token",
            None,
        )
    )
    pool = _pool(conn)
    outbox._pool = pool
    with patch("adcp.decisioning.pg.task_registry.PG_AVAILABLE", True):
        registry = PgTaskRegistry(
            pool=pool,
            task_webhook_outbox=outbox,
        )

    await registry.complete("task_1", {"media_buy_id": "mb_1"})

    outbox.enqueue_terminal.assert_awaited_once_with(
        conn,
        task_id="task_1",
        account_id="acct_1",
        task_type="create_media_buy",
        status="completed",
        result={"media_buy_id": "mb_1"},
        url="https://buyer.example/webhook",
        operation_id="op_1",
        token="buyer-token",
        signing_scope_id=None,
    )
    outbox._open_registration_with_scope.assert_called_once_with(
        account_id="acct_1",
        task_id="task_1",
        task_type="create_media_buy",
        encrypted_registration=b"encrypted-registration",
        nonce=b"registration-nonce",
    )
    conn.transaction.assert_called_once_with()
    assert "webhook_registration = NULL" in conn.execute.await_args_list[-1].args[0]


@pytest.mark.asyncio
async def test_registry_failure_enqueues_on_same_explicit_transaction() -> None:
    from adcp.decisioning.pg.task_registry import PgTaskRegistry

    conn = _connection(
        (
            "task_1",
            "acct_1",
            "create_media_buy",
            b"encrypted-registration",
            b"registration-nonce",
        ),
        None,
    )
    outbox = AsyncMock()
    outbox._sender_resolver = None
    outbox._open_registration_with_scope = MagicMock(
        return_value=(
            "https://buyer.example/webhook",
            "op_1",
            None,
            None,
        )
    )
    pool = _pool(conn)
    outbox._pool = pool
    with patch("adcp.decisioning.pg.task_registry.PG_AVAILABLE", True):
        registry = PgTaskRegistry(pool=pool, task_webhook_outbox=outbox)

    error = {"code": "INTERNAL_ERROR", "message": "failed"}
    await registry.fail("task_1", error)

    outbox.enqueue_terminal.assert_awaited_once_with(
        conn,
        task_id="task_1",
        account_id="acct_1",
        task_type="create_media_buy",
        status="failed",
        result=error,
        url="https://buyer.example/webhook",
        operation_id="op_1",
        token=None,
        signing_scope_id=None,
    )
    conn.transaction.assert_called_once_with()
    assert "webhook_registration = NULL" in conn.execute.await_args_list[-1].args[0]


@pytest.mark.asyncio
async def test_registry_derives_scope_only_from_hydrated_request_context() -> None:
    from adcp.decisioning import RequestContext
    from adcp.decisioning.pg.task_registry import PgTaskRegistry

    pool = MagicMock()
    resolver_outbox = _resolver_outbox(pool, MagicMock(resolve=AsyncMock()))
    scope_hook = AsyncMock(return_value="opaque-tenant-scope")
    with patch("adcp.decisioning.pg.task_registry.PG_AVAILABLE", True):
        registry = PgTaskRegistry(
            pool=pool,
            task_webhook_outbox=resolver_outbox,
            webhook_signing_scope_resolver=scope_hook,
        )
    context = RequestContext(tenant_id="internal-tenant")

    assert await registry.resolve_webhook_signing_scope(context) == "opaque-tenant-scope"
    scope_hook.assert_awaited_once_with(context)


def test_registry_requires_scope_hook_exactly_for_resolver_outbox() -> None:
    from adcp.decisioning.pg.task_registry import PgTaskRegistry

    pool = MagicMock()
    resolver_outbox = _resolver_outbox(pool, MagicMock(resolve=AsyncMock()))
    fixed_outbox = _outbox(pool, _sender())
    with patch("adcp.decisioning.pg.task_registry.PG_AVAILABLE", True):
        with pytest.raises(ValueError, match="required exactly"):
            PgTaskRegistry(pool=pool, task_webhook_outbox=resolver_outbox)
        with pytest.raises(ValueError, match="required exactly"):
            PgTaskRegistry(
                pool=pool,
                task_webhook_outbox=fixed_outbox,
                webhook_signing_scope_resolver=lambda _context: "scope",
            )


@pytest.mark.asyncio
async def test_registry_strips_credentials_before_terminal_update() -> None:
    from adcp.decisioning.pg.task_registry import PgTaskRegistry

    conn = _connection(
        ("sync_accounts",),
        ("task_1", "acct_1", "sync_accounts", None, None),
    )
    pool = _pool(conn)
    with patch("adcp.decisioning.pg.task_registry.PG_AVAILABLE", True):
        registry = PgTaskRegistry(pool=pool)
    result = {
        "accounts": [
            {
                "account_id": "acct_1",
                "notification_configs": [
                    {
                        "authentication": {
                            "schemes": ["Bearer"],
                            "credentials": "secret-that-must-not-enter-wal",
                        }
                    }
                ],
            }
        ]
    }

    await registry.complete("task_1", result)

    persisted = json.loads(conn.execute.await_args_list[1].args[1][0])
    assert "secret-that-must-not-enter-wal" not in str(persisted)
    conn.transaction.assert_called_once_with()


def test_registry_rejects_outbox_on_different_pool() -> None:
    from adcp.decisioning.pg.task_registry import PgTaskRegistry

    outbox = MagicMock()
    outbox._pool = MagicMock()
    with (
        patch("adcp.decisioning.pg.task_registry.PG_AVAILABLE", True),
        pytest.raises(ValueError, match="same connection pool"),
    ):
        PgTaskRegistry(pool=MagicMock(), task_webhook_outbox=outbox)


def test_delivery_status_classification_treats_conflict_as_permanent() -> None:
    from adcp.decisioning.pg.task_webhook_outbox import PgTaskWebhookOutbox

    assert PgTaskWebhookOutbox._is_retryable_http_status(409) is False
    assert PgTaskWebhookOutbox._is_retryable_http_status(429) is True
    assert PgTaskWebhookOutbox._is_retryable_http_status(503) is True


def test_dns_resolution_failure_is_marked_transient() -> None:
    from adcp.signing.jwks import SSRFValidationError

    assert SSRFValidationError("blocked private IP").transient is False
    assert SSRFValidationError("resolver unavailable", transient=True).transient is True
