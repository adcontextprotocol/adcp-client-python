"""Publisher pins narrow operator keys by RFC 7638 public-key thumbprint."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any

PublisherPins = Mapping[str, Sequence[Mapping[str, Any]] | None]


def jwk_thumbprint(key: Mapping[str, Any]) -> bytes | None:
    """Return the RFC 7638 digest; incomplete public keys match nothing."""
    members = {
        "OKP": ("crv", "kty", "x"),
        "EC": ("crv", "kty", "x", "y"),
        "RSA": ("e", "kty", "n"),
    }.get(str(key.get("kty")))
    if members is None or any(not isinstance(key.get(m), str) or not key[m] for m in members):
        return None
    canonical = json.dumps({m: key[m] for m in members}, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).digest()


def matches_publisher_pins(key: Mapping[str, Any], pins: PublisherPins, *, now: float) -> bool:
    """Require every publisher pin; ``now`` is the signature creation time.

    A pin revoked after creation still accepts a valid in-flight signature.
    """
    thumbprint = jwk_thumbprint(key)
    if thumbprint is None:
        return False
    for entries in pins.values():
        if entries is None:  # Publisher has no pin; an empty pin accepts nothing.
            continue
        if not any(
            jwk_thumbprint(entry) == thumbprint and _not_revoked(entry, now) for entry in entries
        ):
            return False
    return True


def _not_revoked(key: Mapping[str, Any], now: float) -> bool:
    value = key.get("revoked_at")
    if value is None:
        return True
    if not isinstance(value, str):
        return False
    try:
        revoked_at = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return revoked_at.tzinfo is not None and revoked_at.timestamp() > now
    except ValueError:
        return False


__all__ = ["PublisherPins", "jwk_thumbprint", "matches_publisher_pins"]
