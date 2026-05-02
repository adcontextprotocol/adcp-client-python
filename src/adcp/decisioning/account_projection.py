"""Response-side projection helpers for v3 :class:`Account` payloads.

The AdCP v3 spec marks :attr:`adcp.types.BusinessEntity.bank` as
write-only — adopters accept it on inbound ``sync_accounts`` requests
but MUST omit it from any response payload that surfaces an
``Account``. The schema *describes* this rule in a docstring, but
doesn't structurally enforce it; Pydantic round-trips ``bank`` on
``model_dump()`` like any other field.

This module ships the structural guard. Adopters call
:func:`project_account_for_response` (or
:func:`project_business_entity_for_response`) on the way out and the
helper returns a fresh model with ``bank`` cleared.

Why a separate function instead of a Pydantic ``field_serializer``?
The framework's typed :class:`Account` model is auto-generated from
the spec schema — patching it in-place would drift on every regen.
Keeping projection in adopter-callable helpers means the wire shape
stays exactly what the spec defines while adopters get a one-line
guard against the leak.

Quickstart::

    from adcp.types import Account
    from adcp.decisioning import project_account_for_response

    # Adopter persists `account` with billing_entity.bank populated
    # for invoicing. On the response path:
    response_payload = project_account_for_response(account).model_dump(
        mode="json", exclude_none=True,
    )
    # `response_payload['billing_entity']` no longer carries 'bank'.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from adcp.types import Account, BusinessEntity


def project_account_for_response(account: Account) -> Account:
    """Return a copy of ``account`` safe to serialize on a response.

    Strips :attr:`Account.billing_entity.bank` — the AdCP v3 spec
    marks bank details as write-only. Adopters that persist the full
    :class:`BusinessEntity` (with bank populated for invoicing) MUST
    project through this helper before serializing on any response.

    Returns the input unchanged when ``billing_entity`` is ``None``
    or ``billing_entity.bank`` is already absent — defensive copy
    via ``model_copy()`` so callers can mutate the returned object
    freely without touching the caller's input.

    The original ``account`` object is not modified.
    """
    if account.billing_entity is None or account.billing_entity.bank is None:
        return account.model_copy()
    safe_billing_entity = account.billing_entity.model_copy(update={"bank": None})
    return account.model_copy(update={"billing_entity": safe_billing_entity})


def project_business_entity_for_response(entity: BusinessEntity) -> BusinessEntity:
    """Return a copy of ``entity`` with ``bank`` cleared.

    Same posture as :func:`project_account_for_response` but
    operating on a :class:`BusinessEntity` directly — useful for
    adopters serializing standalone billing-entity payloads (admin
    APIs, brand-rights flows) that don't go through the
    :class:`Account` envelope.

    The original ``entity`` is not modified.
    """
    if entity.bank is None:
        return entity.model_copy()
    return entity.model_copy(update={"bank": None})


__all__ = [
    "project_account_for_response",
    "project_business_entity_for_response",
]
