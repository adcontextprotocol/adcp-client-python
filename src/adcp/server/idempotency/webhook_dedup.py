"""Webhook receiver-side claim and completion store.

AdCP 3.2 binds a delivery key to one JCS-equivalent payload for the complete
retry window.  A receiver therefore needs more than a first-seen marker:

* same key and a different payload is a non-retryable conflict;
* an identical delivery owned by a live handler is retryable/in-progress;
* only a delivery durably marked handled is an acknowledged duplicate; and
* a failed handler releases its owner lease without erasing the key-to-payload
  binding.

This store reuses :class:`IdempotencyBackend` for durable storage and its
per-key ``hold`` primitive for atomic state transitions.  The full payload is
never stored here; callers supply its RFC 8785/JCS SHA-256 fingerprint.

The caller MUST scope claims with a trusted receiver/tenant identity plus a
stable publisher identity. Neither value may come from the payload, and the
publisher identity must survive signing-key rotation; a verified ``keyid`` is
authentication evidence, not a durable publisher namespace.
"""

from __future__ import annotations

import asyncio
import logging
import secrets
import time
import warnings
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Literal

from adcp.server.idempotency.backends import (
    CachedResponse,
    IdempotencyBackend,
    _legacy_backend_lock_state,
)

logger = logging.getLogger(__name__)

# Spec bound: 24h minimum. Upper bound matches the request-side store (7 days) —
# webhooks have no reason to cache longer.
_MIN_TTL_SECONDS = 86400
_MAX_TTL_SECONDS = 604800
_DEFAULT_PROCESSING_TTL_SECONDS = 300

WebhookClaimStatus = Literal["claimed", "handled", "in_progress", "conflict"]


@dataclass(frozen=True)
class WebhookDedupClaim:
    """Outcome of one atomic delivery claim.

    ``claim_token`` is present only for ``claimed`` and is an owner capability
    consumed by :meth:`WebhookDedupStore.complete` or
    :meth:`WebhookDedupStore.release`.  It must not be logged.
    """

    status: WebhookClaimStatus
    claim_token: str | None = None


class WebhookDedupOwnershipError(RuntimeError):
    """A handler attempted to settle a claim it no longer owns."""


class WebhookDedupStore:
    """Dedup ``(sender_id, idempotency_key)`` pairs to suppress retried webhooks.

    :param backend: any :class:`IdempotencyBackend`. Same MemoryBackend or
        PgBackend type used by :class:`IdempotencyStore` is fine — the
        ``namespace`` parameter prefixes all sender IDs so request-side and
        webhook-side scopes can't alias even when sharing one backend instance.
    :param ttl_seconds: payload-binding and replay window. Must be within ``[86400, 604800]`` per
        the spec minimum. Defaults to 86400 (24h).
    :param processing_ttl_seconds: processing-owner lease. An exact
        retry receives ``in_progress`` while the lease is live and may claim
        after it expires. Defaults to 300 seconds so a crashed handler cannot
        fence retries for the complete advertised delivery horizon. Configure
        it above the receiver's normal publication timeout; shorter leases
        increase the chance of overlapping owners and therefore require
        idempotent application publication. Must be positive and no longer
        than ``ttl_seconds``.
    :param namespace: prefix applied to every ``sender_id`` before it hits
        the backend. Defaults to ``"webhook"``, which is safe when the same
        backend is shared with :class:`IdempotencyStore` (request-side keys
        are scoped by a principal_id that isn't wrapped in this namespace,
        so collisions are impossible). Override only if you run multiple
        webhook scopes against one backend (e.g., separate dedup spaces for
        task webhooks vs list-change webhooks).
    :param clock: Deprecated compatibility argument. Expiry and lease decisions
        use :meth:`IdempotencyBackend.current_time` so durable backends and
        replicas share one authoritative clock.
    """

    def __init__(
        self,
        backend: IdempotencyBackend,
        ttl_seconds: int = _MIN_TTL_SECONDS,
        *,
        processing_ttl_seconds: int | None = None,
        namespace: str = "webhook",
        clock: Callable[[], float] = time.time,
    ) -> None:
        if not _MIN_TTL_SECONDS <= ttl_seconds <= _MAX_TTL_SECONDS:
            raise ValueError(
                f"ttl_seconds must be in [{_MIN_TTL_SECONDS}, {_MAX_TTL_SECONDS}] "
                f"per webhook spec minimum, got {ttl_seconds}"
            )
        if not namespace:
            raise ValueError("namespace must be a non-empty string")
        effective_processing_ttl = (
            _DEFAULT_PROCESSING_TTL_SECONDS
            if processing_ttl_seconds is None
            else processing_ttl_seconds
        )
        if not 1 <= effective_processing_ttl <= ttl_seconds:
            raise ValueError(
                "processing_ttl_seconds must be positive and no greater than "
                f"ttl_seconds, got {effective_processing_ttl}"
            )
        self.backend = backend
        self.ttl_seconds = ttl_seconds
        self.processing_ttl_seconds = effective_processing_ttl
        self.namespace = namespace
        _ = clock
        self._warned_legacy_backend = False

    @asynccontextmanager
    async def _hold(self, scope_key: str, key: str) -> AsyncIterator[None]:
        """Use the backend lock or a warned process-local compatibility lock."""
        if await self.backend.supports_atomic_hold():
            async with self.backend.hold(scope_key, key):
                yield
            return
        if not self._warned_legacy_backend:
            warnings.warn(
                f"{type(self.backend).__name__} does not implement hold(); using "
                "process-local webhook claim locking. This does not satisfy the "
                "multi-process AdCP 3.2 receiver contract; implement a durable "
                "atomic hold operation.",
                DeprecationWarning,
                stacklevel=3,
            )
            self._warned_legacy_backend = True

        slot = (scope_key, key)
        state = _legacy_backend_lock_state(self.backend)
        async with state.guard:
            lock = state.locks.get(slot)
            if lock is None:
                lock = asyncio.Lock()
                state.locks[slot] = lock
        async with lock:
            yield

    async def claim(
        self,
        sender_id: str,
        idempotency_key: str,
        payload_hash: str,
    ) -> WebhookDedupClaim:
        """Atomically claim a delivery or classify its retained state."""
        if not sender_id:
            raise ValueError("sender_id must be a non-empty string")
        if not idempotency_key:
            raise ValueError("idempotency_key must be a non-empty string")
        if not payload_hash:
            raise ValueError("payload_hash must be a non-empty string")

        scoped_sender = f"{self.namespace}:{sender_id}"
        async with self._hold(scoped_sender, idempotency_key):
            now = await self.backend.current_time()
            existing = await self.backend.get(scoped_sender, idempotency_key)
            if existing is not None:
                if existing.payload_hash != payload_hash:
                    return WebhookDedupClaim(status="conflict")
                state = existing.response.get("webhook_state")
                if state == "handled":
                    return WebhookDedupClaim(status="handled")
                lease_expires_at = existing.response.get("lease_expires_at")
                if (
                    state == "processing"
                    and isinstance(lease_expires_at, (int, float))
                    and lease_expires_at > now
                ):
                    return WebhookDedupClaim(status="in_progress")
                retain_until = existing.expires_at_epoch
            else:
                retain_until = now + self.ttl_seconds

            owner = secrets.token_urlsafe(32)
            claim_entry = CachedResponse(
                payload_hash=payload_hash,
                response={
                    "webhook_state": "processing",
                    "owner": owner,
                    "lease_expires_at": now + self.processing_ttl_seconds,
                },
                expires_at_epoch=retain_until,
            )
            if existing is None:
                await self.backend.put(scoped_sender, idempotency_key, claim_entry)
            else:
                await self.backend.replace(scoped_sender, idempotency_key, claim_entry)
            return WebhookDedupClaim(status="claimed", claim_token=owner)

    async def complete(
        self,
        sender_id: str,
        idempotency_key: str,
        payload_hash: str,
        claim_token: str,
    ) -> bool:
        """Durably mark an owned delivery handled.

        Returns ``True`` for the owner transition and ``False`` when the exact
        payload was already handled. Any missing, changed, or superseded claim
        fails closed with :class:`WebhookDedupOwnershipError`.
        """
        return await self._settle(
            sender_id,
            idempotency_key,
            payload_hash,
            claim_token,
            handled=True,
        )

    async def release(
        self,
        sender_id: str,
        idempotency_key: str,
        payload_hash: str,
        claim_token: str,
    ) -> bool:
        """Release an owned failed claim while retaining payload binding."""
        return await self._settle(
            sender_id,
            idempotency_key,
            payload_hash,
            claim_token,
            handled=False,
        )

    async def _settle(
        self,
        sender_id: str,
        idempotency_key: str,
        payload_hash: str,
        claim_token: str,
        *,
        handled: bool,
    ) -> bool:
        if not all((sender_id, idempotency_key, payload_hash, claim_token)):
            raise ValueError(
                "sender_id, idempotency_key, payload_hash, and claim_token are required"
            )
        scoped_sender = f"{self.namespace}:{sender_id}"
        async with self._hold(scoped_sender, idempotency_key):
            existing = await self.backend.get(scoped_sender, idempotency_key)
            if existing is None or existing.payload_hash != payload_hash:
                raise WebhookDedupOwnershipError("webhook delivery claim is missing or changed")
            state = existing.response.get("webhook_state")
            if handled and state == "handled":
                return False
            if state != "processing" or existing.response.get("owner") != claim_token:
                raise WebhookDedupOwnershipError("webhook delivery claim is not owned")
            response: dict[str, object]
            if handled:
                response = {"webhook_state": "handled"}
            else:
                response = {"webhook_state": "retryable"}
            await self.backend.replace(
                scoped_sender,
                idempotency_key,
                CachedResponse(
                    payload_hash=payload_hash,
                    response=response,
                    expires_at_epoch=existing.expires_at_epoch,
                ),
            )
            return True


__all__ = [
    "WebhookClaimStatus",
    "WebhookDedupClaim",
    "WebhookDedupOwnershipError",
    "WebhookDedupStore",
]
