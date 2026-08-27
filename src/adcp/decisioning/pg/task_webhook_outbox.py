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
import base64
import inspect
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

from adcp.decisioning.task_registry import TaskWebhookAuthentication
from adcp.signing.jwks import SSRFValidationError
from adcp.webhook_sender import (
    PreparedWebhook,
    ScopePermanentlyUnknown,
    ScopeTransientlyUnavailable,
    TransportHook,
    WebhookDeliveryResult,
    WebhookSender,
    WebhookSenderResolution,
    WebhookSenderResolver,
)
from adcp.webhook_supervisor import RetryPolicy

if TYPE_CHECKING:
    from psycopg_pool import AsyncConnectionPool

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
MAX_SIGNING_SCOPE_ID_BYTES = 255
_LEGACY_AUTH_SCHEMES = frozenset({"Bearer", "HMAC-SHA256"})
_ENCRYPTED_DELIVERY_VERSION = 1


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
        sender: WebhookSender | None = None,
        sender_resolver: WebhookSenderResolver | None = None,
        encryption_key: bytes,
        delivery_retry_horizon_seconds: int,
        retry: RetryPolicy | None = None,
        lease_seconds: int = 60,
        legacy_hmac_fallback: bool = False,
        legacy_allowed_destination_ports: frozenset[int] | None = None,
        legacy_transport_hooks: tuple[TransportHook, ...] | None = None,
        table: str = DEFAULT_TABLE,
    ) -> None:
        if not PG_AVAILABLE:
            raise ImportError(_INSTALL_HINT)
        if (sender is None) == (sender_resolver is None):
            raise ValueError("pass exactly one of sender or sender_resolver")
        if sender_resolver is not None:
            resolver_method = getattr(sender_resolver, "resolve", None)
            if not callable(resolver_method):
                is_async_resolver = False
            else:
                try:
                    is_async_resolver = inspect.iscoroutinefunction(inspect.unwrap(resolver_method))
                except ValueError:
                    is_async_resolver = False
            if not is_async_resolver:
                raise ValueError("sender_resolver must define async resolve(signing_scope_id)")
        if len(encryption_key) != 32:
            raise ValueError("encryption_key must be exactly 32 bytes for AES-256-GCM")
        if sender is not None:
            self._validate_delivery_sender(sender)
        if type(delivery_retry_horizon_seconds) is not int or not (
            MIN_RETRY_HORIZON_SECONDS <= delivery_retry_horizon_seconds <= MAX_RETRY_HORIZON_SECONDS
        ):
            raise ValueError(
                "delivery_retry_horizon_seconds must be an integer from "
                f"{MIN_RETRY_HORIZON_SECONDS} through {MAX_RETRY_HORIZON_SECONDS}"
            )
        if type(lease_seconds) is not int or lease_seconds <= 1:
            raise ValueError("lease_seconds must be an integer greater than 1")
        if type(legacy_hmac_fallback) is not bool:
            raise ValueError("legacy_hmac_fallback must be a bool")
        sender_timeout = float(getattr(sender, "_timeout", 0.0)) if sender is not None else 0.0
        if sender is not None and lease_seconds < sender_timeout + 5:
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
        self._sender_resolver = sender_resolver
        self._cipher = AESGCM(encryption_key)
        self.delivery_retry_horizon_seconds = delivery_retry_horizon_seconds
        self._retry = resolved_retry
        self._lease_seconds = lease_seconds
        self.legacy_hmac_fallback = legacy_hmac_fallback
        self._legacy_allowed_destination_ports = (
            legacy_allowed_destination_ports
            if legacy_allowed_destination_ports is not None
            else getattr(sender, "_allowed_destination_ports", None)
        )
        self._legacy_transport_hooks = (
            legacy_transport_hooks
            if legacy_transport_hooks is not None
            else tuple(getattr(sender, "_transport_hooks", ()))
        )
        self._table = table
        self._worker_started = False

        self._sql_insert = (  # noqa: S608
            f"INSERT INTO {table} ("
            "task_id, task_type, terminal_status, url, operation_id, "
            "idempotency_key, signing_scope_id, account_id, encrypted_body, envelope_nonce, "
            "retry_horizon_seconds"
            ") VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id"
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
            " outbox.idempotency_key, outbox.signing_scope_id,"
            " outbox.encrypted_body, outbox.envelope_nonce,"
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
                signing_scope_id   TEXT COLLATE "C",
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
            f'''ALTER TABLE {self._table}
                ADD COLUMN IF NOT EXISTS signing_scope_id TEXT COLLATE "C"''',
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
        authentication: TaskWebhookAuthentication | None = None,
        signing_scope_id: str | None = None,
    ) -> int:
        """Insert a terminal webhook using the caller's open transaction."""
        if status not in {"completed", "failed"}:
            raise ValueError(f"terminal webhook status must be completed or failed, got {status!r}")
        self._validate_authentication(authentication)
        self._validate_scope_for_mode(
            signing_scope_id,
            authentication=authentication,
            require_resolver_scope=False,
        )
        preparer = self._sender or WebhookSender
        prepared = preparer.prepare_mcp(
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
            signing_scope_id=signing_scope_id,
        )
        protected_body = self._protect_delivery_body(prepared.body, authentication)
        encrypted_body = self._cipher.encrypt(nonce, protected_body, aad)
        cursor = await conn.execute(
            self._sql_insert,
            (
                task_id,
                task_type,
                status,
                prepared.url,
                operation_id,
                prepared.idempotency_key,
                signing_scope_id,
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

    def validate_registration(
        self,
        url: str,
        authentication: TaskWebhookAuthentication | None = None,
    ) -> None:
        """Validate callback syntax before a task is accepted as Submitted."""
        self._validate_callback_url(url)
        self._validate_authentication(authentication)

    def protect_registration(
        self,
        *,
        account_id: str,
        task_id: str,
        task_type: str,
        url: str,
        operation_id: str,
        token: str | None,
        authentication: TaskWebhookAuthentication | None = None,
        signing_scope_id: str | None = None,
    ) -> tuple[bytes, bytes]:
        """Encrypt and authenticate callback registration at task issue time."""
        self._validate_callback_url(url)
        self._validate_authentication(authentication)
        self._validate_scope_for_mode(
            signing_scope_id,
            authentication=authentication,
            require_resolver_scope=True,
        )
        nonce = os.urandom(12)
        plaintext = json.dumps(
            {
                "url": url,
                "operation_id": operation_id,
                "token": token,
                "authentication": (
                    {
                        "scheme": authentication.scheme,
                        "credentials": authentication.credentials,
                    }
                    if authentication is not None
                    else None
                ),
                "signing_scope_id": signing_scope_id,
            },
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
        """Verify and decrypt a callback registration.

        The three-item return shape is retained for compatibility. Durable
        registry dispatch uses the private scope-aware decoder below.
        """
        url, operation_id, token, _authentication, _signing_scope_id = (
            self._open_registration_with_scope(
                account_id=account_id,
                task_id=task_id,
                task_type=task_type,
                encrypted_registration=encrypted_registration,
                nonce=nonce,
            )
        )
        return url, operation_id, token

    def _open_registration_with_scope(
        self,
        *,
        account_id: str,
        task_id: str,
        task_type: str,
        encrypted_registration: bytes,
        nonce: bytes,
    ) -> tuple[
        str,
        str,
        str | None,
        TaskWebhookAuthentication | None,
        str | None,
    ]:
        """Verify and decrypt callback registration with its trusted scope."""
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
        authentication_value = value.get("authentication")
        signing_scope_id = value.get("signing_scope_id")
        if not isinstance(url, str) or not isinstance(operation_id, str):
            raise ValueError("task webhook registration has invalid URL or operation_id")
        if token is not None and not isinstance(token, str):
            raise ValueError("task webhook registration token must be a string or null")
        if signing_scope_id is not None and not isinstance(signing_scope_id, str):
            raise ValueError("task webhook registration signing scope must be a string or null")
        authentication: TaskWebhookAuthentication | None = None
        if authentication_value is not None:
            if not isinstance(authentication_value, dict):
                raise ValueError("task webhook registration authentication must be an object")
            scheme = authentication_value.get("scheme")
            credentials = authentication_value.get("credentials")
            if not isinstance(scheme, str) or not isinstance(credentials, str):
                raise ValueError("task webhook registration authentication is invalid")
            try:
                authentication = TaskWebhookAuthentication(
                    scheme=scheme,
                    credentials=credentials,
                )
            except (TypeError, ValueError) as exc:
                raise ValueError("task webhook registration authentication is invalid") from exc
        self._validate_callback_url(url)
        self._validate_authentication(authentication)
        self._validate_scope_for_mode(
            signing_scope_id,
            authentication=authentication,
            require_resolver_scope=False,
        )
        return url, operation_id, token, authentication, signing_scope_id

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
            signing_scope_id,
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
            signing_scope_id=(str(signing_scope_id) if signing_scope_id is not None else None),
        )
        try:
            protected_body = self._cipher.decrypt(bytes(nonce), bytes(encrypted_body), aad)
            body_bytes, authentication = self._open_delivery_body(protected_body)
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
                self._deliver_prepared(
                    prepared,
                    str(signing_scope_id) if signing_scope_id is not None else None,
                    authentication,
                ),
                timeout=self._lease_seconds - 1,
            )
        except ScopePermanentlyUnknown:
            await self._quarantine_permanent_delivery_error(
                row_id=row_id,
                lease_token=lease_token,
                task_id=str(task_id),
                error=ValueError("webhook signing scope is permanently unavailable"),
            )
            return True
        except ScopeTransientlyUnavailable:
            error = RuntimeError("webhook signing scope is temporarily unavailable")
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
            # Receiver-controlled bodies can echo Authorization/signature
            # material. Never persist them in the plaintext last_error column.
            error_message = f"permanent HTTP {delivery.status_code}"
            async with self._pool.connection() as conn:
                await conn.execute(
                    self._sql_quarantine,
                    (error_message[:1000], row_id, lease_token),
                )
            logger.error(
                "[adcp.task_webhook_outbox] permanent HTTP failure for task %s; row quarantined",
                task_id,
            )
            return True

        delay = self._retry_delay(int(attempt_count))
        http_status = delivery.status_code if delivery is not None else None
        if delivery is not None:
            error_message = f"HTTP {delivery.status_code}"
        elif error is not None:
            error_message = f"{type(error).__name__}: delivery failed"
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
        # Validation errors can originate in adopter-provided transport hooks,
        # whose messages may contain request credentials. Persist only the
        # local exception discriminator in the plaintext last_error column.
        error_message = f"permanent delivery validation failure: {type(error).__name__}"
        async with self._pool.connection() as conn:
            await conn.execute(
                self._sql_quarantine,
                (error_message[:1000], row_id, lease_token),
            )
        logger.error(
            "[adcp.task_webhook_outbox] permanent delivery failure for task %s; row quarantined",
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
    def _validate_delivery_sender(sender: WebhookSender) -> None:
        if not callable(getattr(sender, "send_prepared", None)):
            raise ValueError("webhook sender resolver must return a WebhookSender")
        if not getattr(sender, "_owns_client", False) or getattr(
            sender, "_allow_private_destinations", False
        ):
            raise ValueError(
                "PgTaskWebhookOutbox requires a WebhookSender using the SDK-owned "
                "IP-pinned transport with private destinations disabled"
            )
        if getattr(sender, "signs_with_rfc9421", False) is not True:
            raise ValueError("PgTaskWebhookOutbox requires an RFC 9421 signing sender")

    @staticmethod
    def _validate_signing_scope_id(signing_scope_id: str | None) -> None:
        if signing_scope_id is None:
            return
        if not signing_scope_id or not signing_scope_id.isprintable():
            raise ValueError("signing_scope_id must be a non-empty printable string")
        if len(signing_scope_id.encode("utf-8")) > MAX_SIGNING_SCOPE_ID_BYTES:
            raise ValueError(
                f"signing_scope_id must not exceed {MAX_SIGNING_SCOPE_ID_BYTES} UTF-8 bytes"
            )

    def _validate_scope_for_mode(
        self,
        signing_scope_id: str | None,
        *,
        authentication: TaskWebhookAuthentication | None,
        require_resolver_scope: bool,
    ) -> None:
        self._validate_signing_scope_id(signing_scope_id)
        if authentication is not None:
            if signing_scope_id is not None:
                raise ValueError(
                    "legacy webhook authentication must not carry an RFC 9421 signing scope"
                )
            return
        if self._sender is not None and signing_scope_id is not None:
            raise ValueError("fixed-sender outboxes must not carry a signing_scope_id")
        if (
            require_resolver_scope
            and self._sender_resolver is not None
            and signing_scope_id is None
        ):
            raise ValueError("sender-resolver outboxes require a signing_scope_id")

    async def _resolve_delivery_sender(self, signing_scope_id: str | None) -> WebhookSender:
        """Resolve and revalidate the sender at every delivery attempt."""
        self._validate_signing_scope_id(signing_scope_id)
        if self._sender is not None:
            if signing_scope_id is not None:
                raise ScopePermanentlyUnknown
            return self._sender

        resolver = self._sender_resolver
        if resolver is None or signing_scope_id is None:
            raise ScopePermanentlyUnknown
        try:
            resolution = await resolver.resolve(signing_scope_id)
        except (ScopePermanentlyUnknown, ScopeTransientlyUnavailable):
            raise
        except Exception:
            # Resolver diagnostics may contain key-service details. Keep the
            # durable row and logs on a bounded local discriminator only.
            raise ScopeTransientlyUnavailable from None
        try:
            if not isinstance(resolution, WebhookSenderResolution):
                raise ValueError("resolver returned an invalid sender resolution")
            sender = resolution.sender
            self._validate_delivery_sender(sender)
            sender_algorithm = getattr(getattr(sender, "_auth", None), "alg", None)
            if sender_algorithm not in resolution.advertised_algorithms:
                raise ValueError("resolved sender algorithm was not advertised for its scope")
            sender_timeout = float(getattr(sender, "_timeout", 0.0))
            if self._lease_seconds < sender_timeout + 5:
                raise ValueError("resolved sender timeout exceeds the outbox lease budget")
        except Exception:
            # Treat malformed/adversarial resolver output as a permanent local
            # configuration error without persisting its diagnostics.
            raise ScopePermanentlyUnknown from None
        return sender

    async def _deliver_prepared(
        self,
        prepared: PreparedWebhook,
        signing_scope_id: str | None,
        authentication: TaskWebhookAuthentication | None = None,
    ) -> WebhookDeliveryResult:
        """Select the registered mode and send within one lease budget."""
        if authentication is None:
            sender = await self._resolve_delivery_sender(signing_scope_id)
            return await sender.send_prepared(prepared)
        if signing_scope_id is not None:
            raise ValueError("legacy webhook authentication cannot use an RFC 9421 scope")
        sender = self._legacy_sender(authentication)
        try:
            return await sender.send_prepared(prepared)
        finally:
            await sender.aclose()

    def _legacy_sender(self, authentication: TaskWebhookAuthentication) -> WebhookSender:
        """Build an SDK-owned, IP-pinned sender for encrypted legacy credentials."""
        self._validate_authentication(authentication)
        # Keep the HTTP attempt inside the outbox lease even for very short
        # adopter-configured leases. Private destinations remain disabled.
        timeout_seconds = max(0.1, min(10.0, self._lease_seconds - 5.0))
        common: dict[str, Any] = {
            "timeout_seconds": timeout_seconds,
            "allow_private_destinations": False,
            "allowed_destination_ports": self._legacy_allowed_destination_ports,
            "transport_hooks": self._legacy_transport_hooks,
        }
        if authentication.scheme == "Bearer":
            return WebhookSender.from_bearer_token(authentication.credentials, **common)
        return WebhookSender.from_adcp_legacy_hmac(
            authentication.credentials.encode("utf-8"),
            key_id="adcp-task-registration",
            **common,
        )

    def _validate_authentication(
        self,
        authentication: TaskWebhookAuthentication | None,
    ) -> None:
        if authentication is None:
            return
        if not isinstance(authentication, TaskWebhookAuthentication):
            raise ValueError("webhook authentication must be TaskWebhookAuthentication or None")
        if authentication.scheme not in _LEGACY_AUTH_SCHEMES:
            raise ValueError(
                f"unsupported task webhook authentication scheme {authentication.scheme!r}; "
                "supported legacy schemes are 'Bearer' and 'HMAC-SHA256'"
            )
        if authentication.scheme == "HMAC-SHA256" and not self.legacy_hmac_fallback:
            raise ValueError(
                "task webhook HMAC-SHA256 authentication requires "
                "legacy_hmac_fallback=True and a matching capability advertisement"
            )
        if any(char in authentication.credentials for char in ("\r", "\n", "\x00")):
            raise ValueError("webhook authentication credentials contain a control character")

    @staticmethod
    def _protect_delivery_body(
        body: bytes,
        authentication: TaskWebhookAuthentication | None,
    ) -> bytes:
        """Keep old RFC rows byte-compatible; wrap encrypted legacy secrets."""
        if authentication is None:
            return body
        return json.dumps(
            {
                "task_webhook_delivery_version": _ENCRYPTED_DELIVERY_VERSION,
                "body": base64.b64encode(body).decode("ascii"),
                "authentication": {
                    "scheme": authentication.scheme,
                    "credentials": authentication.credentials,
                },
            },
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")

    def _open_delivery_body(
        self,
        protected_body: bytes,
    ) -> tuple[bytes, TaskWebhookAuthentication | None]:
        """Decode a legacy-auth envelope or accept a pre-feature RFC body."""
        try:
            value = json.loads(protected_body)
        except (json.JSONDecodeError, UnicodeDecodeError):
            return protected_body, None
        if not isinstance(value, dict) or "task_webhook_delivery_version" not in value:
            return protected_body, None
        if value.get("task_webhook_delivery_version") != _ENCRYPTED_DELIVERY_VERSION:
            raise ValueError("unsupported encrypted task webhook delivery version")
        authentication_value = value.get("authentication")
        if not isinstance(authentication_value, dict):
            raise ValueError("encrypted task webhook authentication is missing")
        scheme = authentication_value.get("scheme")
        credentials = authentication_value.get("credentials")
        encoded_body = value.get("body")
        if (
            not isinstance(scheme, str)
            or not isinstance(credentials, str)
            or not isinstance(encoded_body, str)
        ):
            raise ValueError("encrypted task webhook delivery envelope is invalid")
        try:
            authentication = TaskWebhookAuthentication(
                scheme=scheme,
                credentials=credentials,
            )
            body = base64.b64decode(encoded_body, validate=True)
        except (TypeError, ValueError) as exc:
            raise ValueError("encrypted task webhook delivery envelope is invalid") from exc
        self._validate_authentication(authentication)
        return body, authentication

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
        signing_scope_id: str | None = None,
    ) -> bytes:
        """Canonical associated data binding every routing/security field."""
        fields: list[str | None] = [
            account_id,
            task_id,
            task_type,
            status,
            url,
            operation_id,
            idempotency_key,
        ]
        # Preserve the exact seven-field AAD for rows written before the
        # signing-scope migration. Scoped rows append the trusted scope and
        # therefore fail authenticated decryption if the DB column is swapped.
        if signing_scope_id is not None:
            fields.append(signing_scope_id)
        return json.dumps(
            fields,
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
