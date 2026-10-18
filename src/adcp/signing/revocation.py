"""Revocation list for the AdCP request-signing profile.

Revocation lookups run BEFORE cryptographic verification (step 9, before
step 10). This prevents an attacker from amplifying work on the verifier by
replaying a revoked key's valid signature: rejecting before the expensive
Ed25519/ECDSA verify keeps the cost of rejection bounded.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Protocol


class RevocationChecker(Protocol):
    """Returns True iff `keyid` is revoked."""

    def __call__(self, keyid: str) -> bool: ...


@dataclass
class RevocationList:
    """In-memory representation of the revocation list snapshot."""

    issuer: str
    updated: str
    next_update: str
    revoked_kids: frozenset[str] = field(default_factory=frozenset)
    revoked_jtis: frozenset[str] = field(default_factory=frozenset)

    def is_revoked(self, keyid: str) -> bool:
        return keyid in self.revoked_kids

    def is_stale(self, now: datetime) -> bool:
        """True iff the list's `next_update` timestamp is in the past relative to `now`.

        A stale list must not be trusted to decide revocation — an attacker who
        takes the issuer offline could indefinitely extend a revoked key's life.
        """
        # Python 3.10's fromisoformat rejects the trailing `Z` (fixed in 3.11);
        # the AdCP vectors use `Z`, so normalize before parsing.
        raw = self.next_update
        if raw.endswith("Z"):
            raw = raw[:-1] + "+00:00"
        ts = datetime.fromisoformat(raw)
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        return ts < now

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> RevocationList:
        return cls(
            issuer=data["issuer"],
            updated=data["updated"],
            next_update=data["next_update"],
            revoked_kids=frozenset(data.get("revoked_kids", ())),
            revoked_jtis=frozenset(data.get("revoked_jtis", ())),
        )
