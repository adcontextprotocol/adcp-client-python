"""Webhook receiver-side dedup store.

Reuses :class:`IdempotencyBackend` (same Memory / Pg backends as the
request-side store) but has different semantics, per the adcp webhook
receiver requirements:

* No payload-hash equivalence check. The spec explicitly says receivers do
  NOT verify payload equivalence across key reuse — first copy wins, any
  later copy with the same key is silently deduped. Sender bugs that reuse
  a key with a changed payload are a sender problem.
* No ``IDEMPOTENCY_CONFLICT`` raise path. A duplicate is a no-op, not an
  error — receivers MUST return 2xx on a duplicate so the at-least-once
  sender's retry back-off doesn't fire.
* 24h default TTL (the spec minimum). Webhook senders SHOULD NOT retry
  beyond that window; entries arriving later are reprocessed as fresh
  events.

Dedup scope MUST be ``(authenticated_sender_identity, idempotency_key)``.
"Authenticated sender" means the 9421 verified ``keyid`` (or HMAC
credential), never a payload field — passing a payload-derived string in
here is an accident the receiver API should make awkward. The
:class:`adcp.signing.webhook_verifier.VerifiedWebhookSender.as_sender_identity`
helper gives you the right value.
"""

from __future__ import annotations

import asyncio
import logging
import time
import warnings
from collections.abc import Callable

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

# Sentinel stored in the backend's payload_hash slot. We don't hash a payload
# for webhook dedup, but the shared backend contract requires the field.
_SENTINEL_HASH = "webhook-dedup"


class WebhookDedupStore:
    """Dedup ``(sender_id, idempotency_key)`` pairs to suppress retried webhooks.

    :param backend: any :class:`IdempotencyBackend`. Same MemoryBackend or
        PgBackend type used by :class:`IdempotencyStore` is fine — the
        ``namespace`` parameter prefixes all sender IDs so request-side and
        webhook-side scopes can't alias even when sharing one backend instance.
    :param ttl_seconds: replay window. Must be within ``[86400, 604800]`` per
        the spec minimum. Defaults to 86400 (24h).
    :param namespace: prefix applied to every ``sender_id`` before it hits
        the backend. Defaults to ``"webhook"``, which is safe when the same
        backend is shared with :class:`IdempotencyStore` (request-side keys
        are scoped by a principal_id that isn't wrapped in this namespace,
        so collisions are impossible). Override only if you run multiple
        webhook scopes against one backend (e.g., separate dedup spaces for
        task webhooks vs list-change webhooks).
    """

    def __init__(
        self,
        backend: IdempotencyBackend,
        ttl_seconds: int = _MIN_TTL_SECONDS,
        *,
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
        self.backend = backend
        self.ttl_seconds = ttl_seconds
        self.namespace = namespace
        self._clock = clock
        self._warned_legacy_backend = False

    async def _put_if_absent(
        self,
        scope_key: str,
        key: str,
        entry: CachedResponse,
    ) -> bool:
        """Use backend atomicity or a warned process-local legacy fallback."""
        if await self.backend.supports_atomic_put_if_absent():
            return await self.backend.put_if_absent(scope_key, key, entry)

        if not self._warned_legacy_backend:
            warnings.warn(
                f"{type(self.backend).__name__} implements neither put_if_absent() "
                "nor hold(); using process-local webhook dedup locking. Implement "
                "an atomic operation for cross-process safety; this compatibility "
                "fallback is deprecated.",
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
            if await self.backend.get(scope_key, key) is not None:
                return False
            await self.backend.put(scope_key, key, entry)
            return True

    async def check_and_record(self, sender_id: str, idempotency_key: str) -> bool:
        """Atomically check for first-seen and record if new.

        Returns ``True`` when the pair is first-seen (event should be
        processed), ``False`` on duplicate (caller MUST still return 2xx to
        the sender — the event was delivered successfully, it's just a retry).

        The backend performs a single atomic insert-or-reject operation, so
        concurrent deliveries cannot both be reported as first-seen.
        """
        if not sender_id:
            raise ValueError("sender_id must be a non-empty string")
        if not idempotency_key:
            raise ValueError("idempotency_key must be a non-empty string")

        scoped_sender = f"{self.namespace}:{sender_id}"
        entry = CachedResponse(
            payload_hash=_SENTINEL_HASH,
            response={},
            expires_at_epoch=self._clock() + self.ttl_seconds,
        )
        try:
            inserted = await self._put_if_absent(scoped_sender, idempotency_key, entry)
        except Exception:
            # Same fail-open reasoning as the request-side store: log and
            # process. Swallowing the put failure means this event MIGHT
            # reprocess on retry, not that we drop it. Better than raising,
            # which would look like handler failure to the sender.
            logger.warning(
                "webhook dedup put failed for sender=%s key_prefix=%s — "
                "event processed but next retry will reprocess",
                sender_id,
                idempotency_key[:8],
                exc_info=True,
            )
            return True
        if not inserted:
            logger.debug(
                "webhook dedup: duplicate sender=%s key_prefix=%s",
                sender_id,
                idempotency_key[:8],
            )
        return inserted


__all__ = ["WebhookDedupStore"]
