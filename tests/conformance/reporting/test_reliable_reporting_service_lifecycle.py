"""Actual service admission/settlement with real PostgreSQL transactions."""

from __future__ import annotations

import asyncio
import threading
from dataclasses import replace
from datetime import timedelta
from typing import Any

import pytest

from adcp.reporting.fixtures import redacted_capabilities
from adcp.reporting.ledger.pg import PgReportingLedgerStore
from adcp.reporting.service import (
    ReliableReportingService,
    ReliableReportingShutdownTimeoutError,
    ReliableReportingState,
    ReliableReportingUnavailableError,
    ReportingServiceResource,
)
from adcp.reporting.testing import ScriptedReportingAdapter
from tests.test_reliable_reporting_lifecycle import checkpoint
from tests.test_reliable_reporting_service import _account_context, _configuration, _rows

from ._generation_support import isolated_reporting_pool


@pytest.mark.parametrize("autocommit", [False, True])
async def test_configuration_database_error_isolated_and_retried(autocommit: bool) -> None:
    async with isolated_reporting_pool(autocommit=autocommit) as pool:
        from psycopg import OperationalError

        async with pool.connection() as connection:
            row = await (await connection.execute("SELECT clock_timestamp()")).fetchone()
        now = row[0]
        boundary = now.replace(minute=0, second=0, microsecond=0)
        errors: list[tuple[str, BaseException]] = []

        class FlakyLedger(PgReportingLedgerStore):
            failure: OperationalError | None = None

            async def find_obligation(self, **kwargs: Any) -> Any:
                if kwargs["account_id"] == "account-a" and self.failure is None:
                    # Raise a real driver OperationalError inside a transaction;
                    # its rollback must not prevent the next account from running.
                    try:
                        async with pool.connection() as connection:
                            await connection.execute(
                                "DO $$ BEGIN RAISE EXCEPTION 'temporary ledger failure' "
                                "USING ERRCODE = '08006'; END $$"
                            )
                    except OperationalError as error:
                        self.failure = error
                        raise
                return await super().find_obligation(**kwargs)

        ledger = FlakyLedger(pool=pool)
        service = ReliableReportingService(
            store=ledger,
            account_context=_account_context,
            clock=lambda: now,
            worker_error_handler=lambda component, error: errors.append((component, error)),
        )
        adapter = ScriptedReportingAdapter(redacted_capabilities(), [_rows(11), _rows(12)])
        service.sources.register("gam", adapter)
        configurations = [
            replace(
                _configuration(account_id=account),
                activated_at=boundary - timedelta(hours=3) + timedelta(minutes=20),
                deactivated_at=boundary - timedelta(hours=1),
            )
            for account in ("account-a", "account-b")
        ]
        for configuration in configurations:
            await service.configure(configuration)
        failed_key, healthy_key = (item.generation_key for item in configurations)
        try:
            first = await service.run_worker()
            assert isinstance(ledger.failure, OperationalError)
            assert first.configuration_errors == {failed_key: ledger.failure}
            assert set(first.configurations) == {healthy_key}
            assert len(first.configurations[healthy_key].revisions_committed) == 1
            assert errors == [("configuration:account-a:gam-delivery@1", ledger.failure)]
            assert service.ready
            assert service.failure is None

            recovered = await service.run_worker()
            assert not recovered.configuration_errors
            assert len(recovered.configurations[failed_key].revisions_committed) == 1
            assert len(adapter.calls) == 2
            assert service.ready
        finally:
            await service.close()
        await service.wait()


@pytest.mark.parametrize("autocommit", [False, True])
@pytest.mark.parametrize("cancel_call", [False, True])
async def test_stop_settles_public_configuration_transaction_before_owned_cleanup(
    autocommit: bool, cancel_call: bool
) -> None:
    async with isolated_reporting_pool(autocommit=autocommit) as pool:
        inserted = asyncio.Event()
        release = asyncio.Event()
        cleanup_started = asyncio.Event()
        cleanup_release = asyncio.Event()
        configurations_at_close: list[int] = []

        class PausedLedger(PgReportingLedgerStore):
            async def put_configuration(self, configuration: Any) -> None:
                # Pause after the real ledger mutation but before its real
                # transaction commits. No private prepared producer/store.
                async with self.transaction():
                    await super().put_configuration(configuration)
                    inserted.set()
                    try:
                        await release.wait()
                    finally:
                        if cancel_call:
                            cleanup_started.set()
                            await cleanup_release.wait()

        configuration = _configuration()
        observer = PgReportingLedgerStore(pool=pool)

        async def close_owned() -> None:
            retained = await observer.list_configurations(account_id=configuration.account_id)
            configurations_at_close.append(len(retained))

        service = ReliableReportingService(
            store=PausedLedger(pool=pool),
            account_context=_account_context,
            owned_resources=(ReportingServiceResource(close=close_owned),),
        )
        service.sources.register("gam", ScriptedReportingAdapter(redacted_capabilities(), []))
        await service.start()
        configuring = asyncio.create_task(service.configure(configuration))
        await asyncio.wait_for(inserted.wait(), 5)
        try:
            assert await observer.list_configurations(account_id=configuration.account_id) == ()
            with pytest.raises(ReliableReportingShutdownTimeoutError):
                await service.close(timeout=0)
            assert service.state is ReliableReportingState.STOPPING
            assert configurations_at_close == []
            with pytest.raises(ReliableReportingUnavailableError):
                await service.configure(replace(configuration, delivery_config_version=2))
            if cancel_call:
                configuring.cancel()
                await asyncio.wait_for(cleanup_started.wait(), 5)
                configuring.cancel()
                await checkpoint()
                await checkpoint()
                assert not configuring.done()
                assert configurations_at_close == []
                cleanup_release.set()
                with pytest.raises(asyncio.CancelledError):
                    await configuring
            else:
                release.set()
                await configuring
        finally:
            release.set()
            cleanup_release.set()
            await asyncio.gather(configuring, return_exceptions=True)
            await asyncio.wait_for(service.close(), 5)
        assert configurations_at_close == [0 if cancel_call else 1]
        assert pool.closed is False  # an injected PostgreSQL pool is borrowed
        restarted = ReliableReportingService.postgres(pool=pool, account_context=_account_context)
        await restarted.start()
        try:
            retained = await restarted.store.list_configurations(
                account_id=configuration.account_id
            )
            assert retained == (() if cancel_call else (configuration,))
        finally:
            await restarted.close()


async def test_cancelled_public_producer_settles_thread_and_recovers_same_durable_obligation() -> (
    None
):
    async with isolated_reporting_pool() as pool:
        async with pool.connection() as connection:
            row = await (await connection.execute("SELECT clock_timestamp()")).fetchone()
        now = row[0]
        boundary = now.replace(minute=0, second=0, microsecond=0)
        configuration = replace(
            _configuration(),
            activated_at=boundary - timedelta(hours=3) + timedelta(minutes=20),
            deactivated_at=boundary - timedelta(hours=1),
        )
        entered = asyncio.Event()
        release = threading.Event()
        finished = threading.Event()
        closes: list[str] = []
        loop = asyncio.get_running_loop()

        class Adapter:
            capabilities = redacted_capabilities()

            def fetch_slice(self, _request: Any) -> list[dict[str, Any]]:
                loop.call_soon_threadsafe(entered.set)
                try:
                    assert release.wait(10), "test cleanup watchdog"
                    return _rows(11)
                finally:
                    finished.set()

            async def close(self) -> None:
                assert finished.is_set()
                closes.append("adapter")

        adapter = Adapter()
        service = ReliableReportingService(
            store=PgReportingLedgerStore(pool=pool),
            account_context=_account_context,
            # The semantic producer boundary is sampled from the database;
            # PostgreSQL evidence itself uses the store's unmodified DB clock.
            clock=lambda: now,
            owned_resources=(ReportingServiceResource(close=adapter.close),),
        )
        service.sources.register("gam", adapter)
        await service.configure(configuration)
        turn = asyncio.create_task(service.run_worker(now=now))
        await asyncio.wait_for(entered.wait(), 5)
        try:
            async with pool.connection() as connection:
                rows = await (
                    await connection.execute(
                        "SELECT reporting_obligation_id FROM reporting_obligations"
                    )
                ).fetchall()
            assert len(rows) == 1
            obligation_id = rows[0][0]
            turn.cancel()
            with pytest.raises(ReliableReportingShutdownTimeoutError):
                await service.close(timeout=0)
            assert service.state is ReliableReportingState.STOPPING
            assert closes == []
        finally:
            release.set()
            await asyncio.gather(turn, return_exceptions=True)
            await asyncio.wait_for(service.close(), 5)
        assert closes == ["adapter"]
        assert pool.closed is False
        assert (
            await service.store.list_revisions(
                account_id=configuration.account_id, reporting_obligation_id=obligation_id
            )
            == ()
        )

        # A fresh service replays the accepted generation through the public
        # route and producer. This is restart recovery, not late discovery (the
        # durable binding/discovery bridge remains a subsequent B1 slice).
        restarted = ReliableReportingService(
            store=PgReportingLedgerStore(pool=pool),
            account_context=_account_context,
            clock=lambda: now,
        )
        restarted.sources.register(
            "gam", ScriptedReportingAdapter(redacted_capabilities(), [_rows(11)])
        )
        await restarted.configure(configuration)
        try:
            recovered = await restarted.run_worker(now=now)
            assert (
                len(recovered.configurations[configuration.generation_key].revisions_committed) == 1
            )
            revisions = await restarted.store.list_revisions(
                account_id=configuration.account_id, reporting_obligation_id=obligation_id
            )
            assert len(revisions) == 1
            async with pool.connection() as connection:
                rows = await (
                    await connection.execute(
                        "SELECT reporting_obligation_id FROM reporting_obligations"
                    )
                ).fetchall()
            assert rows == [(obligation_id,)]
        finally:
            await restarted.close()
