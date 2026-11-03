"""Publisher pins narrow operator keys by RFC 7638 public-key thumbprint."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any

from joserfc.errors import JoseError
from joserfc.jwk import ECKey, OKPKey, RSAKey

PublisherPins = Mapping[str, Sequence[Mapping[str, Any]] | None]

_KEY_TYPES: dict[str, type[OKPKey] | type[ECKey] | type[RSAKey]] = {
    "OKP": OKPKey,
    "EC": ECKey,
    "RSA": RSAKey,
}


def jwk_thumbprint(key: Mapping[str, Any]) -> str | None:
    """Return the RFC 7638 thumbprint (``joserfc``); incomplete public keys match nothing.

    Only the members RFC 7638 hashes (the key type's required members in
    ``joserfc``) go to ``joserfc``: a pin or directory entry is a public key,
    and the thumbprint never depends on anything else. ``joserfc`` refuses a
    key missing one of them or carrying a malformed one.
    """
    key_type = _KEY_TYPES.get(str(key.get("kty")))
    if key_type is None:
        return None
    public = {
        name: key[name]
        for name, member in key_type.value_registry.items()
        if member.required and name in key
    }
    try:
        # Keep the boundary explicit when joserfc's Self annotation is
        # unavailable to the SDK's Python 3.10 type-check target.
        thumbprint: str = key_type.import_key({"kty": key["kty"], **public}).thumbprint()
        return thumbprint
    except (JoseError, ValueError, TypeError):
        return None


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
