"""adcp refuses an adcp-reporting-storage of another storage contract at startup (#1763)."""

from __future__ import annotations

from typing import Any

import pytest

from adcp.reporting import ReliableReportingConfigurationError
from adcp.reporting.ledger import InMemoryReportingLedgerStore
from adcp.reporting.ledger import storage_contract as contract
from adcp.reporting.ledger.storage_contract import (
    REPORTING_STORAGE_CONTRACT,
    declared_storage_contract,
    require_storage_contract,
    storage_contract_fingerprint,
)

#: The shared column lists, pinned per contract. Changing a list in
#: ``_record_rows`` changes the fingerprint: bump REPORTING_STORAGE_CONTRACT,
#: ship the matching adcp-reporting-storage DDL and migration, and re-pin.
FINGERPRINTS = {2: "e6cb6454085a8fdc004f564f3882bcfe1a67fd9e622d72a36ba1f9d6ccd12af1"}


def test_the_column_lists_are_pinned_to_the_contract_number() -> None:
    assert REPORTING_STORAGE_CONTRACT in FINGERPRINTS, "pin the new contract's fingerprint"
    assert storage_contract_fingerprint() == FINGERPRINTS[REPORTING_STORAGE_CONTRACT], (
        "the shared ledger column lists changed: bump REPORTING_STORAGE_CONTRACT together "
        "with adcp-reporting-storage's STORAGE_CONTRACT, DDL and migration, then re-pin"
    )


class _Declared:
    storage_contract = REPORTING_STORAGE_CONTRACT


class _Older:
    storage_contract = REPORTING_STORAGE_CONTRACT - 1


class _Newer:
    storage_contract = REPORTING_STORAGE_CONTRACT + 1


class _Undeclared:
    """A store from adcp-reporting-storage 0.1.0b4/0.2.0b0: no declaration at all."""


_Undeclared.__module__ = "adcp_reporting_storage.ledger.pg"


class _Custom:
    """An adopter's own store, outside the check."""


@pytest.mark.parametrize(
    ("store", "expected"),
    [(_Declared(), 2), (_Older(), 1), (_Newer(), 3), (_Undeclared(), 1), (_Custom(), None)],
    ids=["declared", "older", "newer", "undeclared-storage-release", "custom"],
)
def test_a_store_is_classified_by_declaration_or_provenance(
    store: Any, expected: int | None
) -> None:
    assert declared_storage_contract(store) == expected


def test_the_in_memory_store_and_custom_stores_pass() -> None:
    require_storage_contract(InMemoryReportingLedgerStore())
    require_storage_contract(_Custom())
    require_storage_contract(_Declared())


def test_a_storage_release_of_an_older_contract_is_refused_with_the_fix() -> None:
    with pytest.raises(ReliableReportingConfigurationError) as refused:
        require_storage_contract(_Undeclared())
    error = refused.value
    assert error.code == "REPORTING_SCHEMA_BEHIND" and error.recovery == "correctable"
    assert error.config_field == "adcp-reporting-storage"
    message = str(error)
    assert f"storage contract {REPORTING_STORAGE_CONTRACT}" in message
    assert "implements contract 1" in message
    assert "Install adcp-reporting-storage>=0.2.0b1" in message
    assert 'run "python -m adcp migrate"' in message and "create_schema()" in message


def test_a_storage_release_of_a_newer_contract_is_refused_too() -> None:
    with pytest.raises(ReliableReportingConfigurationError, match="implements contract 3"):
        require_storage_contract(_Newer())


async def test_service_startup_refuses_the_pair_before_touching_the_schema(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from adcp.reporting.service import ReliableReportingService
    from tests.test_reliable_reporting_service import NOW, _account_context

    store = InMemoryReportingLedgerStore(clock=lambda: NOW)
    monkeypatch.setattr(InMemoryReportingLedgerStore, "storage_contract", 1, raising=False)
    touched = []

    async def create_schema() -> None:
        touched.append(True)

    monkeypatch.setattr(store, "create_schema", create_schema)
    service = ReliableReportingService(
        store=store, clock=lambda: NOW, account_context=_account_context
    )
    with pytest.raises(ReliableReportingConfigurationError) as refused:
        await service.initialize()
    assert refused.value.code == "REPORTING_SCHEMA_BEHIND"
    assert touched == [], "the schema must not be created or upgraded on a refused pair"


def test_postgres_storage_refuses_the_pair_at_construction(monkeypatch: pytest.MonkeyPatch) -> None:
    import adcp.storage as facade
    from adcp.storage import ReportingStorage

    def build(**kwargs: Any) -> ReportingStorage:
        return ReportingStorage(
            durability="durable", ledger=_Undeclared(), principals=object(), subscriptions=object()
        )

    monkeypatch.setattr(facade, "_plugin", lambda group, name: build)
    with pytest.raises(ReliableReportingConfigurationError, match="implements contract 1"):
        facade.postgres_storage(dsn="postgresql://unused")


def test_the_installed_storage_distribution_declares_this_contract() -> None:
    pytest.importorskip("adcp_reporting_storage")
    from adcp_reporting_storage.contract import STORAGE_CONTRACT, adcp_storage_contract
    from adcp_reporting_storage.ledger.pg import PgReportingLedgerStore

    assert STORAGE_CONTRACT == REPORTING_STORAGE_CONTRACT
    assert adcp_storage_contract() == REPORTING_STORAGE_CONTRACT  # storage reads our declaration
    assert declared_storage_contract.__module__ == contract.__name__
    assert PgReportingLedgerStore.storage_contract == REPORTING_STORAGE_CONTRACT
