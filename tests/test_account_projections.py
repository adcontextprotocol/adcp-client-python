"""Account-response projection helpers — write-only bank-details guard.

Per AdCP 3.0.x spec, ``BusinessEntity.bank`` is marked ``writeOnly: true``:
buyers send bank coordinates as part of account setup, sellers store them,
but sellers MUST NOT echo them back in responses. Pydantic's default
serialization round-trips everything, so an adopter who reuses an internal
``Account`` model on the response path can leak IBAN/routing numbers
without realizing it.

The projection types in ``adcp.types.projections`` make the guard explicit:
type-narrow ``bank`` to ``None`` so the field is unconstructable on the
response variant, and serialize without it regardless.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from adcp.types import Account, BusinessEntity
from adcp.types.generated_poc.core.business_entity import Address, Bank
from adcp.types.projections import (
    AccountResponse,
    BusinessEntityResponse,
    to_account_response,
)

# ---- BusinessEntityResponse — write-only bank guard ----


def test_business_entity_response_rejects_bank_at_construction() -> None:
    """Passing ``bank=...`` to ``BusinessEntityResponse`` raises
    ValidationError. The whole point: an adopter cannot assemble a
    response-shape that would echo bank details, full stop.
    """
    with pytest.raises(ValidationError) as excinfo:
        BusinessEntityResponse(
            legal_name="Acme Corp",
            bank=Bank(account_holder="Acme Corp", iban="DE89370400440532013000"),
        )
    # Error must localize to the bank field.
    assert any("bank" in str(loc) for err in excinfo.value.errors() for loc in err["loc"])


def test_business_entity_response_accepts_other_fields_unchanged() -> None:
    """Non-write-only fields (legal_name, vat_id, address, contacts) flow
    through unchanged — only ``bank`` is restricted."""
    e = BusinessEntityResponse(
        legal_name="Pinnacle Media GmbH",
        vat_id="DE123456789",
        address=Address(
            street="Friedrichstrasse 100",
            city="Berlin",
            postal_code="10117",
            country="DE",
        ),
    )
    assert e.legal_name == "Pinnacle Media GmbH"
    assert e.vat_id == "DE123456789"
    assert e.address is not None and e.address.country == "DE"
    assert e.bank is None


def test_business_entity_response_serializes_without_bank() -> None:
    """``model_dump()`` MUST NOT include ``bank`` on the wire shape — even
    if some upstream path (model_copy, in-place mutation, idempotency replay)
    managed to set it. Defense in depth on top of the construction guard."""
    e = BusinessEntityResponse(legal_name="Acme Corp")
    # Smuggle bank past the type system to exercise the serialization guard
    # specifically (mirrors what a model_copy() round-trip could do).
    object.__setattr__(e, "bank", Bank(account_holder="Acme Corp", iban="DE89370400440532013000"))
    dumped = e.model_dump()
    assert "bank" not in dumped or dumped["bank"] is None


# ---- AccountResponse — uses BusinessEntityResponse for billing_entity ----


def test_account_response_billing_entity_is_response_projection() -> None:
    """``AccountResponse.billing_entity`` MUST be a ``BusinessEntityResponse``
    (not the unguarded ``BusinessEntity``). Validates the type binding —
    the whole projection collapses if billing_entity reverts to the parent
    type, which would silently re-admit bank.
    """
    fields = AccountResponse.model_fields
    annotation = fields["billing_entity"].annotation
    # annotation is `BusinessEntityResponse | None` — drill in.
    args = getattr(annotation, "__args__", ())
    assert BusinessEntityResponse in args, (
        f"AccountResponse.billing_entity should reference BusinessEntityResponse; "
        f"got annotation={annotation}, args={args}"
    )


def test_account_response_rejects_billing_entity_with_bank() -> None:
    """Constructing ``AccountResponse`` with a billing_entity that carries
    bank details raises at validation time — same guard one level up.
    """
    with pytest.raises(ValidationError):
        AccountResponse.model_validate(
            {
                "account_id": "acct-1",
                "name": "Acme",
                "status": "active",
                "billing_entity": {
                    "legal_name": "Acme Corp",
                    "bank": {
                        "account_holder": "Acme Corp",
                        "routing_number": "021000021",
                        "account_number": "123456789",
                    },
                },
            }
        )


# ---- to_account_response — adopter-facing strip helper ----


def test_to_account_response_strips_bank_from_internal_account() -> None:
    """Adopters with internal ``Account`` records that carry bank details
    use ``to_account_response()`` to project to a safe response shape.
    The bank field is dropped; everything else round-trips.
    """
    internal = Account(
        account_id="acct-1",
        name="Acme",
        status="active",
        billing_entity=BusinessEntity(
            legal_name="Acme Corp",
            tax_id="12-3456789",
            bank=Bank(
                account_holder="Acme Corp",
                routing_number="021000021",
                account_number="123456789",
            ),
        ),
    )
    response = to_account_response(internal)
    assert isinstance(response, AccountResponse)
    assert response.billing_entity is not None
    assert response.billing_entity.legal_name == "Acme Corp"
    assert response.billing_entity.tax_id == "12-3456789"
    assert response.billing_entity.bank is None
    # Wire form has no bank key at all.
    assert "bank" not in (response.model_dump().get("billing_entity") or {}) or (
        response.model_dump()["billing_entity"]["bank"] is None
    )


def test_to_account_response_preserves_reporting_bucket() -> None:
    """``reporting_bucket`` is seller-provisioned and buyers actively read it
    for offline-delivery coordinates — it is NOT write-only and MUST NOT be
    stripped by the projection. Pinning this so the guard doesn't grow to
    eat fields it shouldn't.
    """
    internal = Account(
        account_id="acct-1",
        name="Acme",
        status="active",
        reporting_bucket={
            "protocol": "s3",
            "bucket": "reports-acme-prod",
            "file_retention_days": 30,
        },
    )
    response = to_account_response(internal)
    assert response.reporting_bucket is not None
    assert response.reporting_bucket.bucket == "reports-acme-prod"
    assert response.reporting_bucket.file_retention_days == 30


# ---- notification_configs — write-only authentication credentials guard ----


def test_account_response_rejects_notification_credentials() -> None:
    """Response-shaped accounts cannot be constructed with webhook secrets."""
    with pytest.raises(ValidationError) as excinfo:
        AccountResponse.model_validate(
            {
                "account_id": "acct-1",
                "name": "Acme",
                "status": "active",
                "notification_configs": [
                    {
                        "subscriber_id": "buyer-primary",
                        "url": "https://buyer.example/webhooks",
                        "event_types": ["creative.status_changed"],
                        "authentication": {
                            "schemes": ["Bearer"],
                            "credentials": "secret-token-that-is-at-least-32-chars",
                        },
                    }
                ],
            }
        )

    assert any(
        "credentials" in str(loc) for error in excinfo.value.errors() for loc in error["loc"]
    )


def test_to_account_response_strips_notification_credentials() -> None:
    """Projection keeps subscription state and scheme but drops its secret."""
    internal = Account.model_validate(
        {
            "account_id": "acct-1",
            "name": "Acme",
            "status": "active",
            "notification_configs": [
                {
                    "subscriber_id": "buyer-primary",
                    "url": "https://buyer.example/webhooks",
                    "event_types": ["creative.status_changed"],
                    "authentication": {
                        "schemes": ["Bearer"],
                        "credentials": "secret-token-that-is-at-least-32-chars",
                    },
                    "active": True,
                }
            ],
        }
    )

    dumped = to_account_response(internal).model_dump(mode="json", exclude_none=True)
    config = dumped["notification_configs"][0]
    assert config["subscriber_id"] == "buyer-primary"
    assert config["active"] is True
    assert config["authentication"]["schemes"] == ["Bearer"]
    assert "credentials" not in config["authentication"]
