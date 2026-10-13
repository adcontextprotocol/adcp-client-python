"""Explicit origin-to-principal binding for experimental Web Bot Auth identities."""

from __future__ import annotations

import threading
from typing import Protocol

from adcp.signing.canonical import canonical_origin


class OriginBindingResolver(Protocol):
    """Application-owned durable lookup; discovery must never write bindings."""

    async def __call__(self, origin: str) -> str | None:
        raise NotImplementedError("implement the application-owned origin binding lookup")


class OriginBindings:
    """In-memory binding helper for one process; use a durable resolver in production.

    A principal may own several origins, but an origin maps to one principal.
    Migrating a principal that already uses RFC 9421 requires explicit approval.
    Once bound, the transport refuses legacy authentication at that origin.
    """

    def __init__(self) -> None:
        self._bindings: dict[str, str] = {}
        self._lock = threading.Lock()

    def bind(
        self,
        origin: str,
        principal_id: str,
        *,
        existing_rfc9421_principal: bool = False,
        migration_approved: bool = False,
    ) -> None:
        origin = canonical_origin(origin)
        if not origin.startswith("https://") or not principal_id:
            raise ValueError("binding requires an HTTPS origin and a principal ID")
        if existing_rfc9421_principal and not migration_approved:
            raise ValueError("binding an existing RFC 9421 principal requires explicit approval")
        with self._lock:
            existing = self._bindings.get(origin)
            if existing is not None and existing != principal_id:
                raise ValueError("an origin cannot be rebound to another principal")
            self._bindings[origin] = principal_id

    async def __call__(self, origin: str) -> str | None:
        with self._lock:
            return self._bindings.get(canonical_origin(origin))


__all__ = ["OriginBindingResolver", "OriginBindings"]
