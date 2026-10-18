"""Real PostgreSQL evidence for the sibling notification outbox.

ADCP_PG_TEST_URL must point to a private test database, as in the CI pg lane.
"""

from __future__ import annotations

import asyncio
import base64
import inspect
import json
import logging
import os
import secrets
from collections.abc import AsyncIterator
from typing import Any, NoReturn
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

psycopg_pool = pytest.importorskip("psycopg_pool")
TEST_URL = os.environ.get("ADCP_PG_TEST_URL")
if not TEST_URL:
    pytest.skip("ADCP_PG_TEST_URL not set", allow_module_level=True)

from adcp.notification_outbox_pg import PgNotificationOutbox  # noqa: E402
from adcp.webhook_supervisor import RetryPolicy  # noqa: E402
from adcp.webhooks import (  # noqa: E402
    PreparedWebhook,
    ScopePermanentlyUnknown,
    ScopeTransientlyUnavailable,
    WebhookDeliveryResult,
    WebhookSender,
    WebhookSenderResolution,
)

URL = "https://buyer.example/hooks"
KEY = "whk_notification_123456789"
AT = "2026-10-02T12:00:00Z"


def prepared(kind: str = "principal.changed", *, key: str = KEY, url: str = URL) -> PreparedWebhook:
    payload = {
        "notification_id": "transition_1",
        "subscriber_id": "buyer",
        "fired_at": AT,
        "agent_url": "https://seller.example",
        "changed_at": AT,
        "reason": "other",
    }
    if kind == "account.status_changed":
        return WebhookSender.prepare_account_status_changed(
            url=url,
            idempotency_key=key,
            payload={
                "notification_id": "transition_1",
                "subscriber_id": "buyer",
                "fired_at": AT,
                "account_id": "acct_1",
                "previous_status": "pending_approval",
                "status": "active",
                "observed_at": AT,
                "reason_code": "seller_approved",
            },
        )
    if kind == "capabilities.changed":
        return WebhookSender.prepare_capabilities_changed(
            url=url, idempotency_key=key, payload={**payload, "capabilities_version": "v2"}
        )
    if kind == "creative.purged":
        return WebhookSender.prepare_creative_purged(
            url=url,
            idempotency_key=key,
            payload={
                "notification_id": "transition_1",
                "subscriber_id": "buyer",
                "fired_at": AT,
                "account_id": "acct_1",
                "creative_id": "cr_1",
                "purge_kind": "hard",
                "purged_at": AT,
                "reason_code": "legal_erasure",
                "initiator": "seller",
            },
        )
    if kind == "revocation-notification":
        return WebhookSender.prepare_revocation_notification(
            url=url,
            idempotency_key=key,
            rights_id="right_1",
            brand_id="brand_1",
            reason="ended",
            effective_at=AT,
        )
    if kind == "artifact-webhook":
        return WebhookSender.prepare_artifact_webhook(
            url=url,
            idempotency_key=key,
            media_buy_id="mb_1",
            batch_id="batch_1",
            timestamp=AT,
            artifacts=[],
        )
    return WebhookSender.prepare_principal_changed(url=url, idempotency_key=key, payload=payload)


def sender_mock() -> MagicMock:
    sender = MagicMock()
    sender._owns_client = True
    sender._allow_private_destinations = False
    sender._timeout = 10
    sender.signs_with_rfc9421 = True
    sender._auth.alg = "ed25519"

    async def send(value: PreparedWebhook, *, before_attempt: Any = None) -> WebhookDeliveryResult:
        assert before_attempt is None or await before_attempt()
        return WebhookDeliveryResult(
            status_code=200,
            idempotency_key=value.idempotency_key,
            url=value.url,
            response_headers={},
            response_body=b"{}",
            sent_body=value.body,
        )

    sender.send_prepared = AsyncMock(side_effect=send)
    return sender


@pytest.fixture
async def stack() -> AsyncIterator[tuple[Any, PgNotificationOutbox, MagicMock]]:
    async with psycopg_pool.AsyncConnectionPool(
        TEST_URL, min_size=1, max_size=8, open=False
    ) as pool:
        sender = sender_mock()
        outbox = PgNotificationOutbox(
            pool=pool,
            sender=sender,
            encryption_key=b"x" * 32,
            delivery_retry_horizon_seconds=86400,
            table="test_notifications_" + secrets.token_hex(6),
            retry=RetryPolicy(base_delay_seconds=0.01, max_delay_seconds=0.01, jitter=False),
        )
        await outbox.create_schema()
        await outbox.create_schema()
        try:
            yield pool, outbox, sender
        finally:
            async with pool.connection() as conn:
                await conn.execute(f"DROP TABLE {outbox._table}")


async def enqueue(
    pool: Any,
    outbox: PgNotificationOutbox,
    value: PreparedWebhook | None = None,
    *,
    kind: str = "principal.changed",
    caller: str = "caller_1",
    account: str | None = None,
    scope: str | None = None,
) -> int:
    async with pool.connection() as conn:
        async with conn.transaction():
            return await outbox.enqueue_prepared(
                conn,
                value or prepared(kind),
                notification_type=kind,
                caller_scope_id=caller,
                account_id=account,
                signing_scope_id=scope,
            )


async def row(pool: Any, outbox: PgNotificationOutbox, row_id: int) -> dict[str, Any]:
    async with pool.connection() as conn:
        from psycopg.rows import dict_row

        cursor = conn.cursor(row_factory=dict_row)
        await cursor.execute(f"SELECT * FROM {outbox._table} WHERE id=%s", (row_id,))
        return dict(await cursor.fetchone())


async def update(
    pool: Any,
    outbox: PgNotificationOutbox,
    row_id: int,
    assignment: str,
    args: tuple[Any, ...] = (),
) -> None:
    async with pool.connection() as conn:
        await conn.execute(f"UPDATE {outbox._table} SET {assignment} WHERE id=%s", (*args, row_id))


async def test_rollback_and_open_transaction_contract(stack: tuple[Any, ...]) -> None:
    pool, outbox, sender = stack
    with pytest.raises(RuntimeError, match="rollback"):
        async with pool.connection() as conn:
            async with conn.transaction():
                await outbox.enqueue_prepared(
                    conn,
                    prepared(),
                    notification_type="principal.changed",
                    caller_scope_id="caller",
                )
                assert (
                    not await outbox.process_one()
                )  # Other connection cannot see uncommitted work.
                raise RuntimeError("rollback")
    assert not await outbox.process_one()
    sender.send_prepared.assert_not_called()
    async with pool.connection() as conn:
        with pytest.raises(ValueError, match="open business transaction"):
            await outbox.enqueue_prepared(
                conn, prepared(), notification_type="principal.changed", caller_scope_id="caller"
            )


@pytest.mark.parametrize(
    "kind",
    [
        "principal.changed",
        "capabilities.changed",
        "account.status_changed",
        "creative.purged",
        "artifact-webhook",
        "revocation-notification",
    ],
)
async def test_commit_survives_new_worker(stack: tuple[Any, ...], kind: str) -> None:
    pool, outbox, sender = stack
    value = prepared(kind)
    account = "acct_1" if kind.startswith(("account.", "creative.")) else None
    row_id = await enqueue(pool, outbox, value, kind=kind, account=account)
    restarted = PgNotificationOutbox(
        pool=pool,
        sender=sender,
        encryption_key=b"x" * 32,
        delivery_retry_horizon_seconds=86400,
        table=outbox._table,
    )
    assert await restarted.process_one()
    assert (await row(pool, outbox, row_id))["state"] == "delivered"
    sent = sender.send_prepared.call_args.args[0]
    assert sent == value
    assert not await outbox.process_one()


async def test_transient_retry_and_lease_recovery(stack: tuple[Any, ...]) -> None:
    pool, outbox, sender = stack
    row_id = await enqueue(pool, outbox)
    normal = sender.send_prepared.side_effect
    sender.send_prepared.side_effect = httpx.ConnectError("secret diagnostic")
    assert await outbox.process_one()
    stored = await row(pool, outbox, row_id)
    assert stored["state"] == "pending"
    assert stored["last_error"] == "ConnectError"
    assert "secret" not in stored["last_error"]
    # Model a worker killed after committing its claim. Lease recovery is DB-driven.
    await update(
        pool,
        outbox,
        row_id,
        "state='in_flight', lease_token='dead-worker', lease_expires_at=now()-interval '1 second'",
    )
    sender.send_prepared.side_effect = normal
    assert await outbox.process_one()
    assert (await row(pool, outbox, row_id))["attempt_count"] == 2
    assert (await row(pool, outbox, row_id))["state"] == "delivered"
    assert (
        sender.send_prepared.call_args_list[0].args[0]
        == sender.send_prepared.call_args_list[1].args[0]
    )


async def test_duplicate_binding_and_independent_callers(stack: tuple[Any, ...]) -> None:
    pool, outbox, sender = stack
    first = await enqueue(pool, outbox)
    assert await enqueue(pool, outbox) == first
    second = await enqueue(pool, outbox, caller="caller_2")
    third = await enqueue(pool, outbox, prepared(url="https://buyer.example/other"))
    fourth = await enqueue(
        pool, outbox, prepared("capabilities.changed"), kind="capabilities.changed"
    )
    fifth = await enqueue(pool, outbox, account="acct_2")
    assert len({first, second, third, fourth, fifth}) == 5
    changed = PreparedWebhook(
        url=URL, idempotency_key=KEY, body=prepared().body, extra_headers={"X-New": "different"}
    )
    with pytest.raises(ValueError, match="different immutable"):
        await enqueue(pool, outbox, changed)
    await asyncio.gather(*(outbox.process_one() for _ in range(5)))
    assert sender.send_prepared.call_count == 5


@pytest.mark.parametrize(
    "column,new_value",
    [
        ("url", "https://attacker.example/hooks"),
        ("notification_type", "capabilities.changed"),
        ("account_id", "other"),
        ("caller_scope_id", "other"),
        ("signing_scope_id", "other"),
        ("idempotency_key", "whk_different_1234567890"),
        ("encryption_key_id", "other"),
        ("dedup_scope", b"z" * 32),
        ("encrypted_body", b"tampered"),
        ("envelope_nonce", b"z" * 12),
    ],
)
async def test_tampering_is_quarantined(
    stack: tuple[Any, ...], column: str, new_value: Any
) -> None:
    pool, outbox, sender = stack
    row_id = await enqueue(pool, outbox)
    await update(pool, outbox, row_id, f"{column}=%s", (new_value,))
    assert await outbox.process_one()
    assert (await row(pool, outbox, row_id))["state"] == "invalid"
    sender.send_prepared.assert_not_called()


async def test_authenticated_body_metadata_mismatch_is_quarantined(stack: tuple[Any, ...]) -> None:
    pool, outbox, sender = stack
    row_id = await enqueue(pool, outbox)
    stored = await row(pool, outbox, row_id)
    wrong = json.loads(prepared().body)
    wrong["idempotency_key"] = "whk_wrong_1234567890000"
    protected = outbox._protected(
        PreparedWebhook(url=URL, idempotency_key=KEY, body=json.dumps(wrong).encode())
    )
    aad = outbox._aad(
        caller=stored["caller_scope_id"],
        account=stored["account_id"],
        url=stored["url"],
        kind=stored["notification_type"],
        key=KEY,
        signing_scope=None,
        encryption_key_id="default",
        dedup_scope=bytes(stored["dedup_scope"]),
    )
    encrypted = outbox._ciphers["default"].encrypt(bytes(stored["envelope_nonce"]), protected, aad)
    await update(pool, outbox, row_id, "encrypted_body=%s", (encrypted,))
    assert await outbox.process_one()
    assert (await row(pool, outbox, row_id))["state"] == "invalid"
    sender.send_prepared.assert_not_called()


async def test_invalid_envelopes_are_rejected_before_insert(stack: tuple[Any, ...]) -> None:
    pool, outbox, _ = stack
    with pytest.raises(ValueError, match="type does not match"):
        await enqueue(pool, outbox, kind="account.status_changed", value=prepared())
    with pytest.raises(ValueError, match="require account_id"):
        await enqueue(pool, outbox, kind="account.status_changed")
    with pytest.raises(ValueError, match="schema validation"):
        await enqueue(
            pool,
            outbox,
            PreparedWebhook(
                url=URL,
                idempotency_key=KEY,
                body=json.dumps(
                    {"idempotency_key": KEY, "notification_type": "principal.changed"}
                ).encode(),
            ),
        )
    assert not await outbox.process_one()


async def test_expiry_and_retention(stack: tuple[Any, ...]) -> None:
    pool, outbox, sender = stack
    row_id = await enqueue(pool, outbox)
    await update(pool, outbox, row_id, "retry_until=now()-interval '1 second'")
    assert not await outbox.process_one()
    assert (await row(pool, outbox, row_id))["state"] == "expired"
    await outbox.purge_expired()
    assert (await row(pool, outbox, row_id))["state"] == "expired"
    await update(pool, outbox, row_id, "retry_until=now()-interval '8 days'")
    await outbox.purge_expired()
    async with pool.connection() as conn:
        assert await (await conn.execute(f"SELECT id FROM {outbox._table}")).fetchone() is None
    sender.send_prepared.assert_not_called()


@pytest.mark.parametrize(
    "status,state",
    [
        (503, "pending"),
        (429, "pending"),
        (408, "pending"),
        (425, "pending"),
        (400, "invalid"),
        (302, "invalid"),
    ],
)
async def test_http_retry_policy(stack: tuple[Any, ...], status: int, state: str) -> None:
    pool, outbox, sender = stack
    row_id = await enqueue(pool, outbox)
    sender.send_prepared.side_effect = None
    sender.send_prepared.return_value = WebhookDeliveryResult(
        status_code=status,
        idempotency_key=KEY,
        url=URL,
        response_headers={},
        response_body=b"secret must not reach row",
    )
    assert await outbox.process_one()
    stored = await row(pool, outbox, row_id)
    assert stored["state"] == state
    assert "secret" not in stored["last_error"]


async def test_resolver_rotation_and_scope_isolation(stack: tuple[Any, ...]) -> None:
    pool, original, _ = stack
    old, new = sender_mock(), sender_mock()
    old.send_prepared.side_effect = ScopeTransientlyUnavailable("secret")
    resolver = MagicMock()
    resolver.resolve = AsyncMock(
        side_effect=[
            WebhookSenderResolution(sender=old, advertised_algorithms=frozenset({"ed25519"})),
            WebhookSenderResolution(sender=new, advertised_algorithms=frozenset({"ed25519"})),
        ]
    )
    outbox = PgNotificationOutbox(
        pool=pool,
        sender_resolver=resolver,
        encryption_key=b"x" * 32,
        delivery_retry_horizon_seconds=86400,
        table=original._table,
        retry=RetryPolicy(base_delay_seconds=0.01, max_delay_seconds=0.01, jitter=False),
    )
    with pytest.raises(ValueError, match="require a signing_scope_id"):
        await enqueue(pool, outbox)
    row_id = await enqueue(pool, outbox, scope="buyer-tenant-1")
    await outbox.process_one()
    await update(pool, outbox, row_id, "available_at=now()")
    await outbox.process_one()
    assert resolver.resolve.call_args_list[0].args == ("buyer-tenant-1",)
    assert resolver.resolve.call_args_list[1].args == ("buyer-tenant-1",)
    assert old.send_prepared.call_args.args[0] == new.send_prepared.call_args.args[0]
    assert (await row(pool, outbox, row_id))["state"] == "delivered"


@pytest.mark.parametrize(
    "failure,state",
    [
        (ScopeTransientlyUnavailable("secret"), "pending"),
        (ScopePermanentlyUnknown("secret"), "invalid"),
        (RuntimeError("secret"), "pending"),
    ],
)
async def test_resolver_failures_are_sanitized(
    stack: tuple[Any, ...], failure: Exception, state: str
) -> None:
    pool, original, _ = stack
    resolver = MagicMock()
    resolver.resolve = AsyncMock(side_effect=failure)
    outbox = PgNotificationOutbox(
        pool=pool,
        sender_resolver=resolver,
        encryption_key=b"x" * 32,
        delivery_retry_horizon_seconds=86400,
        table=original._table,
    )
    row_id = await enqueue(pool, outbox, scope="buyer-1")
    await outbox.process_one()
    stored = await row(pool, outbox, row_id)
    assert stored["state"] == state
    assert "secret" not in stored["last_error"]


async def test_encryption_rotation_preserves_old_rows(stack: tuple[Any, ...]) -> None:
    pool, original, sender = stack
    old_id = await enqueue(pool, original)
    rotated = PgNotificationOutbox(
        pool=pool,
        sender=sender,
        encryption_key=b"y" * 32,
        encryption_key_id="new-key",
        decryption_keys={"default": b"x" * 32},
        delivery_retry_horizon_seconds=86400,
        table=original._table,
    )
    new_id = await enqueue(pool, rotated, prepared(key="whk_notification_new_123456"))
    assert await enqueue(pool, rotated) == old_id  # Existing immutable rows survive key rotation.
    assert (await row(pool, rotated, new_id))["encryption_key_id"] == "new-key"
    assert await rotated.process_one()
    assert await rotated.process_one()
    assert sender.send_prepared.call_count == 2


async def test_real_signing_keeps_body_key_and_headers_on_retry(
    stack: tuple[Any, ...], monkeypatch: pytest.MonkeyPatch
) -> None:
    pool, original, _ = stack
    from adcp.signing import StaticJwksResolver
    from adcp.webhooks import WebhookVerifyOptions, verify_webhook_signature

    requests: list[httpx.Request] = []
    keys = [Ed25519PrivateKey.generate(), Ed25519PrivateKey.generate()]
    jwks = {
        "keys": [
            {
                "kty": "OKP",
                "use": "sig",
                "key_ops": ["verify"],
                "alg": "EdDSA",
                "crv": "Ed25519",
                "kid": f"key-{i}",
                "adcp_use": "webhook-signing",
                "x": base64.urlsafe_b64encode(
                    key.public_key().public_bytes(
                        serialization.Encoding.Raw, serialization.PublicFormat.Raw
                    )
                )
                .decode()
                .rstrip("="),
            }
            for i, key in enumerate(keys)
        ]
    }
    verify_options = WebhookVerifyOptions(jwks_resolver=StaticJwksResolver(jwks))

    def receive(request: httpx.Request) -> httpx.Response:
        verified = verify_webhook_signature(
            method="POST",
            url=str(request.url),
            headers=request.headers,
            body=request.content,
            options=verify_options,
        )
        assert verified.key_id == f"key-{len(requests)}"
        requests.append(request)
        return httpx.Response(503 if len(requests) == 1 else 200)

    monkeypatch.setattr(
        "adcp.webhook_sender.build_async_ip_pinned_transport",
        lambda *a, **k: httpx.MockTransport(receive),
    )
    senders = [
        WebhookSender(private_key=key, key_id=f"key-{i}", alg="ed25519")
        for i, key in enumerate(keys)
    ]
    resolver = MagicMock()
    resolver.resolve = AsyncMock(
        side_effect=[
            WebhookSenderResolution(sender=sender, advertised_algorithms=frozenset({"ed25519"}))
            for sender in senders
        ]
    )
    try:
        outbox = PgNotificationOutbox(
            pool=pool,
            sender_resolver=resolver,
            encryption_key=b"x" * 32,
            delivery_retry_horizon_seconds=86400,
            table=original._table,
            retry=RetryPolicy(base_delay_seconds=0.01, max_delay_seconds=0.01, jitter=False),
        )
        value = PreparedWebhook(
            url=URL,
            idempotency_key=KEY,
            body=prepared().body,
            extra_headers={"X-Credential": "sensitive"},
        )
        row_id = await enqueue(pool, outbox, value, scope="buyer-tenant-1")
        stored = await row(pool, outbox, row_id)
        assert b"sensitive" not in bytes(stored["encrypted_body"])
        assert await outbox.process_one()
        await update(pool, outbox, row_id, "available_at=now()")
        assert await outbox.process_one()
        assert requests[0].content == requests[1].content == value.body
        assert (
            requests[0].headers["X-Credential"]
            == requests[1].headers["X-Credential"]
            == "sensitive"
        )
        assert "signature" in requests[0].headers
        assert "content-digest" in requests[0].headers
    finally:
        for sender in senders:
            await sender.aclose()


async def test_worker_cancel_recovers_lease(stack: tuple[Any, ...]) -> None:
    pool, outbox, sender = stack
    row_id = await enqueue(pool, outbox)
    started = asyncio.Event()
    normal = sender.send_prepared.side_effect

    async def hang(*args: Any, **kwargs: Any) -> None:
        started.set()
        await asyncio.Event().wait()

    sender.send_prepared.side_effect = hang
    worker = asyncio.create_task(outbox.run_worker(poll_interval=0.01))
    await asyncio.wait_for(started.wait(), timeout=5)
    worker.cancel()
    with pytest.raises(asyncio.CancelledError):
        await worker
    assert (await row(pool, outbox, row_id))["state"] == "in_flight"
    await update(pool, outbox, row_id, "lease_expires_at=now()-interval '1 second'")
    sender.send_prepared.side_effect = normal
    assert await outbox.process_one()
    assert (await row(pool, outbox, row_id))["state"] == "delivered"


@pytest.mark.parametrize(
    "change,state",
    [
        ("retry_until=now()-interval '1 second'", "expired"),
        ("lease_token='replacement-worker'", "in_flight"),
    ],
)
async def test_fence_rechecked_before_http(stack: tuple[Any, ...], change: str, state: str) -> None:
    from adcp.webhook_sender import PreparedWebhookAttemptExpiredError

    pool, outbox, sender = stack
    row_id = await enqueue(pool, outbox)
    accepted = []

    async def attempt(value: PreparedWebhook, *, before_attempt: Any) -> None:
        await update(pool, outbox, row_id, change)
        if not await before_attempt():
            raise PreparedWebhookAttemptExpiredError("prepared_attempt_expired")
        accepted.append(value)

    sender.send_prepared.side_effect = attempt
    assert await outbox.process_one()
    assert accepted == []
    assert (await row(pool, outbox, row_id))["state"] == state


async def test_scoped_duplicates_cannot_change_signing_scope(stack: tuple[Any, ...]) -> None:
    pool, original, sender = stack
    resolver = MagicMock()
    resolver.resolve = AsyncMock(
        return_value=WebhookSenderResolution(
            sender=sender, advertised_algorithms=frozenset({"ed25519"})
        )
    )
    outbox = PgNotificationOutbox(
        pool=pool,
        sender_resolver=resolver,
        encryption_key=b"x" * 32,
        delivery_retry_horizon_seconds=86400,
        table=original._table,
    )
    row_id = await enqueue(pool, outbox, scope="buyer-1")
    deadline = (await row(pool, outbox, row_id))["retry_until"]
    assert await enqueue(pool, outbox, scope="buyer-1") == row_id
    assert (await row(pool, outbox, row_id))["retry_until"] == deadline
    with pytest.raises(ValueError, match="different immutable"):
        await enqueue(pool, outbox, scope="buyer-2")
    resolver.resolve.assert_not_called()  # Resolution is never part of the business transaction.


def _raise_sensitive_failure() -> NoReturn:
    credential = "outbox-credential-local-sentinel"
    assert credential
    try:
        raise LookupError("outbox-credential-chain-sentinel")
    except LookupError as cause:
        raise RuntimeError("outbox-credential-args-sentinel") from cause  # outbox-source-sentinel


def assert_safe_failure_diagnostics(caplog: pytest.LogCaptureFixture) -> None:
    records = [r for r in caplog.records if r.name == "adcp.notification_outbox_pg"]
    assert records
    source, first_line = inspect.getsourcelines(_raise_sensitive_failure)
    origin_line = first_line + next(
        i for i, line in enumerate(source) if "raise RuntimeError" in line
    )
    origin = (
        _raise_sensitive_failure.__code__.co_filename,
        "_raise_sensitive_failure",
        origin_line,
    )
    assert any(
        "RuntimeError" in record.getMessage() and str(origin) in record.getMessage()
        for record in records
    )
    for record in records:
        assert record.exc_info is None
        assert record.exc_text is None
        assert record.stack_info is None
        rendered = logging.Formatter("%(message)s").format(record)
        for sentinel in (
            "outbox-credential-args-sentinel",
            "outbox-credential-chain-sentinel",
            "outbox-credential-local-sentinel",
            "outbox-source-sentinel",
        ):
            assert sentinel not in rendered
            assert sentinel not in repr(record.__dict__)
        assert "LookupError" not in rendered
        assert "CancelledError" not in rendered


@pytest.mark.parametrize("boundary", ["delivery", "resolver"])
async def test_unexpected_delivery_diagnostics_preserve_retry(
    stack: tuple[Any, ...], boundary: str, caplog: pytest.LogCaptureFixture
) -> None:
    pool, outbox, sender = stack
    normal = sender.send_prepared.side_effect

    async def fail(*args: Any, **kwargs: Any) -> Any:
        _raise_sensitive_failure()

    scope = None
    if boundary == "resolver":
        resolver = MagicMock()
        resolver.resolve = AsyncMock(side_effect=fail)
        outbox._sender = None
        outbox._sender_resolver = resolver
        scope = "buyer-diagnostics"
    else:
        sender.send_prepared.side_effect = fail
    row_id = await enqueue(pool, outbox, scope=scope)
    original = await row(pool, outbox, row_id)
    with caplog.at_level(logging.ERROR, logger="adcp.notification_outbox_pg"):
        assert await outbox.process_one()
    assert_safe_failure_diagnostics(caplog)
    stored = await row(pool, outbox, row_id)
    assert stored["state"] == "pending"
    assert stored["attempt_count"] == 1
    assert stored["lease_token"] is stored["lease_expires_at"] is None
    assert stored["last_error"] == (
        "ScopeTransientlyUnavailable" if boundary == "resolver" else "RuntimeError"
    )
    assert stored["retry_until"] == original["retry_until"]
    assert stored["encrypted_body"] == original["encrypted_body"]
    assert stored["signing_scope_id"] == scope
    if boundary == "resolver":
        resolver.resolve.side_effect = None
        resolver.resolve.return_value = WebhookSenderResolution(
            sender=sender, advertised_algorithms=frozenset({"ed25519"})
        )
    else:
        sender.send_prepared.side_effect = normal
    await update(pool, outbox, row_id, "available_at=now()")
    assert await outbox.process_one()
    delivered = await row(pool, outbox, row_id)
    assert delivered["state"] == "delivered"
    assert delivered["attempt_count"] == 2
    assert delivered["last_error"] is None
    assert delivered["signing_scope_id"] == scope
    if boundary == "delivery":
        assert (
            sender.send_prepared.call_args_list[0].args[0] == sender.send_prepared.call_args.args[0]
        )
    else:
        assert [call.args for call in resolver.resolve.call_args_list] == [(scope,), (scope,)]


@pytest.mark.parametrize("boundary", ["iteration", "purge", "database_ack"])
async def test_worker_failure_diagnostics_preserve_recovery(
    stack: tuple[Any, ...],
    boundary: str,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    from psycopg import AsyncConnection

    pool, outbox, sender = stack
    row_id = await enqueue(pool, outbox)
    original = await row(pool, outbox, row_id)
    recovered = asyncio.Event()
    failed = False
    process_one = outbox.process_one
    purge_expired = outbox.purge_expired
    execute = AsyncConnection.execute

    async def iteration() -> bool:
        nonlocal failed
        if boundary == "iteration" and not failed:
            failed = True
            _raise_sensitive_failure()
        result = await process_one()
        recovered.set()
        return result

    async def purge() -> None:
        nonlocal failed
        if boundary == "purge" and not failed:
            failed = True
            _raise_sensitive_failure()
        await purge_expired()

    async def database_ack(conn: Any, query: Any, *args: Any, **kwargs: Any) -> Any:
        nonlocal failed
        if boundary == "database_ack" and query == outbox._sql_ack and not failed:
            failed = True
            _raise_sensitive_failure()
        return await execute(conn, query, *args, **kwargs)

    monkeypatch.setattr(outbox, "process_one", iteration)
    monkeypatch.setattr(outbox, "purge_expired", purge)
    monkeypatch.setattr(AsyncConnection, "execute", database_ack)
    with caplog.at_level(logging.ERROR, logger="adcp.notification_outbox_pg"):
        worker = asyncio.create_task(outbox.run_worker(poll_interval=0.01))
        try:
            await asyncio.wait_for(recovered.wait(), timeout=5)
        finally:
            worker.cancel()
            with pytest.raises(asyncio.CancelledError):
                await worker
    assert failed
    assert_safe_failure_diagnostics(caplog)
    stored = await row(pool, outbox, row_id)
    assert stored["retry_until"] == original["retry_until"]
    assert stored["encrypted_body"] == original["encrypted_body"]
    assert stored["last_error"] is None  # Iteration diagnostics are never copied to row errors.
    if boundary == "database_ack":
        assert stored["state"] == "in_flight"
        assert stored["lease_token"] and stored["lease_expires_at"]
        await update(pool, outbox, row_id, "lease_expires_at=now()-interval '1 second'")
        assert await process_one()
        delivered = await row(pool, outbox, row_id)
        assert delivered["state"] == "delivered"
        assert delivered["attempt_count"] == 2
        assert (
            sender.send_prepared.call_args_list[0].args[0] == sender.send_prepared.call_args.args[0]
        )
    else:
        assert stored["state"] == "delivered"
        assert stored["attempt_count"] == 1
