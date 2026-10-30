"""The principal a reporting subscription belongs to (AdCP 3.3, experimental).

Kept dependency-free so the reporting runtime, the materializer and the server
can all name it without importing each other.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class PrincipalRef:
    """The identity a reporting subscription belongs to: ``(tenant_id, principal_id)``.

    ``principal_id`` is the seller-resolved, stable identity of the
    authenticated caller (the same value the reporting caller resolver yields
    as ``consumer_id``), never anything read from request content.
    ``tenant_id`` names the tenant on a multi-tenant server and is ``None`` on
    a single-tenant one. One reporting engine serves one tenant: state is keyed
    by ``principal_id``, and the tenant is recorded with it.

    The TypeScript SDK spells it ``PrincipalRef { tenantId, principalId }``.
    """

    tenant_id: str | None
    principal_id: str

    def __post_init__(self) -> None:
        if not isinstance(self.principal_id, str) or not self.principal_id:
            raise ValueError("PrincipalRef.principal_id must be a non-empty string")
        if self.tenant_id is not None and (
            not isinstance(self.tenant_id, str) or not self.tenant_id
        ):
            raise ValueError("PrincipalRef.tenant_id must be a non-empty string or None")

    @property
    def subject(self) -> str:
        """The key reporting state and ledger ownership use (``principal_id``)."""
        return self.principal_id

    @classmethod
    def coerce(cls, value: Any, *, tenant_id: str | None = None) -> PrincipalRef:
        """Accept a ``PrincipalRef``, a ``PrincipalIdentity`` or a principal id string."""
        if isinstance(value, PrincipalRef):
            return value
        if isinstance(value, str):
            return cls(tenant_id, value)
        subject = getattr(value, "subject", None)
        if isinstance(subject, str):
            return cls(getattr(value, "tenant_id", tenant_id), subject)
        raise TypeError("expected a PrincipalRef, a PrincipalIdentity or a principal id")


__all__ = ["PrincipalRef"]
