"""Public, local identifier validation and canonicalization (no network I/O)."""

from __future__ import annotations

import ipaddress
import re
from dataclasses import dataclass
from urllib.parse import urlsplit

import idna

from adcp.types.domains.enums.identifier_types import PropertyIdentifierTypes


class IdentifierValidationError(ValueError):
    """An identifier is invalid for its declared type.

    ``identifier_type`` and ``reason`` are safe machine-readable diagnostics;
    error messages deliberately exclude the supplied value.
    """

    def __init__(self, identifier_type: str, reason: str):
        self.identifier_type = identifier_type
        self.reason = reason
        super().__init__(f"Invalid {identifier_type} identifier: {reason}")


@dataclass(frozen=True)
class NormalizedIdentifier:
    type: str
    value: str
    original_value: str

    @property
    def rewritten(self) -> bool:
        return self.value != self.original_value

    def as_dict(self) -> dict[str, str]:
        return {"type": self.type, "value": self.value}


def normalize_domain(value: str, *, allow_wildcard: bool = True) -> NormalizedIdentifier:
    """Canonicalize a hostname (IDNA, lowercase, no trailing dot).

    URLs, ports, IP addresses and embedded wildcards are rejected. A leading
    ``*.`` is accepted only when ``allow_wildcard`` is true. No scheme or path
    is silently discarded. This validates syntax, not registration/ownership.
    """
    if not isinstance(value, str):
        raise IdentifierValidationError("domain", "expected_string")
    canonical = value.strip().removesuffix(".").lower()
    wildcard = canonical.startswith("*.")
    host = canonical[2:] if wildcard else canonical
    if wildcard and not allow_wildcard:
        raise IdentifierValidationError("domain", "wildcard_not_allowed")
    if any(c in host for c in "/:@?#*\\"):
        raise IdentifierValidationError("domain", "expected_hostname")
    try:
        host = idna.encode(host, uts46=True, transitional=False).decode("ascii")
    except idna.IDNAError:
        raise IdentifierValidationError("domain", "invalid_idna") from None
    labels = host.split(".")
    if (
        len(host) > 253
        or len(labels) < 2
        or not all(re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label) for label in labels)
    ):
        raise IdentifierValidationError("domain", "invalid_hostname")
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        raise IdentifierValidationError("domain", "ip_address_not_allowed")
    return NormalizedIdentifier("domain", ("*." if wildcard else "") + host, value)


def validate_publisher_domain(value: str) -> str:
    """Return a canonical publisher hostname; reject URLs and wildcards."""
    return normalize_domain(value, allow_wildcard=False).value


def normalize_identifier(identifier_type: str, value: str) -> NormalizedIdentifier:
    """Validate a declared identifier and report any canonical rewrite.

    Bundle/package and opaque IDs preserve case. Numeric store IDs contain
    digits only; Fire TV ASINs canonicalize to uppercase. Unknown types fail
    explicitly rather than being mistaken for domains.
    """
    try:
        kind = PropertyIdentifierTypes(identifier_type).value
    except ValueError:
        raise IdentifierValidationError(identifier_type, "unknown_type") from None
    if not isinstance(value, str):
        raise IdentifierValidationError(kind, "expected_string")
    if kind in {"domain", "subdomain"}:
        try:
            result = normalize_domain(value, allow_wildcard=kind == "domain")
        except IdentifierValidationError as exc:
            raise IdentifierValidationError(kind, exc.reason) from None
        return NormalizedIdentifier(kind, result.value, value)
    canonical = value.strip()
    if not canonical or any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in canonical):
        raise IdentifierValidationError(kind, "empty_or_whitespace")
    if kind in {"ios_bundle", "apple_tv_bundle", "bundle_id"}:
        if not re.fullmatch(r"[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+", canonical):
            raise IdentifierValidationError(kind, "invalid_bundle")
    elif kind in {"android_package", "google_play_id"}:
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z][A-Za-z0-9_]*)+", canonical):
            raise IdentifierValidationError(kind, "invalid_package")
    elif kind in {"apple_app_store_id", "roku_store_id", "apple_podcast_id"}:
        if not re.fullmatch(r"[0-9]+", canonical):
            raise IdentifierValidationError(kind, "expected_numeric_store_id")
    elif kind == "fire_tv_asin":
        canonical = canonical.upper()
        if not re.fullmatch(r"[A-Z0-9]{10}", canonical):
            raise IdentifierValidationError(kind, "invalid_asin")
    elif kind == "rss_url":
        try:
            parsed = urlsplit(canonical)
            if (
                parsed.scheme not in {"http", "https"}
                or not parsed.hostname
                or parsed.username is not None
            ):
                raise ValueError
            # Accessing .port validates both numeric syntax and its range.
            parsed.port
            normalize_domain(parsed.hostname, allow_wildcard=False)
        except ValueError:
            raise IdentifierValidationError(kind, "expected_http_url") from None
    return NormalizedIdentifier(kind, canonical, value)


def validate_identifier(identifier_type: str, value: str) -> NormalizedIdentifier:
    """Validate and return the canonical identifier and its rewrite information."""
    return normalize_identifier(identifier_type, value)


__all__ = [
    "IdentifierValidationError",
    "NormalizedIdentifier",
    "normalize_domain",
    "normalize_identifier",
    "validate_identifier",
    "validate_publisher_domain",
]
