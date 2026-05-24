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

from adcp.types import CanonicalFormatKind, ProductFormatDeclaration


class FormatKindNotInClosedSetError(ValueError):
    """Raised when a ``format_kind`` is not in the product's ``format_options[]``.

    Carries the rejected kind plus the closed set on the exception
    instance so handlers can surface them on the wire response (e.g.,
    via ``error.details.accepted_values``).
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


__all__ = [
    "FormatKindNotInClosedSetError",
    "find_declaration_by_kind",
    "validate_format_kind_in_options",
]
