"""Crash-durable PostgreSQL outbox for terminal protocol-task webhooks.

The outbox is intentionally coupled to :class:`PgTaskRegistry`: terminal
task state and the immutable webhook request are written on the same database
connection and commit together. Workers claim rows with expiring leases,
perform HTTP outside the database transaction, then acknowledge or release
the lease. A crash after receiver acceptance can cause an exact retry; the
stable body and idempotency key make that retry safe for conformant receivers.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import random
import re
import time
import uuid
from typing import TYPE_CHECKING, Any, ClassVar

import httpx
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from adcp.signing.jwks import SSRFValidationError
from adcp.webhook_sender import PreparedWebhook, WebhookDeliveryResult
from adcp.webhook_supervisor import RetryPolicy

if TYPE_CHECKING:
    from psycopg_pool import AsyncConnectionPool

    from adcp.webhook_sender import WebhookSender

try:
    import psycopg_pool

    PG_AVAILABLE = bool(psycopg_pool.AsyncConnectionPool)
except ImportError:
    PG_AVAILABLE = False

logger = logging.getLogger(__name__)
_INSTALL_HINT = (
    "PgTaskWebhookOutbox requires psycopg3 and psycopg-pool. "
    "Install the 'pg' extra: `pip install 'adcp[pg]'` "
    "(Poetry: `poetry add 'adcp[pg]'`)."
)
_SAFE_IDENTIFIER_RE = re.compile(r"^[a-z_][a-z0-9_]{0,44}$")
DEFAULT_TABLE = "adcp_task_webhook_outbox"
MIN_RETRY_HORIZON_SECONDS = 86_400
MAX_RETRY_HORIZON_SECONDS = 604_800


class PgTaskWebhookOutbox:
    """Atomic task-webhook outbox and lease-based delivery worker.

    Construct this with the same pool as :class:`PgTaskRegistry`, then pass it
    to ``PgTaskRegistry(..., task_webhook_outbox=outbox)``. Call
    :meth:`create_schema` during migration/startup and run at least one
    :meth:`run_worker` loop in every deployment.
    """

    delivery_state_is_durable: ClassVar[bool] = True
    supports_atomic_task_outbox: ClassVar[bool] = True

    def __init__(
        self,
        *,
        pool: AsyncConnectionPool,
        sender: WebhookSender | None,
        encryption_key: bytes,
        delivery_retry_horizon_seconds: int,
        retry: RetryPolicy | None = None,
        lease_seconds: int = 60,
        table: str = DEFAULT_TABLE,
    ) -> None:
        if not PG_AVAILABLE:
            raise ImportError(_INSTALL_HINT)
        if sender is None:
            raise ValueError("PgTaskWebhookOutbox requires a non-None WebhookSender")
        if len(encryption_key) != 32:
            raise ValueError("encryption_key must be exactly 32 bytes for AES-256-GCM")
        if not getattr(sender, "_owns_client", False) or getattr(
            sender, "_allow_private_destinations", False
        ):
            raise ValueError(
                "PgTaskWebhookOutbox requires a WebhookSender using the SDK-owned "
                "IP-pinned transport with private destinations disabled"
            )
        if type(delivery_retry_horizon_seconds) is not int or not (
            MIN_RETRY_HORIZON_SECONDS <= delivery_retry_horizon_seconds <= MAX_RETRY_HORIZON_SECONDS
        ):
            raise ValueError(
                "delivery_retry_horizon_seconds must be an integer from "
                f"{MIN_RETRY_HORIZON_SECONDS} through {MAX_RETRY_HORIZON_SECONDS}"
            )
        if type(lease_seconds) is not int or lease_seconds <= 0:
            raise ValueError("lease_seconds must be a positive integer")
        sender_timeout = float(getattr(sender, "_timeout", 0.0))
        if lease_seconds < sender_timeout + 5:
            raise ValueError(
                "lease_seconds must exceed the sender HTTP timeout by at least 5 seconds"
            )
        resolved_retry = retry or RetryPolicy()
        if (
            resolved_retry.base_delay_seconds <= 0
            or resolved_retry.max_delay_seconds <= 0
            or resolved_retry.max_delay_seconds < resolved_retry.base_delay_seconds
        ):
            raise ValueError(
                "retry delays must be positive and max_delay_seconds must be at least "
                "base_delay_seconds"
            )
        if not _SAFE_IDENTIFIER_RE.fullmatch(table):
            raise ValueError(
                f"table must match [a-z_][a-z0-9_]{{0,44}} (ASCII only), got {table!r}"
            )

        self._pool = pool
        self._sender = sender
        self._cipher = AESGCM(encryption_key)
        self.delivery_retry_horizon_seconds = delivery_retry_horizon_seconds
        self._retry = resolved_retry
        self._lease_seconds = lease_seconds
        self._table = table
        self._worker_started = False

        self._sql_insert = (  # noqa: S608
            f"INSERT INTO {table} ("
            "task_id, task_type, terminal_status, url, operation_id, "
            "idempotency_key, account_id, encrypted_body, envelope_nonce, "
            "retry_horizon_seconds"
            ") VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id"
        )
        self._sql_expire = (  # noqa: S608
            f"WITH expired AS (SELECT id FROM {table}"
            " WHERE state IN ('pending', 'in_flight') AND retry_until <= now()"
            " ORDER BY id FOR UPDATE SKIP LOCKED LIMIT 1000)"
            f" UPDATE {table} AS outbox SET state = 'expired', lease_token = NULL,"
            " lease_expires_at = NULL, updated_at = now()"
            " FROM expired WHERE outbox.id = expired.id"
        )
        self._sql_claim = (  # noqa: S608
            f"WITH candidate AS ("
            f" SELECT id FROM {table}"
            " WHERE (retry_until IS NULL OR retry_until > now()) AND ("
            "   (state = 'pending' AND available_at <= now()) OR"
            "   (state = 'in_flight' AND lease_expires_at <= now())"
            " ) ORDER BY available_at, id FOR UPDATE SKIP LOCKED LIMIT 1"
            f") UPDATE {table} AS outbox SET"
            " state = 'in_flight', lease_token = %s,"
            " lease_expires_at = now() + (%s * interval '1 second'),"
            " first_attempt_at = COALESCE(outbox.first_attempt_at, now()),"
            " retry_until = COALESCE("
            "   outbox.retry_until, now() + (outbox.retry_horizon_seconds * interval '1 second')"
            " ),"
            " attempt_count = outbox.attempt_count + 1, updated_at = now()"
            " FROM candidate WHERE outbox.id = candidate.id"
            " RETURNING outbox.id, outbox.account_id, outbox.task_id, outbox.task_type,"
            " outbox.terminal_status, outbox.url, outbox.operation_id,"
            " outbox.idempotency_key, outbox.encrypted_body, outbox.envelope_nonce,"
            " outbox.attempt_count"
        )
        self._sql_ack = (  # noqa: S608
            f"UPDATE {table} SET state = 'delivered', delivered_at = now(),"
            " lease_token = NULL, lease_expires_at = NULL,"
            " last_http_status = %s, last_error = NULL, updated_at = now()"
            " WHERE id = %s AND state = 'in_flight' AND lease_token = %s"
        )
        self._sql_release = (  # noqa: S608
            f"UPDATE {table} SET"
            " state = CASE WHEN retry_until <= now() THEN 'expired' ELSE 'pending' END,"
            " available_at = CASE WHEN retry_until <= now() THEN available_at"
            "                     ELSE now() + (%s * interval '1 second') END,"
            " lease_token = NULL, lease_expires_at = NULL,"
            " last_http_status = %s, last_error = %s, updated_at = now()"
            " WHERE id = %s AND state = 'in_flight' AND lease_token = %s"
        )
        self._sql_quarantine = (  # noqa: S608
            f"UPDATE {table} SET state = 'invalid', lease_token = NULL,"
            " lease_expires_at = NULL, last_error = %s, updated_at = now()"
            " WHERE id = %s AND state = 'in_flight' AND lease_token = %s"
        )
        self._sql_purge = (  # noqa: S608
            f"DELETE FROM {table} WHERE id IN ("
            f" SELECT id FROM {table} WHERE retry_until <= now()"
            " AND state IN ('delivered', 'expired', 'invalid')"
            " ORDER BY id LIMIT 1000"
            ")"
        )

    async def create_schema(self) -> None:
        """Create the outbox table and work index idempotently."""
        statements = [
            f"""CREATE TABLE IF NOT EXISTS {self._table} (
                id                 BIGSERIAL PRIMARY KEY,
                task_id            TEXT COLLATE "C" NOT NULL UNIQUE,
                account_id         TEXT COLLATE "C" NOT NULL,
                task_type          TEXT NOT NULL,
                terminal_status    TEXT NOT NULL,
                url                TEXT NOT NULL,
                operation_id       TEXT NOT NULL,
                idempotency_key    TEXT COLLATE "C" NOT NULL UNIQUE,
                encrypted_body     BYTEA NOT NULL,
                envelope_nonce     BYTEA NOT NULL,
                state              TEXT NOT NULL DEFAULT 'pending',
                attempt_count      INTEGER NOT NULL DEFAULT 0,
                available_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
                lease_token        TEXT COLLATE "C",
                lease_expires_at   TIMESTAMPTZ,
                created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
                updated_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
                first_attempt_at   TIMESTAMPTZ,
                retry_until        TIMESTAMPTZ,
                retry_horizon_seconds INTEGER NOT NULL,
                delivered_at       TIMESTAMPTZ,
                last_http_status   INTEGER,
                last_error         TEXT,
                CHECK (state IN ('pending', 'in_flight', 'delivered', 'expired', 'invalid')),
                CHECK (terminal_status IN ('completed', 'failed')),
                CHECK (attempt_count >= 0),
                CHECK (octet_length(envelope_nonce) = 12),
                CHECK (retry_horizon_seconds BETWEEN 86400 AND 604800),
                CHECK ((first_attempt_at IS NULL) = (retry_until IS NULL)),
                CHECK (retry_until IS NULL OR retry_until > first_attempt_at)
            )""",
            f"""CREATE INDEX IF NOT EXISTS {self._table}_work_idx
                ON {self._table} (available_at, id)
                WHERE state IN ('pending', 'in_flight')""",
            f"""CREATE INDEX IF NOT EXISTS {self._table}_retry_until_idx
                ON {self._table} (retry_until)""",
        ]
        async with self._pool.connection() as conn:
            for statement in statements:
                await conn.execute(statement)

    async def enqueue_terminal(
        self,
        conn: Any,
        *,
        task_id: str,
        account_id: str,
        task_type: str,
        status: str,
        result: dict[str, Any],
        url: str,
        operation_id: str,
        token: str | None,
    ) -> int:
        """Insert a terminal webhook using the caller's open transaction."""
        if status not in {"completed", "failed"}:
            raise ValueError(f"terminal webhook status must be completed or failed, got {status!r}")
        prepared = self._sender.prepare_mcp(
            url=url,
            task_id=task_id,
            task_type=task_type,
            status=status,
            result=result,
            operation_id=operation_id,
            token=token,
        )
        self._validate_callback_url(prepared.url)
        nonce = os.urandom(12)
        aad = self._envelope_aad(
            account_id=account_id,
            task_id=task_id,
            task_type=task_type,
            status=status,
            url=prepared.url,
            operation_id=operation_id,
            idempotency_key=prepared.idempotency_key,
        )
        encrypted_body = self._cipher.encrypt(nonce, prepared.body, aad)
        cursor = await conn.execute(
            self._sql_insert,
            (
                task_id,
                task_type,
                status,
                prepared.url,
                operation_id,
                prepared.idempotency_key,
                account_id,
                encrypted_body,
                nonce,
                self.delivery_retry_horizon_seconds,
            ),
        )
        row = await cursor.fetchone()
        if row is None:
            raise RuntimeError("task webhook outbox insert returned no id")
        return int(row[0])

    def validate_registration(self, url: str) -> None:
        """Validate callback syntax before a task is accepted as Submitted."""
        self._validate_callback_url(url)

    def protect_registration(
        self,
        *,
        account_id: str,
        task_id: str,
        task_type: str,
        url: str,
        operation_id: str,
        token: str | None,
    ) -> tuple[bytes, bytes]:
        """Encrypt and authenticate callback registration at task issue time."""
        self._validate_callback_url(url)
        nonce = os.urandom(12)
        plaintext = json.dumps(
            {"url": url, "operation_id": operation_id, "token": token},
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        return (
            self._cipher.encrypt(
                nonce,
                plaintext,
                self._registration_aad(
                    account_id=account_id,
                    task_id=task_id,
                    task_type=task_type,
                ),
            ),
            nonce,
        )

    def open_registration(
        self,
        *,
        account_id: str,
        task_id: str,
        task_type: str,
        encrypted_registration: bytes,
        nonce: bytes,
    ) -> tuple[str, str, str | None]:
        """Verify and decrypt callback registration at terminal transition."""
        try:
            plaintext = self._cipher.decrypt(
                nonce,
                encrypted_registration,
                self._registration_aad(
                    account_id=account_id,
                    task_id=task_id,
                    task_type=task_type,
                ),
            )
            value = json.loads(plaintext)
        except (InvalidTag, json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise ValueError("task webhook registration failed authenticated decryption") from exc
        if not isinstance(value, dict):
            raise ValueError("task webhook registration must decrypt to an object")
        url = value.get("url")
        operation_id = value.get("operation_id")
        token = value.get("token")
        if not isinstance(url, str) or not isinstance(operation_id, str):
            raise ValueError("task webhook registration has invalid URL or operation_id")
        if token is not None and not isinstance(token, str):
            raise ValueError("task webhook registration token must be a string or null")
        self._validate_callback_url(url)
        return url, operation_id, token

    async def run_worker(
        self,
        *,
        poll_interval: float = 1.0,
        purge_interval: float = 300.0,
    ) -> None:
        """Continuously publish eligible rows until the task is cancelled."""
        if poll_interval <= 0:
            raise ValueError("poll_interval must be positive")
        if purge_interval <= 0:
            raise ValueError("purge_interval must be positive")
        self._worker_started = True
        next_purge = 0.0
        try:
            while True:
                try:
                    now = time.monotonic()
                    if now >= next_purge:
                        await self.purge_expired()
                        next_purge = now + purge_interval
                    processed = await self.process_one()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.exception("[adcp.task_webhook_outbox] worker iteration failed; retrying")
                    await asyncio.sleep(poll_interval)
                    continue
                if not processed:
                    await asyncio.sleep(poll_interval)
        finally:
            self._worker_started = False

    async def process_one(self) -> bool:
        """Claim and attempt one delivery; return ``False`` when idle."""
        lease_token = uuid.uuid4().hex
        async with self._pool.connection() as conn:
            await conn.execute(self._sql_expire)
            cursor = await conn.execute(
                self._sql_claim,
                (lease_token, self._lease_seconds),
            )
            row = await cursor.fetchone()
        if row is None:
            return False

        (
            row_id,
            account_id,
            task_id,
            task_type,
            status,
            url,
            operation_id,
            idempotency_key,
            encrypted_body,
            nonce,
            attempt_count,
        ) = row
        aad = self._envelope_aad(
            account_id=str(account_id),
            task_id=str(task_id),
            task_type=str(task_type),
            status=str(status),
            url=str(url),
            operation_id=str(operation_id),
            idempotency_key=str(idempotency_key),
        )
        try:
            body_bytes = self._cipher.decrypt(bytes(nonce), bytes(encrypted_body), aad)
            self._validate_stored_body(
                body_bytes,
                task_id=str(task_id),
                task_type=str(task_type),
                status=str(status),
                operation_id=str(operation_id),
                idempotency_key=str(idempotency_key),
            )
        except (InvalidTag, ValueError, json.JSONDecodeError, UnicodeDecodeError):
            error_message = (
                "stored webhook envelope failed authenticated binding verification; "
                "row quarantined without delivery"
            )
            async with self._pool.connection() as conn:
                await conn.execute(
                    self._sql_quarantine,
                    (error_message, row_id, lease_token),
                )
            logger.error(
                "[adcp.task_webhook_outbox] integrity failure for task %s; row quarantined",
                task_id,
            )
            return True
        prepared = PreparedWebhook(
            url=str(url),
            idempotency_key=str(idempotency_key),
            body=body_bytes,
        )
        delivery: WebhookDeliveryResult | None = None
        error: BaseException | None = None
        try:
            delivery = await asyncio.wait_for(
                self._sender.send_prepared(prepared),
                timeout=self._lease_seconds - 1,
            )
        except SSRFValidationError as exc:
            if exc.transient:
                error = exc
            else:
                await self._quarantine_permanent_delivery_error(
                    row_id=row_id,
                    lease_token=lease_token,
                    task_id=str(task_id),
                    error=exc,
                )
                return True
        except ValueError as exc:
            await self._quarantine_permanent_delivery_error(
                row_id=row_id,
                lease_token=lease_token,
                task_id=str(task_id),
                error=exc,
            )
            return True
        except Exception as exc:
            error = exc

        if delivery is not None and delivery.ok:
            async with self._pool.connection() as conn:
                await conn.execute(
                    self._sql_ack,
                    (delivery.status_code, row_id, lease_token),
                )
            return True

        if delivery is not None and not self._is_retryable_http_status(delivery.status_code):
            error_message = (
                f"permanent HTTP {delivery.status_code}: {delivery.response_body[:200]!r}"
            )
            async with self._pool.connection() as conn:
                await conn.execute(
                    self._sql_quarantine,
                    (error_message[:1000], row_id, lease_token),
                )
            logger.error(
                "[adcp.task_webhook_outbox] permanent HTTP failure for task %s; " "row quarantined",
                task_id,
            )
            return True

        delay = self._retry_delay(int(attempt_count))
        http_status = delivery.status_code if delivery is not None else None
        if delivery is not None:
            error_message = f"HTTP {delivery.status_code}: {delivery.response_body[:200]!r}"
        elif error is not None:
            error_message = f"{type(error).__name__}: {error}"
        else:
            error_message = "delivery failed without a result"
        async with self._pool.connection() as conn:
            await conn.execute(
                self._sql_release,
                (delay, http_status, error_message, row_id, lease_token),
            )
        logger.warning(
            "[adcp.task_webhook_outbox] delivery failed for task %s; retry in %.1fs",
            task_id,
            delay,
        )
        return True

    async def _quarantine_permanent_delivery_error(
        self,
        *,
        row_id: int,
        lease_token: str,
        task_id: str,
        error: BaseException,
    ) -> None:
        error_message = f"permanent delivery validation failure: {type(error).__name__}: {error}"
        async with self._pool.connection() as conn:
            await conn.execute(
                self._sql_quarantine,
                (error_message[:1000], row_id, lease_token),
            )
        logger.error(
            "[adcp.task_webhook_outbox] permanent delivery failure for task %s; " "row quarantined",
            task_id,
        )

    @staticmethod
    def _is_retryable_http_status(status_code: int) -> bool:
        return status_code >= 500 or status_code in {408, 425, 429}

    async def purge_expired(self) -> None:
        """Delete delivery proof only after the advertised horizon elapses."""
        async with self._pool.connection() as conn:
            await conn.execute(self._sql_expire)
            await conn.execute(self._sql_purge)

    def _retry_delay(self, attempt_count: int) -> float:
        exponent = max(0, min(attempt_count - 1, 30))
        delay: float = float(
            min(
                self._retry.base_delay_seconds * (2**exponent),
                self._retry.max_delay_seconds,
            )
        )
        if self._retry.jitter:
            delay *= 0.5 + random.random() * 0.5
        return delay

    @staticmethod
    def _envelope_aad(
        *,
        account_id: str,
        task_id: str,
        task_type: str,
        status: str,
        url: str,
        operation_id: str,
        idempotency_key: str,
    ) -> bytes:
        """Canonical associated data binding every routing/security field."""
        return json.dumps(
            [account_id, task_id, task_type, status, url, operation_id, idempotency_key],
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")

    @staticmethod
    def _registration_aad(*, account_id: str, task_id: str, task_type: str) -> bytes:
        return json.dumps(
            ["task-webhook-registration-v1", account_id, task_id, task_type],
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")

    @staticmethod
    def _validate_callback_url(url: str) -> None:
        if len(url) > 2048:
            raise ValueError("webhook URL must not exceed 2048 characters")
        parsed = httpx.URL(url)
        if parsed.scheme != "https" or not parsed.host:
            raise ValueError("webhook URL must be an absolute HTTPS URL")
        if parsed.username or parsed.password:
            raise ValueError("webhook URL must not contain userinfo")

    @staticmethod
    def _validate_stored_body(
        body: bytes,
        *,
        task_id: str,
        task_type: str,
        status: str,
        operation_id: str,
        idempotency_key: str,
    ) -> None:
        payload = json.loads(body)
        if not isinstance(payload, dict):
            raise ValueError("stored webhook body must be a JSON object")
        expected = {
            "task_id": task_id,
            "task_type": task_type,
            "status": status,
            "operation_id": operation_id,
            "idempotency_key": idempotency_key,
        }
        if any(payload.get(key) != value for key, value in expected.items()):
            raise ValueError("stored webhook body does not match its envelope metadata")


__all__ = [
    "DEFAULT_TABLE",
    "MAX_RETRY_HORIZON_SECONDS",
    "MIN_RETRY_HORIZON_SECONDS",
    "PG_AVAILABLE",
    "PgTaskWebhookOutbox",
]
