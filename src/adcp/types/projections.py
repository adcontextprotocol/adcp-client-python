"""Response-shape projections that strip write-only fields.

The AdCP spec marks certain fields as ``writeOnly: true`` — present in
requests so adopters can populate them, but MUST NOT be echoed in
responses. The clearest case is ``BusinessEntity.bank``: IBANs, BICs,
routing numbers, and account numbers flow into the seller during account
setup and stay there. Pydantic's default serialization round-trips
everything, so an adopter who reuses an internal ``Account`` model on
the response path can leak bank details without realizing it.

The projections here type-narrow the write-only fields to ``None``:
construction with a non-None value raises ``ValidationError``, and the
serialization path excludes the fields regardless. Adopters opt in by
constructing the ``*Response`` variant on the response edge, or by
piping through ``to_account_response()``.

Out of scope:

* ``GovernanceAgent.authentication`` (also write-only): generated nested
  type, separate adopter contract; track separately.
* ``reporting_bucket``: NOT write-only — seller-provisioned, buyer-readable
  for offline delivery coordinates. Preserved as-is by the projection.
"""

from __future__ import annotations

from typing import Any

from pydantic import Field, field_validator

from adcp.types import Account, BusinessEntity


class BusinessEntityResponse(BusinessEntity):
    """Response projection of :class:`BusinessEntity` with bank details stripped.

    Per AdCP 3.0.x ``core/business-entity.json``: ``bank.*`` fields carry
    ``writeOnly: true`` and MUST NOT appear in responses. Sellers store
    bank coordinates and confirm receipt without echoing them.

    This subclass enforces the contract two ways:

    * Construction: passing ``bank=...`` raises ``ValidationError``.
    * Serialization: the field is excluded from ``model_dump()`` output
      even if some path mutated it post-construction (defense in depth
      against ``model_copy()``, idempotency replay caches, etc.).
    """

    bank: Any = Field(default=None, exclude=True)

    @field_validator("bank", mode="before")
    @classmethod
    def _reject_bank(cls, v: Any) -> None:
        if v is not None:
            raise ValueError(
                "BusinessEntityResponse must not carry bank details — bank is "
                "write-only per AdCP spec. Drop the field before constructing "
                "a response, or use to_account_response() to strip it."
            )
        return None


class AccountResponse(Account):
    """Response projection of :class:`Account` — billing_entity is the
    bank-stripped variant.

    Use this on the response edge of any handler that returns account
    state (``list_accounts``, ``get_account_financials``, etc.) when
    your internal ``Account`` records carry bank details. For convenience,
    :func:`to_account_response` projects an existing ``Account`` instance
    to an ``AccountResponse`` and drops bank along the way.
    """

    billing_entity: BusinessEntityResponse | None = None


def to_account_response(account: Account) -> AccountResponse:
    """Project an internal ``Account`` to its response shape.

    Strips ``billing_entity.bank`` and returns an :class:`AccountResponse`.
    The remaining fields (legal_name, tax_id, address, contacts, vat_id,
    registration_number, ext) round-trip unchanged. ``reporting_bucket``,
    ``governance_agents``, and other non-write-only fields are preserved.

    Raises:
        ValidationError: if the source ``Account`` fails revalidation
            against the response shape (other than the bank strip).
    """
    payload = account.model_dump(mode="python")
    if isinstance(payload.get("billing_entity"), dict):
        payload["billing_entity"].pop("bank", None)
    return AccountResponse.model_validate(payload)


__all__ = [
    "AccountResponse",
    "BusinessEntityResponse",
    "to_account_response",
]
