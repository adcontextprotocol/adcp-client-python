"""Atomic PostgreSQL publication of immutable, non-task notifications.

Enqueue on the business transaction; run workers separately. HTTP is outside
SQL transactions, so a crash after acceptance can cause an identical retry.
The receiver must deduplicate by the stable key and authenticated sender.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import inspect
import json
import logging
import math
import os
import re
import time
import uuid
from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar

import httpx
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from adcp._notification_envelope import validate_notification_body
from adcp.signing.jwks import SSRFValidationError
from adcp.webhook_auth import merge_extra_headers
from adcp.webhook_sender import (
    PreparedWebhook,
    ScopePermanentlyUnknown,
    ScopeTransientlyUnavailable,
    WebhookDeliveryResult,
    WebhookSender,
    WebhookSenderResolution,
    WebhookSenderResolver,
)
from adcp.webhook_supervisor import RetryPolicy

if TYPE_CHECKING:
    from psycopg import AsyncConnection
    from psycopg_pool import AsyncConnectionPool

DEFAULT_TABLE = "adcp_notification_outbox"
MIN_RETRY_HORIZON_SECONDS = 86_400
MAX_RETRY_HORIZON_SECONDS = 604_800
_SAFE_IDENTIFIER = re.compile(r"^[a-z_][a-z0-9_]{0,44}$")
logger = logging.getLogger(__name__)


def _log_failure(operation: str, exc: Exception) -> None:
    """Expose failure sites without exception text, chains, locals or source lines."""
    locations: list[tuple[str, str, int]] = []
    traceback = exc.__traceback__
    while traceback is not None:
        code = traceback.tb_frame.f_code
        locations.append((code.co_filename, code.co_name, traceback.tb_lineno))
        traceback = traceback.tb_next
    # Pass only primitive metadata to logging; never retain the exception/frame objects.
    logger.error(
        "[adcp.notification_outbox] %s: %s; frames=%s",
        operation,
        type(exc).__name__,
        locations,
    )


def _canonical(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode(
        "utf-8"
    )


def _identifier(value: str, name: str) -> None:
    if (
        not isinstance(value, str)
        or not value
        or not value.isprintable()
        or len(value.encode("utf-8")) > 255
    ):
        raise ValueError(f"{name} must be a non-empty printable string of at most 255 UTF-8 bytes")


class PgNotificationOutbox:
    """Opt-in notification table and lease worker, independent of task storage.

    ``caller_scope_id`` is a trusted publisher identity, not a buyer payload
    field. It separates otherwise identical keys from independent callers.
    Account-less principal/capabilities notifications use ``account_id=None``.
    Resolve signing credentials on every attempt to permit key rotation under
    the same trusted scope. A fixed sender instead requires a null scope.

    Retry horizon (1–7 days) starts at enqueue, including time in the backlog.
    Delivered/expired/invalid rows retain their binding until seven days after
    that deadline. ``RetryPolicy.max_attempts`` does not truncate this horizon.
    """

    delivery_state_is_durable: ClassVar[bool] = True

    def __init__(
        self,
        *,
        pool: AsyncConnectionPool,
        encryption_key: bytes,
        delivery_retry_horizon_seconds: int,
        sender: WebhookSender | None = None,
        sender_resolver: WebhookSenderResolver | None = None,
        retry: RetryPolicy | None = None,
        lease_seconds: int = 60,
        table: str = DEFAULT_TABLE,
        encryption_key_id: str = "default",
        decryption_keys: Mapping[str, bytes] | None = None,
    ) -> None:
        try:
            import psycopg_pool  # noqa: F401
        except ImportError as exc:
            raise ImportError("PgNotificationOutbox requires `pip install 'adcp[pg]'`") from exc
        if (sender is None) == (sender_resolver is None):
            raise ValueError("pass exactly one of sender or sender_resolver")
        if sender_resolver is not None and not inspect.iscoroutinefunction(
            getattr(sender_resolver, "resolve", None)
        ):
            raise ValueError("sender_resolver must define async resolve(signing_scope_id)")
        if (
            type(delivery_retry_horizon_seconds) is not int
            or not MIN_RETRY_HORIZON_SECONDS
            <= delivery_retry_horizon_seconds
            <= MAX_RETRY_HORIZON_SECONDS
        ):
            raise ValueError(
                "delivery_retry_horizon_seconds must be an integer from 86400 through 604800"
            )
        if type(lease_seconds) is not int or lease_seconds <= 1:
            raise ValueError("lease_seconds must be an integer greater than 1")
        if not _SAFE_IDENTIFIER.fullmatch(table):
            raise ValueError("invalid outbox table identifier")
        _identifier(encryption_key_id, "encryption_key_id")
        keys = dict(decryption_keys or {})
        if encryption_key_id in keys and keys[encryption_key_id] != encryption_key:
            raise ValueError("active encryption key conflicts with decryption_keys")
        keys[encryption_key_id] = encryption_key
        for key_id, key in keys.items():
            _identifier(key_id, "encryption_key_id")
            if not isinstance(key, bytes) or len(key) != 32:
                raise ValueError("encryption keys must be exactly 32 bytes for AES-256-GCM")
        self._ciphers = {key_id: AESGCM(key) for key_id, key in keys.items()}
        self._key_id = encryption_key_id
        self._pool = pool
        self._sender = sender
        self._sender_resolver = sender_resolver
        self.delivery_retry_horizon_seconds = delivery_retry_horizon_seconds
        self._lease_seconds = lease_seconds
        if sender is not None:
            self._validate_sender(sender)
        self._retry = retry or RetryPolicy()
        if (
            any(
                not math.isfinite(v) or v <= 0
                for v in (self._retry.base_delay_seconds, self._retry.max_delay_seconds)
            )
            or self._retry.max_delay_seconds < self._retry.base_delay_seconds
        ):
            raise ValueError("retry delays must be finite, positive and ordered")
        self._table = table
        self._columns = (
            "id, account_id, caller_scope_id, notification_type, url, idempotency_key, "
            "signing_scope_id, encryption_key_id, dedup_scope, encrypted_body, envelope_nonce"
        )
        # Only regex-validated ASCII table names and static columns enter SQL.
        # All notification values use psycopg parameters (B608 false positives below).
        self._sql_insert = (  # noqa: S608
            f"INSERT INTO {table} (notification_type, caller_scope_id, account_id, "  # nosec B608
            "url, idempotency_key, "
            "signing_scope_id, encryption_key_id, dedup_scope, encrypted_body, "
            "envelope_nonce, retry_until) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) "
            "ON CONFLICT (dedup_scope, idempotency_key) DO NOTHING RETURNING id"
        )
        self._sql_expire = (  # noqa: S608
            f"WITH expired AS (SELECT id FROM {table}"  # nosec B608
            " WHERE state IN ('pending', 'in_flight') AND retry_until <= now()"
            " ORDER BY id FOR UPDATE SKIP LOCKED LIMIT 1000)"
            f" UPDATE {table} AS outbox SET state = 'expired', lease_token = NULL,"  # nosec B608
            " lease_expires_at = NULL, updated_at = now()"
            " FROM expired WHERE outbox.id = expired.id"
        )
        self._sql_claim = (  # noqa: S608
            f"WITH candidate AS ("
            f" SELECT id FROM {table}"  # nosec B608
            " WHERE retry_until > now() AND ("
            "   (state = 'pending' AND available_at <= now()) OR"
            "   (state = 'in_flight' AND lease_expires_at <= now())"
            " ) ORDER BY available_at, id FOR UPDATE SKIP LOCKED LIMIT 1"
            f") UPDATE {table} AS outbox SET"  # nosec B608
            " state = 'in_flight', lease_token = %s,"
            " lease_expires_at = now() + (%s * interval '1 second'),"
            " attempt_count = outbox.attempt_count + 1, updated_at = now()"
            " FROM candidate WHERE outbox.id = candidate.id"
            " RETURNING outbox.id, outbox.account_id, outbox.caller_scope_id,"
            " outbox.notification_type, outbox.url,"
            " outbox.idempotency_key, outbox.signing_scope_id, outbox.encryption_key_id,"
            " outbox.dedup_scope,"
            " outbox.encrypted_body, outbox.envelope_nonce,"
            " outbox.attempt_count"
        )
        self._sql_ack = (  # noqa: S608
            f"UPDATE {table} SET state = 'delivered', delivered_at = now(),"  # nosec B608
            " lease_token = NULL, lease_expires_at = NULL,"
            " last_http_status = %s, last_error = NULL, updated_at = now()"
            " WHERE id = %s AND state = 'in_flight' AND lease_token = %s"
        )
        self._sql_release = (  # noqa: S608
            f"UPDATE {table} SET"  # nosec B608
            " state = CASE WHEN retry_until <= now() THEN 'expired' ELSE 'pending' END,"
            " available_at = CASE WHEN retry_until <= now() THEN available_at"
            "                     ELSE now() + (%s * interval '1 second') END,"
            " lease_token = NULL, lease_expires_at = NULL,"
            " last_http_status = %s, last_error = %s, updated_at = now()"
            " WHERE id = %s AND state = 'in_flight' AND lease_token = %s"
        )
        self._sql_quarantine = (  # noqa: S608
            f"UPDATE {table} SET state = 'invalid', lease_token = NULL,"  # nosec B608
            " lease_expires_at = NULL, last_error = %s, updated_at = now()"
            " WHERE id = %s AND state = 'in_flight' AND lease_token = %s"
        )
        self._sql_purge = (  # noqa: S608
            f"DELETE FROM {table} WHERE id IN ("  # nosec B608
            f" SELECT id FROM {table} WHERE retry_until + interval '7 days' <= now()"  # nosec B608
            " AND state IN ('delivered', 'expired', 'invalid')"
            " ORDER BY id LIMIT 1000"
            ")"
        )

    async def create_schema(self) -> None:
        """Apply the bundled, additive DDL on a pool-owned transaction."""
        ddl = Path(__file__).with_name("notification_outbox.sql").read_text()
        ddl = ddl.replace(DEFAULT_TABLE, self._table)
        async with self._pool.connection() as conn:
            await conn.execute(ddl)

    @staticmethod
    def _scope(caller: str, account: str | None, url: str, kind: str) -> bytes:
        return hashlib.sha256(_canonical([caller, account, url, kind])).digest()

    @staticmethod
    def _aad(
        *,
        caller: str,
        account: str | None,
        url: str,
        kind: str,
        key: str,
        signing_scope: str | None,
        encryption_key_id: str,
        dedup_scope: bytes,
    ) -> bytes:
        return _canonical(
            [
                "notification-outbox-v1",
                kind,
                url,
                account,
                caller,
                signing_scope,
                key,
                encryption_key_id,
                dedup_scope.hex(),
            ]
        )

    def _validate_scope(self, scope: str | None) -> None:
        if scope is not None:
            _identifier(scope, "signing_scope_id")
        if self._sender is not None and scope is not None:
            raise ValueError("fixed-sender outboxes must not carry a signing_scope_id")
        if self._sender_resolver is not None and scope is None:
            raise ValueError("sender-resolver outboxes require a signing_scope_id")

    @staticmethod
    def _validate_url(url: str) -> None:
        if not isinstance(url, str) or len(url.encode("utf-8")) > 2048:
            raise ValueError("webhook URL must not exceed 2048 UTF-8 bytes")
        parsed = httpx.URL(url)
        if (
            parsed.scheme != "https"
            or not parsed.host
            or parsed.username
            or parsed.password
            or parsed.fragment
        ):
            raise ValueError("webhook URL must be absolute HTTPS without userinfo or fragment")

    @staticmethod
    def _protected(prepared: PreparedWebhook) -> bytes:
        headers = dict(prepared.extra_headers)
        if any(not isinstance(k, str) or not isinstance(v, str) for k, v in headers.items()):
            raise ValueError("notification extra_headers must map strings to strings")
        merge_extra_headers(
            base={},
            extra=headers,
            reserved=frozenset(
                {
                    "content-type",
                    "content-length",
                    "host",
                    "signature",
                    "signature-input",
                    "content-digest",
                }
            ),
        )
        for name, value in headers.items():
            if not re.fullmatch(r"[!#$%&'*+\-.^_`|~0-9A-Za-z]+", name) or any(
                c in value for c in ("\r", "\n", "\x00")
            ):
                raise ValueError("invalid notification HTTP headers")
        # Credentials/headers are authenticated and encrypted along with the body.
        return _canonical(
            {
                "version": 1,
                "body": base64.b64encode(prepared.body).decode("ascii"),
                "extra_headers": headers,
            }
        )

    async def enqueue_prepared(
        self,
        conn: AsyncConnection[Any],
        prepared: PreparedWebhook,
        *,
        notification_type: str,
        caller_scope_id: str,
        account_id: str | None = None,
        signing_scope_id: str | None = None,
    ) -> int:
        """Enqueue on the caller's transaction; never commit or perform HTTP.

        Identical re-enqueue returns the existing id. Reusing a scoped key with
        different body, headers or signing scope raises ``ValueError``. The
        caller must open a real transaction (autocommit alone is rejected).
        """
        from psycopg.pq import TransactionStatus

        if conn.info.transaction_status != TransactionStatus.INTRANS:
            raise ValueError("enqueue_prepared requires an open business transaction")
        _identifier(caller_scope_id, "caller_scope_id")
        _identifier(notification_type, "notification_type")
        _identifier(prepared.idempotency_key, "idempotency_key")
        if account_id is not None:
            _identifier(account_id, "account_id")
        self._validate_scope(signing_scope_id)
        self._validate_url(prepared.url)
        payload = validate_notification_body(
            prepared.body,
            idempotency_key=prepared.idempotency_key,
            notification_type=notification_type,
            account_id=account_id,
        )
        if payload.get("account_id") is not None and account_id is None:
            raise ValueError("account-anchored notifications require account_id")
        protected = self._protected(prepared)
        scope = self._scope(caller_scope_id, account_id, prepared.url, notification_type)
        aad = self._aad(
            caller=caller_scope_id,
            account=account_id,
            url=prepared.url,
            kind=notification_type,
            key=prepared.idempotency_key,
            signing_scope=signing_scope_id,
            encryption_key_id=self._key_id,
            dedup_scope=scope,
        )
        nonce = os.urandom(12)
        encrypted = self._ciphers[self._key_id].encrypt(nonce, protected, aad)
        deadline = datetime.now(timezone.utc) + timedelta(
            seconds=self.delivery_retry_horizon_seconds
        )
        cursor = await conn.execute(
            self._sql_insert,
            (
                notification_type,
                caller_scope_id,
                account_id,
                prepared.url,
                prepared.idempotency_key,
                signing_scope_id,
                self._key_id,
                scope,
                encrypted,
                nonce,
                deadline,
            ),
        )
        row = await cursor.fetchone()
        if row is not None:
            return int(row[0])
        cursor = await conn.execute(  # noqa: S608
            f"SELECT {self._columns} FROM {self._table} "  # nosec B608
            "WHERE dedup_scope=%s AND idempotency_key=%s FOR UPDATE",
            (scope, prepared.idempotency_key),
        )
        existing = await cursor.fetchone()
        if existing is None:
            raise RuntimeError("notification outbox conflict row disappeared")
        try:
            stored = self._open(existing)
        except (InvalidTag, ValueError, KeyError) as exc:
            raise ValueError("existing notification binding is invalid") from exc
        if stored != prepared or existing[6] != signing_scope_id:
            raise ValueError("notification key is already bound to a different immutable delivery")
        return int(existing[0])

    def _open(self, row: tuple[Any, ...]) -> PreparedWebhook:
        _, account, caller, kind, url, key, signing_scope, key_id, scope, ciphertext, nonce = row[
            :11
        ]
        scope = bytes(scope)
        if scope != self._scope(caller, account, url, kind):
            raise ValueError("notification deduplication scope mismatch")
        self._validate_scope(signing_scope)
        self._validate_url(url)
        aad = self._aad(
            caller=caller,
            account=account,
            url=url,
            kind=kind,
            key=key,
            signing_scope=signing_scope,
            encryption_key_id=key_id,
            dedup_scope=scope,
        )
        cipher = self._ciphers.get(key_id)
        if cipher is None:
            raise ValueError("notification encryption key is unavailable")
        value = json.loads(cipher.decrypt(bytes(nonce), bytes(ciphertext), aad))
        if not isinstance(value, dict) or value.get("version") != 1:
            raise ValueError("invalid encrypted notification envelope")
        body = base64.b64decode(value["body"], validate=True)
        headers = value.get("extra_headers")
        if not isinstance(headers, dict) or any(
            not isinstance(k, str) or not isinstance(v, str) for k, v in headers.items()
        ):
            raise ValueError("invalid stored notification headers")
        validate_notification_body(
            body, idempotency_key=key, notification_type=kind, account_id=account
        )
        return PreparedWebhook(url=url, idempotency_key=key, body=body, extra_headers=headers)

    def _validate_sender(self, sender: WebhookSender) -> None:
        if (
            not callable(getattr(sender, "send_prepared", None))
            or not getattr(sender, "_owns_client", False)
            or getattr(sender, "_allow_private_destinations", False)
            or getattr(sender, "signs_with_rfc9421", False) is not True
        ):
            raise ValueError(
                "notification delivery requires an RFC 9421 sender with SDK-owned "
                "IP-pinned transport and private destinations disabled"
            )
        if self._lease_seconds < float(getattr(sender, "_timeout", 0.0)) + 5:
            raise ValueError("lease_seconds must exceed sender timeout by at least 5 seconds")

    async def _resolve_sender(self, scope: str | None) -> WebhookSender:
        self._validate_scope(scope)
        if self._sender is not None:
            self._validate_sender(self._sender)
            return self._sender
        assert self._sender_resolver is not None and scope is not None
        try:
            resolution = await self._sender_resolver.resolve(scope)
        except (ScopePermanentlyUnknown, ScopeTransientlyUnavailable):
            raise
        except Exception as exc:
            _log_failure("unexpected signing resolver failure", exc)
            raise ScopeTransientlyUnavailable from None
        try:
            if not isinstance(resolution, WebhookSenderResolution):
                raise ValueError("invalid sender resolution")
            self._validate_sender(resolution.sender)
            if (
                getattr(getattr(resolution.sender, "_auth", None), "alg", None)
                not in resolution.advertised_algorithms
            ):
                raise ValueError("resolved signing algorithm is not advertised")
        except Exception:
            raise ScopePermanentlyUnknown from None
        return resolution.sender

    async def _lease_active(self, row_id: int, token: str) -> bool:
        async with self._pool.connection() as conn:
            cursor = await conn.execute(  # noqa: S608
                f"SELECT 1 FROM {self._table} WHERE id=%s AND state='in_flight' "  # nosec B608
                "AND lease_token=%s AND lease_expires_at > now() AND retry_until > now()",
                (row_id, token),
            )
            return await cursor.fetchone() is not None

    async def _quarantine(self, row_id: int, token: str, reason: str) -> None:
        async with self._pool.connection() as conn:
            await conn.execute(self._sql_quarantine, (reason, row_id, token))
        logger.error("[adcp.notification_outbox] row %s quarantined: %s", row_id, reason)

    async def process_one(self) -> bool:
        """Claim a row, verify its envelope, then send under an expiring fence."""
        token = uuid.uuid4().hex
        async with self._pool.connection() as conn:
            await conn.execute(self._sql_expire)
            cursor = await conn.execute(self._sql_claim, (token, self._lease_seconds))
            row = await cursor.fetchone()
        if row is None:
            return False
        row_id = int(row[0])
        try:
            prepared = self._open(row)
        except (InvalidTag, ValueError, KeyError, TypeError):
            await self._quarantine(
                row_id, token, "stored notification failed authenticated binding verification"
            )
            return True
        delivery = None
        error_name = "delivery failed"
        try:

            async def attempt() -> WebhookDeliveryResult:
                sender = await self._resolve_sender(row[6])
                return await sender.send_prepared(
                    prepared, before_attempt=lambda: self._lease_active(row_id, token)
                )

            delivery = await asyncio.wait_for(attempt(), timeout=self._lease_seconds - 1)
        except ScopePermanentlyUnknown:
            await self._quarantine(
                row_id, token, "notification signing scope is permanently unavailable"
            )
            return True
        except SSRFValidationError as exc:
            if not exc.transient:
                await self._quarantine(row_id, token, "permanent destination validation failure")
                return True
            error_name = "SSRFValidationError"
        except ValueError:
            await self._quarantine(
                row_id, token, "permanent notification delivery validation failure"
            )
            return True
        except Exception as exc:
            error_name = type(exc).__name__  # Never persist credentials or peer/hook diagnostics.
            _log_failure(f"row {row_id} delivery failed; retrying", exc)
        if delivery is not None and delivery.ok:
            async with self._pool.connection() as conn:
                await conn.execute(self._sql_ack, (delivery.status_code, row_id, token))
        elif delivery is not None and not (
            delivery.status_code >= 500 or delivery.status_code in {408, 425, 429}
        ):
            await self._quarantine(row_id, token, f"permanent HTTP {delivery.status_code}")
        else:
            # Reuse the public exponential-backoff primitive, saturating its exponent.
            delay = self._retry.delay_for_attempt(min(int(row[11]), 30) + 1)
            status = delivery.status_code if delivery is not None else None
            error_name = f"HTTP {status}" if status is not None else error_name
            async with self._pool.connection() as conn:
                await conn.execute(self._sql_release, (delay, status, error_name, row_id, token))
        return True

    async def purge_expired(self) -> None:
        """Expire overdue work and purge terminal bindings after retention."""
        async with self._pool.connection() as conn:
            await conn.execute(self._sql_expire)
            await conn.execute(self._sql_purge)

    async def run_worker(
        self, *, poll_interval: float = 1.0, purge_interval: float = 300.0
    ) -> None:
        """Run until cancelled; worker failures retry and abandoned leases recover."""
        if any(not math.isfinite(v) or v <= 0 for v in (poll_interval, purge_interval)):
            raise ValueError("worker intervals must be finite and positive")
        next_purge = 0.0
        while True:
            try:
                if time.monotonic() >= next_purge:
                    await self.purge_expired()
                    next_purge = time.monotonic() + purge_interval
                if not await self.process_one():
                    await asyncio.sleep(poll_interval)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                _log_failure("worker iteration failed; retrying", exc)
                await asyncio.sleep(poll_interval)


__all__ = ["PgNotificationOutbox"]
