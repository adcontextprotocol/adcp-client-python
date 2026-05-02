"""SQL-backed :class:`adcp.server.SubdomainTenantRouter`.

Resolves the request ``Host`` header to a :class:`adcp.server.Tenant`
by hitting the ``tenants`` table. The framework's ASGI middleware
threads the result onto the ``current_tenant()`` contextvar; the
adopter's ``context_factory`` reads it to populate
:attr:`ToolContext.tenant_id`.

Cached lookups via a small in-process LRU keep the hot path off the
database. The cache is invalidated when an admin update changes
``tenants.host`` (admin API publishes a tenant-changed event;
production sellers wire a Postgres LISTEN / pub-sub for the
invalidation).
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from sqlalchemy import select

from adcp.server import Tenant

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import async_sessionmaker

from .models import Tenant as TenantRow


class SqlSubdomainTenantRouter:
    """Production-shape adopter impl.

    Adopters with a separate cache layer (Redis, in-process LRU)
    swap this out — the Protocol is one async method.
    """

    def __init__(
        self,
        *,
        sessionmaker: async_sessionmaker,
        cache_size: int = 256,
    ) -> None:
        self._sessionmaker = sessionmaker
        self._cache: dict[str, Tenant | None] = {}
        self._cache_size = cache_size
        self._cache_lock = asyncio.Lock()

    async def resolve(self, host: str) -> Tenant | None:
        # Tiny LRU-ish cache — first-write-wins eviction. Adopters
        # under heavy traffic swap to a real LRU (functools.lru_cache
        # doesn't compose with async, so we hand-roll).
        async with self._cache_lock:
            if host in self._cache:
                return self._cache[host]
        async with self._sessionmaker() as session:
            result = await session.execute(select(TenantRow).where(TenantRow.host == host))
            row = result.scalar_one_or_none()
        tenant: Tenant | None
        if row is None or row.status != "active":
            # Suspended / archived tenants resolve to None — same
            # outer behavior as unknown hosts (404 with no body).
            tenant = None
        else:
            tenant = Tenant(
                id=row.id,
                display_name=row.display_name,
                ext={"db_status": row.status, **(row.ext or {})},
            )
        async with self._cache_lock:
            if len(self._cache) >= self._cache_size:
                # Simple eviction: drop one arbitrary entry. With
                # cache_size=256 across thousands of QPS this
                # smoothly cycles. Adopters needing strict LRU swap
                # in ``cachetools.LRUCache``.
                self._cache.pop(next(iter(self._cache)))
            self._cache[host] = tenant
        return tenant

    def invalidate(self, host: str) -> None:
        """Drop a single host from the cache.

        Called by the admin API after CRUD that touches the tenants
        table. Production sellers wire a Postgres LISTEN to broadcast
        invalidations across worker processes.
        """
        self._cache.pop(host, None)

    def clear_cache(self) -> None:
        """Drop all cached entries — useful after bulk edits."""
        self._cache.clear()


__all__ = ["SqlSubdomainTenantRouter"]
