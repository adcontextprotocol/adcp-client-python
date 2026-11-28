"""Account offering pins survive configuration persistence and recovery."""

import asyncio
from dataclasses import replace
from datetime import datetime, timezone

import pytest
from adcp_reporting_storage.ledger.pg import PgReportingLedgerStore

from adcp.reporting.ledger import ReportingStatusCaller
from adcp.reporting.ledger.store import LedgerConflictError
from adcp.reporting.service import ReliableReportingService
from adcp.reporting.testing import ScriptedReportingAdapter
from tests.test_reliable_reporting_service import _account_binding, _rows

from ._generation_support import configuration, isolated_reporting_pool


def _unexpected_deactivation(*args):
    raise AssertionError("unexpected deactivation")


class _CatalogService(ReliableReportingService):
    def __init__(self, **kwargs):
        kwargs.setdefault("supports_frozen_offering", lambda *_: True)
        kwargs.setdefault("deactivate_unserved_generation", _unexpected_deactivation)
        super().__init__(**kwargs)


async def test_postgres_retains_account_binding_and_rejects_a_changed_generation() -> None:
    accepted = replace(configuration(), account_binding_sha256="c" * 64)
    async with isolated_reporting_pool(autocommit=True) as pool:
        store = PgReportingLedgerStore(pool=pool)
        await store.create_schema()
        await store.put_configuration(accepted)

        assert await store.list_all_configurations() == (accepted,)
        with pytest.raises(LedgerConflictError, match="different content"):
            await store.put_configuration(replace(accepted, account_binding_sha256="d" * 64))


async def test_postgres_recovers_pinned_agent_offering_after_catalog_changes() -> None:
    generation, context, capabilities, offering = _account_binding("a")
    context = replace(context, serving_agent_id="agent-a")
    generation = replace(generation, account_binding_sha256=context.binding_sha256())
    offerings = [offering]
    async with isolated_reporting_pool(autocommit=True) as pool:
        store = PgReportingLedgerStore(pool=pool)
        await store.create_schema()

        def service_for(bound):
            service = _CatalogService(
                store=store,
                account_context=lambda _: bound,
                agent_offerings=lambda _: offerings,
                supports_frozen_offering=lambda *_: True,
            )
            service.sources.register("gam", ScriptedReportingAdapter(capabilities, []))
            return service

        original = service_for(context)
        await original.configure(generation, serving_agent_id="agent-a")
        await original.initialize()
        offerings.clear()
        recovered = service_for(context)
        await recovered.initialize()
        assert generation.generation_key in recovered._bindings
        assert await store.list_all_configurations() == (generation,)
        drifted = replace(
            context, capability_offering={**offering, "standard_dimensions": ["campaign_id"]}
        )
        refused = service_for(drifted)
        await refused.initialize()
        assert "frozen configuration" in str(
            refused.initialization_errors[generation.generation_key]
        )
        assert generation.generation_key not in refused._bindings
        assert await store.list_all_configurations() == (generation,)


async def test_postgres_fences_deactivation_against_a_stale_worker() -> None:
    started = asyncio.Event()
    resume = asyncio.Event()

    class PausingStore(PgReportingLedgerStore):
        async def commit_agent_obligation(self, configuration, obligation):
            started.set()
            await resume.wait()
            return await super().commit_agent_obligation(configuration, obligation)

    generation, context, capabilities, offering = _account_binding("a")
    context = replace(context, serving_agent_id="agent-a")
    generation = replace(generation, account_binding_sha256=context.binding_sha256())
    cutoff = datetime(2026, 11, 1, 3, tzinfo=timezone.utc)
    now = datetime(2026, 11, 1, 5, tzinfo=timezone.utc)
    async with isolated_reporting_pool(autocommit=True) as pool:
        store = PausingStore(pool=pool)
        await store.create_schema()
        service = _CatalogService(
            store=store,
            account_context=lambda _: context,
            agent_offerings=lambda _: [offering],
            supports_frozen_offering=lambda *_: True,
        )
        service.sources.register("gam", ScriptedReportingAdapter(capabilities, [_rows(10)]))
        await service.configure(generation, serving_agent_id="agent-a")
        await service.initialize()
        turn = asyncio.create_task(service.run_worker(now=now))
        await asyncio.wait_for(started.wait(), timeout=2)
        await store.put_configuration(replace(generation, deactivated_at=cutoff))
        resume.set()
        await asyncio.wait_for(turn, timeout=2)
        await service.run_worker(now=now)
        snapshot = await store.read_status_snapshot(
            caller=ReportingStatusCaller(generation.account_id, generation.consumer_id)
        )
        assert snapshot.obligations
        assert all(obligation.period.end <= cutoff for obligation in snapshot.obligations)
