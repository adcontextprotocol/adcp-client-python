"""One storage decision: ``memory_storage()`` / ``postgres_storage(...)`` (AdCP 3.3)."""

from __future__ import annotations

from datetime import timedelta

import pytest

from adcp.reporting import SubscriptionOptions
from adcp.reporting.service import ReliableReportingConfigurationError, ReliableReportingService
from adcp.storage import ReportingStorage, memory_storage
from tests.test_reliable_reporting_service import DEFINITION
from tests.test_reporting_subscription_managed import (
    OFFERING,
    START,
    SUBSCRIPTION,
    Seller,
    _ByBuySource,
    _context,
    _ctx,
)


def _service(storage: ReportingStorage, **kwargs) -> ReliableReportingService:
    from adcp.reporting.testing import DeterministicReportingClock

    service = ReliableReportingService(
        storage=storage,
        account_context=_context,
        clock=DeterministicReportingClock(START),
        read_jitter_window=timedelta(0),
        subscriptions=SubscriptionOptions(definitions={OFFERING["offering_id"]: DEFINITION}),
        **kwargs,
    )
    service.sources.register("gam", _ByBuySource(), capability_offerings=[OFFERING])
    return service


def test_memory_storage_is_process_local() -> None:
    storage = memory_storage()
    assert storage.durability == "process-local" and not storage.durable
    assert storage.check_schema is None and storage.close is None


@pytest.mark.asyncio
async def test_one_storage_object_backs_every_store() -> None:
    storage = memory_storage()
    service = _service(storage)
    platform = service.install(Seller())
    assert service.store is storage.ledger
    response = await platform.sync_principal(
        {
            "idempotency_key": "storage-facade-key-0001",
            "configuration": {"reporting_subscriptions": [SUBSCRIPTION]},
        },
        _ctx(),
    )
    assert response.result.kind == "applied"
    # The principal record and the engine state live in the storage's stores.
    assert await storage.principals.get(_ctx().caller_identity) is not None
    assert await storage.subscriptions.list_principals() == [_ctx().caller_identity]
    await service.run_worker()


@pytest.mark.asyncio
async def test_process_local_storage_is_refused_in_production(monkeypatch) -> None:
    monkeypatch.setattr("adcp.storage.development_environment", lambda: False)
    service = _service(memory_storage())
    service.install(Seller())
    with pytest.raises(ReliableReportingConfigurationError) as refused:
        await service.initialize()
    error = refused.value
    assert error.code == "REPORTING_STORAGE_NOT_DURABLE" and error.recovery == "correctable"
    assert "postgres_storage" in str(error) and "ADCP_ENV=development" in str(error)


@pytest.mark.asyncio
async def test_in_memory_stores_without_storage_are_refused_too(monkeypatch) -> None:
    monkeypatch.setattr("adcp.storage.development_environment", lambda: False)
    service = ReliableReportingService.memory(
        account_context=_context,
        subscriptions=SubscriptionOptions(definitions={OFFERING["offering_id"]: DEFINITION}),
    )
    service.sources.register("gam", _ByBuySource(), capability_offerings=[OFFERING])
    service.install(Seller())
    with pytest.raises(ReliableReportingConfigurationError) as refused:
        await service.initialize()
    assert refused.value.code == "REPORTING_STORAGE_NOT_DURABLE"
    assert "the reporting ledger" in str(refused.value)


@pytest.mark.parametrize("env", ["development", "test"])
def test_adcp_env_allows_process_local_storage(monkeypatch, env: str) -> None:
    import sys

    from adcp.storage import development_environment

    monkeypatch.setenv("ADCP_ENV", env)
    assert development_environment()
    monkeypatch.setenv("ADCP_ENV", "production")
    monkeypatch.delitem(sys.modules, "pytest")
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    assert not development_environment()


def test_storage_and_store_are_exclusive() -> None:
    storage = memory_storage()
    with pytest.raises(ReliableReportingConfigurationError, match="not both"):
        ReliableReportingService(storage=storage, store=storage.ledger, account_context=_context)
    with pytest.raises(ReliableReportingConfigurationError, match="storage="):
        ReliableReportingService(account_context=_context)


def test_migrations_are_numbered_files_with_their_upgrade_notes() -> None:
    pytest.importorskip("adcp_reporting_storage")
    from adcp_reporting_storage.migrations import MigrationStatus, describe, migrations

    shipped = migrations()
    assert [item.version for item in shipped] == ["0001", "0002", "0003", "0004", "0005", "0006"]
    widened, offerings = shipped[1], shipped[5]
    assert widened.name == "reporting_subscription_generations" and widened.stopped_worker
    assert widened.includes == ("ledger/reporting_ledger_account_generations.sql",)
    assert offerings.name == "reporting_status_checkpoint_offerings" and offerings.stopped_worker
    assert offerings.includes == ("ledger/reporting_status_checkpoint_offerings.sql",)
    for item in (widened, offerings):
        assert "stop every reporting worker" in item.description
    assert not any(item.stopped_worker for item in shipped if item not in (widened, offerings))
    for item in shipped:
        assert item.statements(), item.version  # every include resolves
    lines = describe([MigrationStatus(item, None) for item in shipped])
    assert lines[1] == (
        "0002 reporting_subscription_generations: pending  [stopped-worker upgrade: stop every "
        "reporting worker before migrating]"
    )


def test_migrate_cli_requires_a_database(capsys) -> None:
    from adcp_reporting_storage.migrations.cli import main

    with pytest.raises(SystemExit):
        main(["--status", "--dsn", ""])
    assert "DATABASE_URL" in capsys.readouterr().err


def test_python_m_adcp_migrate_dispatches_to_the_storage_distribution(monkeypatch, capsys) -> None:
    pytest.importorskip("adcp_reporting_storage")
    import sys

    from adcp.__main__ import main

    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("ADCP_DATABASE_URL", raising=False)
    monkeypatch.setattr(sys, "argv", ["adcp", "migrate", "--status"])
    with pytest.raises(SystemExit):
        main()
    assert "DATABASE_URL" in capsys.readouterr().err


def test_postgres_storage_resolves_through_the_entry_point() -> None:
    pytest.importorskip("adcp_reporting_storage")
    from adcp_reporting_storage.storage import postgres_storage as direct

    from adcp.storage import _plugin

    assert _plugin("adcp.storage", "postgres") is direct
