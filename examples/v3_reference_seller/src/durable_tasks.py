"""Production task-registry and webhook-outbox wiring.

The web process and the separately supervised worker process each construct
this bundle from the same environment.  They share PostgreSQL and encryption
material, but own separate connection pools and webhook senders.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import logging
import os
from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING

from adcp.decisioning import PgTaskRegistry, PgTaskWebhookOutbox
from adcp.server.idempotency import IdempotencyStore, PgBackend
from adcp.webhook_sender import WebhookSender

from .workflow_queue import PgWorkflowQueue

if TYPE_CHECKING:
    from psycopg_pool import AsyncConnectionPool

logger = logging.getLogger(__name__)


DEFAULT_RETRY_HORIZON_SECONDS = 86_400
MAX_RETRY_HORIZON_SECONDS = 604_800

_REQUIRED_ENV = (
    "ADCP_TASK_DATABASE_URL",
    "ADCP_TASK_WEBHOOK_ENCRYPTION_KEY",
    "ADCP_WEBHOOK_SIGNING_KEY_PATH",
    "ADCP_WEBHOOK_SIGNING_KEY_ID",
)


def _production_env(environ: Mapping[str, str]) -> bool:
    return environ.get("ADCP_ENV", "").strip().lower() in {"prod", "production"}


def _decode_encryption_key(encoded: str) -> bytes:
    """Decode a base64 secret without ever including it in diagnostics."""
    try:
        key = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError(
            "ADCP_TASK_WEBHOOK_ENCRYPTION_KEY must be valid base64 for exactly 32 bytes"
        ) from exc
    if len(key) != 32:
        raise ValueError("ADCP_TASK_WEBHOOK_ENCRYPTION_KEY must decode to exactly 32 bytes")
    return key


def _retry_horizon(environ: Mapping[str, str]) -> int:
    raw = environ.get(
        "ADCP_TASK_WEBHOOK_RETRY_HORIZON_SECONDS",
        str(DEFAULT_RETRY_HORIZON_SECONDS),
    )
    try:
        horizon = int(raw)
    except ValueError as exc:
        raise ValueError("ADCP_TASK_WEBHOOK_RETRY_HORIZON_SECONDS must be an integer") from exc
    if not DEFAULT_RETRY_HORIZON_SECONDS <= horizon <= MAX_RETRY_HORIZON_SECONDS:
        raise ValueError(
            "ADCP_TASK_WEBHOOK_RETRY_HORIZON_SECONDS must be between "
            f"{DEFAULT_RETRY_HORIZON_SECONDS} and {MAX_RETRY_HORIZON_SECONDS}"
        )
    return horizon


@dataclass(frozen=True)
class DurableTaskWiring:
    """One process's validated durable task/webhook resources."""

    pool: AsyncConnectionPool
    lock_pool: AsyncConnectionPool | None
    sender: WebhookSender
    outbox: PgTaskWebhookOutbox
    registry: PgTaskRegistry
    workflow_queue: PgWorkflowQueue
    idempotency_backend: PgBackend | None
    idempotency: IdempotencyStore | None
    retry_horizon_seconds: int
    signing_algorithm: str

    @classmethod
    def from_env(
        cls,
        environ: Mapping[str, str] | None = None,
        *,
        required: bool | None = None,
        include_idempotency: bool = True,
    ) -> DurableTaskWiring | None:
        """Validate configuration and build resources without opening sockets.

        Local development may omit the entire durable bundle. Production and
        worker processes require all fields. A partial configuration always
        fails before the HTTP listener or worker loop starts.
        """
        values = os.environ if environ is None else environ
        must_configure = _production_env(values) if required is None else required
        configured = [name for name in _REQUIRED_ENV if values.get(name)]
        if not configured and not must_configure:
            return None

        missing = [name for name in _REQUIRED_ENV if not values.get(name)]
        if missing:
            raise ValueError(
                "Durable task/webhook configuration is incomplete; missing: " + ", ".join(missing)
            )

        # Imported only when durable mode is selected so the lightweight local
        # example still runs without the optional ``adcp[pg]`` dependency.
        from psycopg_pool import AsyncConnectionPool

        retry_horizon = _retry_horizon(values)
        signing_algorithm = values.get("ADCP_WEBHOOK_SIGNING_ALG", "ed25519")
        encryption_key = _decode_encryption_key(values["ADCP_TASK_WEBHOOK_ENCRYPTION_KEY"])
        sender = WebhookSender.from_pem(
            values["ADCP_WEBHOOK_SIGNING_KEY_PATH"],
            key_id=values["ADCP_WEBHOOK_SIGNING_KEY_ID"],
            alg=signing_algorithm,
        )
        pool = AsyncConnectionPool(
            values["ADCP_TASK_DATABASE_URL"],
            min_size=1,
            max_size=10,
            open=False,
        )
        # Advisory-lock operations reserve a connection for the whole handler
        # call, so the SDK requires a distinct pool to prevent self-deadlock.
        lock_pool = (
            AsyncConnectionPool(
                values["ADCP_TASK_DATABASE_URL"],
                min_size=1,
                max_size=10,
                open=False,
            )
            if include_idempotency
            else None
        )
        outbox = PgTaskWebhookOutbox(
            pool=pool,
            sender=sender,
            encryption_key=encryption_key,
            delivery_retry_horizon_seconds=retry_horizon,
        )
        registry = PgTaskRegistry(pool=pool, task_webhook_outbox=outbox)
        workflow_queue = PgWorkflowQueue(pool=pool, registry=registry)
        idempotency_backend = (
            PgBackend(pool=pool, lock_pool=lock_pool) if lock_pool is not None else None
        )
        idempotency = (
            IdempotencyStore(
                backend=idempotency_backend,
                ttl_seconds=retry_horizon,
                raise_on_persist_error=True,
            )
            if idempotency_backend is not None
            else None
        )
        return cls(
            pool=pool,
            lock_pool=lock_pool,
            sender=sender,
            outbox=outbox,
            registry=registry,
            workflow_queue=workflow_queue,
            idempotency_backend=idempotency_backend,
            idempotency=idempotency,
            retry_horizon_seconds=retry_horizon,
            signing_algorithm=signing_algorithm,
        )

    async def startup(self) -> None:
        """Open the process-local pool and idempotently create SDK tables."""
        try:
            await self.pool.open()
            if self.lock_pool is not None:
                await self.lock_pool.open()
            await self.registry.create_schema()
            await self.outbox.create_schema()
            await self.workflow_queue.create_schema()
            if self.idempotency_backend is not None:
                await self.idempotency_backend.create_schema()
        except asyncio.CancelledError:
            cleanup = asyncio.create_task(self.shutdown())
            # Once startup owns resources, repeated cancellation must not
            # strand them. Shield the cleanup task and preserve cancellation
            # for the caller after every close has had a chance to run.
            while not cleanup.done():
                try:
                    await asyncio.shield(cleanup)
                except asyncio.CancelledError:
                    continue
            try:
                cleanup.result()
            except asyncio.CancelledError:
                logger.exception("Durable resource cleanup was cancelled")
            except Exception:
                logger.exception("Durable resource cleanup failed after startup cancellation")
            raise
        except Exception:
            try:
                await self.shutdown()
            except Exception:
                logger.exception("Durable resource cleanup failed after startup error")
            raise

    async def shutdown(self) -> None:
        """Best-effort close every resource owned by this process."""
        first_error: BaseException | None = None
        closers = [self.sender.aclose]
        if self.lock_pool is not None:
            closers.append(self.lock_pool.close)
        closers.append(self.pool.close)
        for close in closers:
            try:
                await close()
            except asyncio.CancelledError as exc:
                logger.exception("Durable task resource close was cancelled")
                if first_error is None:
                    first_error = exc
            except Exception as exc:
                logger.exception("Failed to close a durable task resource")
                if first_error is None:
                    first_error = exc
        if first_error is not None:
            raise first_error


__all__ = ["DEFAULT_RETRY_HORIZON_SECONDS", "DurableTaskWiring"]
