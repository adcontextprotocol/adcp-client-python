"""Publisher pins narrow operator keys by RFC 7638 public-key thumbprint."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any

from joserfc.errors import JoseError
from joserfc.jwk import ECKey, OKPKey, RSAKey
from joserfc.util import urlsafe_b64decode

PublisherPins = Mapping[str, Sequence[Mapping[str, Any]] | None]

#: The public members RFC 7638 hashes, per key type. A key missing one (or
#: carrying a non-string one) has no thumbprint and matches nothing.
_THUMBPRINT_MEMBERS: dict[
    str, tuple[type[OKPKey] | type[ECKey] | type[RSAKey], tuple[str, ...]]
] = {
    "OKP": (OKPKey, ("crv", "x")),
    "EC": (ECKey, ("crv", "x", "y")),
    "RSA": (RSAKey, ("e", "n")),
}


def jwk_thumbprint(key: Mapping[str, Any]) -> bytes | None:
    """Return the RFC 7638 digest (``joserfc``); incomplete public keys match nothing.

    Only the public members go to ``joserfc``: a pin or directory entry is a
    public key, and the thumbprint never depends on anything else.
    """
    shape = _THUMBPRINT_MEMBERS.get(str(key.get("kty")))
    if shape is None:
        return None
    key_type, members = shape
    if any(not isinstance(key.get(m), str) or not key[m] for m in members):
        return None
    try:
        imported = key_type.import_key({"kty": key["kty"], **{m: key[m] for m in members}})
        return urlsafe_b64decode(imported.thumbprint().encode("ascii"))
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
