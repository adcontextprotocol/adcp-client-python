"""Closed-set ``format_options[]`` validation.

Per AdCP 3.1 ``ProductFormatDeclaration.seller_preference`` (normative):

    `format_options[]` IS the closed set of accepted formats; anything
    outside the list is rejected at `create_media_buy` regardless of
    preference.

Sellers MUST reject a ``create_media_buy`` whose creative manifest
declares a ``format_kind`` outside the product's published
``format_options[]``. This module provides the pre-call guard.

Two helpers:

* :func:`validate_format_kind_in_options` — raises
  :class:`FormatKindNotInClosedSetError` when the kind is absent.
  Seller-side check; pair with an ``UNSUPPORTED_FEATURE`` error
  emitted on the wire response.
* :func:`find_declaration_by_kind` — looks up the matching declaration
  (with optional ``capability_id`` disambiguation when the closed set
  carries multiple declarations of the same kind).
"""

from __future__ import annotations

from collections.abc import Iterable
from urllib.parse import urlsplit, urlunsplit

from adcp.types import CanonicalFormatKind, Error, FormatId, ProductFormatDeclaration

# Default ports per RFC 3986 §3.2.3 — stripped during canonicalization
# so ``https://x.example:443`` matches ``https://x.example``.
_DEFAULT_PORTS: dict[str, int] = {"http": 80, "https": 443}


def _canonicalize_agent_url(raw: str) -> str:
    """Return ``raw`` with scheme + host lowercased and default port stripped.

    Per ``core/format-id.json`` (normative): callers MUST canonicalize
    ``agent_url`` before comparing two ``FormatId`` values for identity.
    Pydantic's ``AnyUrl`` does trailing-slash normalization but not
    RFC 3986 §6 host-casefolding or default-port stripping — a seller
    publishing ``"https://Creative.AdContextProtocol.org"`` would
    silently miss-match a buyer's ``"https://creative.adcontextprotocol.org"``
    without this step.

    Non-throwing: malformed inputs round-trip as-is. The lookup is a
    closed-set match, not a security check; we don't want to reject
    here, just normalize what we can.
    """
    try:
        parts = urlsplit(raw)
    except ValueError:
        return raw
    if not parts.scheme or not parts.hostname:
        return raw
    scheme = parts.scheme.lower()
    host = parts.hostname.lower()
    port = parts.port
    if port is not None and port == _DEFAULT_PORTS.get(scheme):
        port = None
    netloc = host if port is None else f"{host}:{port}"
    return urlunsplit((scheme, netloc, parts.path, parts.query, ""))


class FormatKindNotInClosedSetError(ValueError):
    """Raised when a ``format_kind`` is not in the product's ``format_options[]``.

    Carries the rejected kind plus the closed set on the exception
    instance so handlers can surface them on the wire response (e.g.,
    via ``error.details.accepted_values``). Use :meth:`to_wire_error`
    to construct the response ``Error`` directly.
    """

    def __init__(
        self,
        format_kind: str,
        accepted_kinds: list[str],
    ) -> None:
        self.format_kind = format_kind
        self.accepted_kinds = accepted_kinds
        super().__init__(
            f"format_kind={format_kind!r} is not in the product's format_options[] "
            f"closed set (accepted: {sorted(set(accepted_kinds))!r})."
        )

    def to_wire_error(
        self,
        *,
        field: str = "manifest.format_kind",
        message: str | None = None,
    ) -> Error:
        """Build the wire-correct ``UNSUPPORTED_FEATURE`` ``Error`` for the response.

        Per ``error.json``, closed-set rejections SHOULD use
        ``details.rejected_value`` + ``details.accepted_values`` so
        buyer-side diagnostic tooling can surface the accepted set
        without per-seller pattern matching.

        Args:
            field: JSONPath-lite pointer to the rejected field on the
                buyer's request (default ``"manifest.format_kind"`` —
                the typical ``create_media_buy`` location).
            message: Override the default human-readable message.
        """
        return Error(
            code="UNSUPPORTED_FEATURE",
            message=message or str(self),
            field=field,
            details={
                "rejected_value": self.format_kind,
                "accepted_values": sorted(set(self.accepted_kinds)),
            },
        )


def _coerce_kind(value: str | CanonicalFormatKind) -> str:
    """Normalise the input to the wire string the schema uses."""
    if isinstance(value, CanonicalFormatKind):
        return value.value
    return value


def validate_format_kind_in_options(
    format_kind: str | CanonicalFormatKind,
    format_options: Iterable[ProductFormatDeclaration],
) -> None:
    """Raise if ``format_kind`` isn't published in ``format_options[]``.

    Args:
        format_kind: The kind a buyer's manifest targets. Accepts both
            the wire-string form (``"image"``) and the typed enum form
            (``CanonicalFormatKind.image``).
        format_options: The product's closed set of accepted format
            declarations.

    Raises:
        FormatKindNotInClosedSetError: when no declaration in the closed
            set carries that ``format_kind``. The seller MUST surface
            ``UNSUPPORTED_FEATURE`` on the response.
    """
    wanted = _coerce_kind(format_kind)
    accepted = [_coerce_kind(d.format_kind) for d in format_options]
    if wanted not in accepted:
        raise FormatKindNotInClosedSetError(wanted, accepted)


def find_declaration_by_kind(
    format_kind: str | CanonicalFormatKind,
    format_options: Iterable[ProductFormatDeclaration],
    *,
    capability_id: str | None = None,
) -> ProductFormatDeclaration | None:
    """Look up the declaration in ``format_options[]`` matching the kind.

    Disambiguates with ``capability_id`` when the closed set carries
    multiple declarations sharing the same ``format_kind`` (the case
    where ``capability_id`` is REQUIRED per
    ``ProductFormatDeclaration.capability_id``).

    Args:
        format_kind: The kind to match. Accepts string or enum.
        format_options: The product's ``format_options[]``.
        capability_id: When provided, only declarations whose
            ``capability_id`` equals this value are considered a match.
            When omitted, the first kind match wins; this is unambiguous
            only when every declaration of that kind shares the same
            ``capability_id``.

    Returns:
        The matching declaration, or ``None`` when no declaration in the
        closed set satisfies the query.
    """
    wanted = _coerce_kind(format_kind)
    for d in format_options:
        if _coerce_kind(d.format_kind) != wanted:
            continue
        if capability_id is not None and d.capability_id != capability_id:
            continue
        return d
    return None


def find_declaration_by_v1_format_id(
    format_id: FormatId,
    format_options: Iterable[ProductFormatDeclaration],
) -> ProductFormatDeclaration | None:
    """Look up the declaration whose ``v1_format_ref[]`` includes ``format_id``.

    Seller-side helper for processing v1 ``create_media_buy`` requests
    against a product publishing v2 ``format_options[]``. A buyer
    targeting a v1 ``format_id`` lands here: the SDK walks the closed
    set looking for the declaration that asserted this v1 ref.

    Matches on both ``agent_url`` and ``id`` — a v1 format identity is
    the ``(agent_url, id)`` pair, not the id alone. Returns the first
    declaration whose ``v1_format_ref[]`` contains a structurally equal
    entry.

    Args:
        format_id: The v1 ``FormatId`` the buyer's manifest targets.
        format_options: The product's ``format_options[]`` closed set.

    Returns:
        The matching declaration, or ``None`` when no declaration in the
        closed set asserts this v1 ref. ``None`` means the request
        should be rejected with ``UNSUPPORTED_FEATURE`` — the v1
        ``format_id`` is not a recognised entry for this product.
    """
    target_url = _canonicalize_agent_url(str(format_id.agent_url))
    target_id = format_id.id
    for decl in format_options:
        refs = decl.v1_format_ref or []
        for ref in refs:
            ref_url = _canonicalize_agent_url(str(ref.agent_url))
            if ref_url == target_url and ref.id == target_id:
                return decl
    return None


__all__ = [
    "FormatKindNotInClosedSetError",
    "find_declaration_by_kind",
    "find_declaration_by_v1_format_id",
    "validate_format_kind_in_options",
]
