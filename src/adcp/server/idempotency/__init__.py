"""Server-side idempotency middleware for AdCP mutating tool handlers.

Implements the seller side of AdCP #2315: extract ``idempotency_key``, look up
cached responses scoped by ``(authenticated_principal, idempotency_key)``, and
replay the cached response verbatim when a subsequent request carries the same
key + canonicalized-equivalent payload. Reject key reuse with a different
payload as ``IDEMPOTENCY_CONFLICT``.

The spec contract lives at
``adcontextprotocol/adcp/docs/building/implementation/security.mdx#idempotency``.

Typical usage::

    from adcp.server import ADCPHandler, IdempotencyStore, MemoryBackend, ToolContext
    from adcp.server.responses import capabilities_response

    idempotency = IdempotencyStore(
        backend=MemoryBackend(),
        ttl_seconds=86400,  # 24 hours, matches spec minimum
    )

    class MySeller(ADCPHandler):
        @idempotency.wrap
        async def create_media_buy(self, params, context=None):
            # ``params`` carries ``idempotency_key``; ``context.caller_identity``
            # identifies the principal. Without either, the middleware falls
            # through to this handler with no dedup (schema validation above
            # us rejects missing keys per AdCP #2315).
            return my_create_logic(params)

        async def get_adcp_capabilities(self, params, context=None):
            return capabilities_response(
                ["media_buy"],
                idempotency=idempotency.capability(),
            )

Callers who invoke the handler directly (tests, non-HTTP code paths) must
pass a :class:`adcp.server.base.ToolContext` with ``caller_identity`` set —
it's how the middleware scopes the cache namespace per-principal::

    ctx = ToolContext(caller_identity="buyer-acme")
    result = await seller.create_media_buy(
        {"idempotency_key": key, ...}, ctx
    )

Backends:

- :class:`MemoryBackend` — in-process dict with TTL; use for tests and
  single-process reference implementations.
- :class:`PgBackend` — scaffold for a SQLAlchemy/asyncpg-backed store that can
  commit cache writes atomically with business writes. Implementation arrives
  in a follow-up PR.
"""

from adcp.server.idempotency.backends import (
    CachedResponse,
    IdempotencyBackend,
    MemoryBackend,
    PgBackend,
)
from adcp.server.idempotency.canonicalize import (
    EXCLUDED_FIELDS,
    canonical_json_sha256,
    strip_excluded_fields,
)
from adcp.server.idempotency.store import IdempotencyStore, is_wrapped
from adcp.server.idempotency.webhook_dedup import WebhookDedupStore

__all__ = [
    "CachedResponse",
    "EXCLUDED_FIELDS",
    "IdempotencyBackend",
    "IdempotencyStore",
    "MemoryBackend",
    "PgBackend",
    "WebhookDedupStore",
    "canonical_json_sha256",
    "is_wrapped",
    "strip_excluded_fields",
]
