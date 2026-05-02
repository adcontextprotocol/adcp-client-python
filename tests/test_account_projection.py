"""Tests for :mod:`adcp.decisioning.account_projection`.

The AdCP v3 spec marks :attr:`adcp.types.BusinessEntity.bank` as
write-only. The schema doesn't structurally enforce this — the
projection helpers here do, by returning a copy of the model with
``bank`` cleared. Tests verify:

* ``bank`` is absent from the projected output
* The original input is not mutated
* JSON serialization of the projection genuinely lacks ``bank``
* Defensive copy when there's nothing to strip
"""

from __future__ import annotations

import pytest

from adcp.decisioning import (
    project_account_for_response,
    project_business_entity_for_response,
)
from adcp.types import (
    Account,
    BusinessEntity,
)
from adcp.types.generated_poc.core.business_entity import Address, Bank


def _bank_fixture() -> Bank:
    return Bank(
        account_holder="Acme Inc.",
        iban="DE89370400440532013000",
        bic="DEUTDEFF",
    )


def _entity_fixture(*, with_bank: bool = True) -> BusinessEntity:
    return BusinessEntity(
        legal_name="Acme Inc.",
        vat_id="DE123456789",
        address=Address(
            street="123 Main",
            city="Berlin",
            postal_code="10115",
            country="DE",
        ),
        bank=_bank_fixture() if with_bank else None,
    )


def _account_fixture(*, with_bank: bool = True) -> Account:
    return Account(
        account_id="acct_1",
        name="Acme",
        status="active",
        billing_entity=_entity_fixture(with_bank=with_bank),
    )


# ----- project_business_entity_for_response -----


def test_project_business_entity_strips_bank() -> None:
    entity = _entity_fixture()
    assert entity.bank is not None  # sanity

    projected = project_business_entity_for_response(entity)

    assert projected.bank is None
    # Other fields preserved.
    assert projected.legal_name == "Acme Inc."
    assert projected.vat_id == "DE123456789"
    assert projected.address is not None
    assert projected.address.country == "DE"


def test_project_business_entity_does_not_mutate_input() -> None:
    """The helper returns a fresh model — the original keeps bank
    populated for adopter persistence."""
    entity = _entity_fixture()
    project_business_entity_for_response(entity)
    assert entity.bank is not None
    assert entity.bank.iban == "DE89370400440532013000"


def test_project_business_entity_returns_copy_when_no_bank() -> None:
    """No bank to strip — defensive copy still returned so callers
    can mutate freely."""
    entity = _entity_fixture(with_bank=False)
    projected = project_business_entity_for_response(entity)
    assert projected is not entity
    assert projected.bank is None


def test_projected_business_entity_serializes_without_bank() -> None:
    """The actual JSON output — what lands on the wire — must not
    contain ``bank`` at all (not just ``bank: null``)."""
    entity = _entity_fixture()
    projected = project_business_entity_for_response(entity)
    payload = projected.model_dump(mode="json", exclude_none=True)
    assert "bank" not in payload


# ----- project_account_for_response -----


def test_project_account_strips_billing_entity_bank() -> None:
    account = _account_fixture()
    assert account.billing_entity is not None
    assert account.billing_entity.bank is not None

    projected = project_account_for_response(account)

    assert projected.billing_entity is not None
    assert projected.billing_entity.bank is None


def test_project_account_does_not_mutate_input() -> None:
    account = _account_fixture()
    project_account_for_response(account)
    assert account.billing_entity is not None
    assert account.billing_entity.bank is not None


def test_project_account_when_billing_entity_is_none() -> None:
    """No billing_entity to project — return a defensive copy."""
    account = Account(account_id="acct_x", name="x", status="active")
    projected = project_account_for_response(account)
    assert projected is not account
    assert projected.billing_entity is None


def test_project_account_when_billing_entity_has_no_bank() -> None:
    """billing_entity present but already bank-less — defensive copy."""
    account = _account_fixture(with_bank=False)
    projected = project_account_for_response(account)
    assert projected is not account
    assert projected.billing_entity is not None
    assert projected.billing_entity.bank is None


def test_projected_account_serializes_without_bank() -> None:
    """End-to-end: an adopter dumping the projected account to wire
    JSON does not leak bank details."""
    account = _account_fixture()
    projected = project_account_for_response(account)
    payload = projected.model_dump(mode="json", exclude_none=True)
    assert "billing_entity" in payload
    assert "bank" not in payload["billing_entity"]
    # Other fields preserved.
    assert payload["billing_entity"]["legal_name"] == "Acme Inc."


def test_projected_account_other_fields_preserved() -> None:
    """The projection only touches ``billing_entity.bank`` — every
    other field round-trips."""
    account = _account_fixture()
    projected = project_account_for_response(account)
    assert projected.account_id == "acct_1"
    assert projected.name == "Acme"
    assert projected.status.value == "active"


def test_double_projection_is_idempotent() -> None:
    """Projecting an already-projected account is a no-op + copy.
    Useful when adopters compose projections through middleware
    pipelines."""
    account = _account_fixture()
    once = project_account_for_response(account)
    twice = project_account_for_response(once)
    assert twice.billing_entity is not None
    assert twice.billing_entity.bank is None
    # Different objects (defensive copy) but equal payloads.
    assert twice is not once
    assert twice.model_dump() == once.model_dump()


# ----- spec-claim regression: schema docstring vs structural guard -----


def test_unprojected_account_leaks_bank_via_model_dump() -> None:
    """Demonstrates *why* the helper exists. Without the projection,
    a naive ``model_dump()`` puts ``bank`` on the wire — the spec
    forbids this on responses but Pydantic doesn't enforce it
    structurally. Adopters who skip the helper ship the leak.

    This test is the regression guard: if a future schema regen ever
    moves ``bank`` to a write-only Pydantic field
    (``Field(exclude=True)`` or similar), this test will start
    failing — which means we can simplify the helper to a no-op.
    Failing this test is a signal to revisit, not a defect."""
    account = _account_fixture()
    payload = account.model_dump(mode="json", exclude_none=True)
    assert "bank" in payload["billing_entity"]
    pytest.skip("regression guard — see docstring")
