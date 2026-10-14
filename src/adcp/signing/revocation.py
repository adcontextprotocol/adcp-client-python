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


@dataclass(frozen=True)
class RevocationDetails:
    """Optional RFC 5280 reason and the earliest known invalidity date."""

    reason: str | None = None
    invalidity_date: datetime | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> RevocationDetails:
        reason = data.get("reason")
        if reason is not None and (
            not isinstance(reason, str)
            or reason
            not in {
                "unspecified",
                "keyCompromise",
                "cACompromise",
                "affiliationChanged",
                "superseded",
                "cessationOfOperation",
                "certificateHold",
                "removeFromCRL",
                "privilegeWithdrawn",
                "aACompromise",
            }
        ):
            raise ValueError("unknown RFC 5280 revocation reason")
        raw = data.get("invalidity_date")
        date = None
        if raw is not None:
            if not isinstance(raw, str):
                raise ValueError("invalidity_date must be an RFC 3339 timestamp")
            date = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            if date.tzinfo is None:
                raise ValueError("invalidity_date must carry a timezone")
        return cls(reason=reason, invalidity_date=date)

    def merge(self, newer: RevocationDetails) -> RevocationDetails:
        dates = [date for date in (self.invalidity_date, newer.invalidity_date) if date is not None]
        undated_compromise = any(
            detail.reason == "keyCompromise" and detail.invalidity_date is None
            for detail in (self, newer)
        )
        reason = (
            "keyCompromise"
            if "keyCompromise" in {self.reason, newer.reason}
            else self.reason or newer.reason
        )
        # An undated compromise exposes all prior receipts. Attaching a later
        # date would narrow that previously observed exposure window.
        return RevocationDetails(
            reason=reason,
            invalidity_date=None if undated_compromise or not dates else min(dates),
        )

    def exposes(self, received_at: datetime) -> bool:
        """Compromise exposure uses seller receipt time, never signature creation."""
        return self.reason == "keyCompromise" and (
            self.invalidity_date is None or received_at >= self.invalidity_date
        )


@dataclass
class RevocationList:
    """In-memory representation of the revocation list snapshot."""

    issuer: str
    updated: str
    next_update: str
    revoked_kids: frozenset[str] = field(default_factory=frozenset)
    revoked_jtis: frozenset[str] = field(default_factory=frozenset)
    revocation_details: dict[str, RevocationDetails] = field(default_factory=dict)

    def retain(self, previous: RevocationList) -> None:
        """Never withdraw revocations or weaken previously observed details."""
        if self.issuer != previous.issuer:
            raise ValueError("cannot merge revocations from different issuers")
        self.revoked_kids |= previous.revoked_kids
        self.revoked_jtis |= previous.revoked_jtis
        for kid, detail in previous.revocation_details.items():
            self.revocation_details[kid] = detail.merge(self.revocation_details.get(kid, detail))

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
        kids = data.get("revoked_kids", [])
        jtis = data.get("revoked_jtis", [])
        if any(
            not isinstance(values, (list, tuple)) or any(not isinstance(v, str) for v in values)
            for values in (kids, jtis)
        ):
            raise ValueError("revocation identifiers must be arrays of strings")
        details = data.get("revocation_details", {})
        if not isinstance(details, dict) or any(
            kid not in kids or not isinstance(detail, dict) for kid, detail in details.items()
        ):
            raise ValueError("revocation_details must describe revoked_kids only")
        return cls(
            issuer=data["issuer"],
            updated=data["updated"],
            next_update=data["next_update"],
            revoked_kids=frozenset(data.get("revoked_kids", ())),
            revoked_jtis=frozenset(data.get("revoked_jtis", ())),
            revocation_details={
                kid: RevocationDetails.from_dict(detail) for kid, detail in details.items()
            },
        )
