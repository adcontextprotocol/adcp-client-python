"""D5 live source authorization at dispatch, seal, publication and recovery."""

import asyncio
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import replace
from functools import partial

import pytest

from adcp.reporting.ledger import InMemoryReportingLedgerStore
from adcp.reporting.materializer import reference_verifier
from adcp.reporting.service import ReliableReportingService, ReportingAccountContext

from ._generation_support import END, configuration, isolated_reporting_pool
from ._materializer_support import reference_rows
from ._production_support import production_harness
from .test_reporting_production_lock_order import source_turn
from .test_reporting_production_settling import SettlingSource, revisions


class RevocableSource(SettlingSource):
    def __init__(self, *args, authorized=True, **kwargs):
        super().__init__(*args, **kwargs)
        self.authorized = authorized
        self.dispatched = []
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.release.set()
        self.cancelled = False

    def configuration_binding(self, configuration):
        return super().configuration_binding(configuration) if self.authorized else None

    async def execute(self, request, *, cancel, heartbeat=None):
        self.dispatched.append(request)
        try:
            return await super().execute(request, cancel=cancel, heartbeat=heartbeat)
        finally:
            self.cancelled |= cancel.is_set()

    async def fetch(self, request):
        self.started.set()
        try:
            await asyncio.wait_for(self.release.wait(), 10)
        except asyncio.CancelledError:
            self.cancelled = True
            raise
        return await super().fetch(request)


async def assert_unpublished(h):
    assert not await revisions(h)
    identity = {
        "account_id": h.item.config.account_id,
        "reporting_obligation_id": h.item.obligation.reporting_obligation_id,
    }
    assert await h.store.get_provisional_observation(**identity) is None
    assert await h.store.get_restatement_checkpoint(**identity) is None


def harness(backend, path, *, source_factory=RevocableSource, **kwargs):
    return production_harness(
        backend,
        path,
        count=1,
        source_publication=True,
        periods=1,
        source_factory=source_factory,
        **kwargs,
    )


@pytest.mark.parametrize("backend", ["memory", "postgres"])
async def test_revoked_before_dispatch_resumes_on_next_authorized_turn(backend, tmp_path):
    async with harness(backend, tmp_path / "destination.sqlite") as h:
        await h.production.activate(account_id=h.item.config.account_id)
        source = h.production.offerings[0].producer._source
        source.authorized = False
        revoked = await source_turn(h.production)
        assert not revoked.revisions_committed
        assert source.dispatched == []
        await assert_unpublished(h)

        source.authorized = True
        restored = await source_turn(h.production)
        assert len(restored.revisions_committed) == 1
        assert len(source.requests) == 1
        checkpoint = await h.store.get_restatement_checkpoint(
            account_id=h.item.config.account_id,
            reporting_obligation_id=h.item.obligation.reporting_obligation_id,
        )
        assert checkpoint is not None and checkpoint.next_observation == 1


@pytest.mark.parametrize("backend", ["memory", "postgres"])
async def test_dispatch_rechecks_after_acquisition_reservation(backend, tmp_path, monkeypatch):
    async with harness(backend, tmp_path / "destination.sqlite") as h:
        await h.production.activate(account_id=h.item.config.account_id)
        source = h.production.offerings[0].producer._source
        reserve = h.store.reserve_provisional_acquisition

        async def revoke_after_reservation(acquisition):
            result = await reserve(acquisition)
            source.authorized = False
            return result

        monkeypatch.setattr(h.store, "reserve_provisional_acquisition", revoke_after_reservation)
        revoked = await source_turn(h.production)
        assert not revoked.revisions_committed
        assert source.dispatched == []
        await assert_unpublished(h)

        monkeypatch.setattr(h.store, "reserve_provisional_acquisition", reserve)
        source.authorized = True
        restored = await source_turn(h.production)
        assert len(restored.revisions_committed) == 1
        assert len(source.dispatched) == 1


@pytest.mark.parametrize("backend", ["memory", "postgres"])
async def test_inflight_revocation_discards_result_without_cancelling_fetch(backend, tmp_path):
    async with harness(backend, tmp_path / "destination.sqlite") as h:
        await h.production.activate(account_id=h.item.config.account_id)
        source = h.production.offerings[0].producer._source
        source.release.clear()
        running = asyncio.create_task(source_turn(h.production))
        try:
            await asyncio.wait_for(source.started.wait(), 10)
            source.authorized = False
            assert not running.done()
        finally:
            source.release.set()
        revoked = await asyncio.wait_for(running, 10)
        assert not revoked.revisions_committed and not revoked.slices_failed
        assert len(source.requests) == 1
        assert not source.cancelled
        await assert_unpublished(h)
        request = source.dispatched[0]
        assert (
            await source.inline._seals.get(
                account_id=request.identity.account_id,
                source_execution_key=request.identity.source_execution_key,
            )
            is None
        )

        # Changed rows prove the rejected result did not become a replay seal.
        source.rows = reference_rows(2)
        source.authorized = True
        restored = await source_turn(h.production)
        assert len(restored.revisions_committed) == 1
        assert len(source.requests) == 2
        assert (await revisions(h))[0].row_count == 2
        assert source.dispatched[1].identity == request.identity


@pytest.mark.parametrize("backend", ["memory", "postgres"])
async def test_seal_authorization_checked_after_obtaining_account_lock(
    backend, tmp_path, monkeypatch
):
    async with harness(backend, tmp_path / "destination.sqlite") as h:
        await h.production.activate(account_id=h.item.config.account_id)
        source = h.production.offerings[0].producer._source
        source.release.clear()
        running = asyncio.create_task(source_turn(h.production))
        waiting = asyncio.Event()
        publication = h.store._source_publication

        @asynccontextmanager
        async def observe_publication(account_id, **kwargs):
            waiting.set()
            async with publication(account_id, **kwargs) as publish_seal:
                yield publish_seal

        try:
            await asyncio.wait_for(source.started.wait(), 10)
            monkeypatch.setattr(h.store, "_source_publication", observe_publication)
            async with publication(h.item.config.account_id):
                source.release.set()
                await asyncio.wait_for(waiting.wait(), 10)
                assert not running.done()
                source.authorized = False
        finally:
            source.release.set()
        revoked = await asyncio.wait_for(running, 10)
        assert not revoked.revisions_committed
        await assert_unpublished(h)
        request = source.dispatched[0]
        assert (
            await source.inline._seals.get(
                account_id=request.identity.account_id,
                source_execution_key=request.identity.source_execution_key,
            )
            is None
        )


@pytest.mark.parametrize("backend", ["memory", "postgres"])
async def test_revoked_before_ledger_commit_replay_rechecks_after_restart(
    backend, tmp_path, monkeypatch
):
    path = tmp_path / "destination.sqlite"
    async with harness(backend, path) as h:
        await h.production.activate(account_id=h.item.config.account_id)
        source = h.production.offerings[0].producer._source
        read = source.reader.read

        async def revoke_after_read(**kwargs):
            result = await read(**kwargs)
            source.authorized = False
            return result

        monkeypatch.setattr(source.reader, "read", revoke_after_read)
        revoked = await source_turn(h.production)
        assert not revoked.revisions_committed
        await assert_unpublished(h)
        request = source.dispatched[0]
        sealed = await source.inline._seals.get(
            account_id=request.identity.account_id,
            source_execution_key=request.identity.source_execution_key,
        )
        assert sealed is not None, "seal preceded revocation, ledger publication did not"
        await h.production.aclose()

        existing = {"existing_store": h.store} if h.pool is None else {"existing_pool": h.pool}
        async with harness(
            backend, path, source_factory=partial(RevocableSource, authorized=False), **existing
        ) as fresh:
            resumed = fresh.production.offerings[0].producer._source
            await source_turn(fresh.production)
            assert resumed.dispatched == []
            await assert_unpublished(fresh)

            resumed.authorized = True
            restored = await source_turn(fresh.production)
            assert len(restored.revisions_committed) == 1
            assert len(resumed.dispatched) == 1
            assert resumed.dispatched[0].identity == request.identity
            assert resumed.requests == [], "the existing seal was replayed with fresh authority"


@pytest.mark.parametrize("backend", ["memory", "postgres"])
async def test_revocation_stops_account_for_turn_and_other_accounts_continue(backend, tmp_path):
    class WithdrawOnceSource(RevocableSource):
        withdrawn = False
        refuse_next_check = False

        async def fetch(self, request):
            result = await super().fetch(request)
            if request.identity.account_id == "acct_a" and not self.withdrawn:
                self.withdrawn = self.refuse_next_check = True
            return result

        def configuration_binding(self, configuration):
            if self.refuse_next_check:
                self.refuse_next_check = False
                return None
            return super().configuration_binding(configuration)

    key = reference_verifier().key
    source = WithdrawOnceSource(
        key,
        tmp_path / "source",
        reference_rows(1),
        clock=lambda: END,
        close_officially=False,
        product_ids=(key.report_definition_id,),
    )

    def account_context(config):
        return ReportingAccountContext(
            config.account_id,
            "source",
            "USD",
            source.capabilities.source_scope,
            snapshot_offering_id=source.source_id,
            capability_offering={
                "offering_id": source.source_id,
                "feed_purpose": config.feed_purpose,
                "report_definition_id": config.report_definition_id,
                "supported_finality": ["snapshot"],
            },
            publication_namespace=source.capabilities.offerings[0].publication_namespace,
        )

    async with AsyncExitStack() as stack:
        if backend == "memory":
            store = InMemoryReportingLedgerStore(clock=lambda: END)
        else:
            from adcp.reporting.ledger.pg import PgReportingLedgerStore

            pool = await stack.enter_async_context(isolated_reporting_pool(autocommit=True))
            store = PgReportingLedgerStore(pool=pool, clock=lambda: END)
        service = ReliableReportingService(
            store=store, account_context=account_context, clock=lambda: END
        )
        stack.push_async_callback(service.close)
        service.sources.register_executor("source", source, object_reader=source.reader)
        configs = []
        for account_id, config_id in (("acct_a", "first"), ("acct_a", "later"), ("acct_b", "only")):
            config = replace(
                configuration(account_id),
                delivery_config_id=config_id,
                definition=key.definition,
                report_definition_id=key.report_definition_id,
                reporting_profile=key.reporting_profile,
            )
            source.bind_generation(config, product_id=key.report_definition_id)
            await service.configure(config)
            configs.append(config)

        turn = await service.run_worker()
        assert not turn.configuration_errors
        assert [request.identity.account_id for request in source.dispatched] == [
            "acct_a",
            "acct_b",
        ]
        assert not turn.configurations[configs[0].generation_key].revisions_committed
        assert not turn.configurations[configs[1].generation_key].revisions_committed
        assert len(turn.configurations[configs[2].generation_key].revisions_committed) == 1

        restored = await service.run_worker()
        assert not restored.configuration_errors
        assert len(restored.configurations[configs[0].generation_key].revisions_committed) == 1
        assert len(restored.configurations[configs[1].generation_key].revisions_committed) == 1


@pytest.mark.parametrize("interruption", ["revoked", "storage_error", "cancelled"])
@pytest.mark.parametrize("autocommit", [False, True])
async def test_postgres_inline_publication_shares_size_one_pool(
    tmp_path, monkeypatch, interruption, autocommit
):
    pools = pytest.importorskip("psycopg_pool")
    psycopg = pytest.importorskip("psycopg")

    from adcp.reporting.inline_source import InlineReportingSource
    from adcp.reporting.inline_storage import (
        InlineStorageError,
        PgReportingSealStore,
        PgReportingStagingStore,
    )

    async with isolated_reporting_pool(autocommit=autocommit) as isolated:
        async with pools.AsyncConnectionPool(
            isolated.conninfo,
            kwargs=isolated.kwargs,
            min_size=1,
            max_size=1,
            timeout=2,
            open=False,
        ) as pool:
            staging = PgReportingStagingStore(pool=pool)
            seals = PgReportingSealStore(pool=pool)
            await staging.create_schema()

            def factory(*args, **kwargs):
                source = RevocableSource(*args, **kwargs)
                source.inline = InlineReportingSource(
                    capabilities=source.capabilities,
                    fetch=source.fetch,
                    staging=staging,
                    seals=seals,
                    constituent_of=lambda row, request: (
                        request.coverage.constituents[0].constituent_id
                    ),
                    clock=kwargs["clock"],
                )
                source.reader = staging
                return source

            async with harness(
                "postgres",
                tmp_path / "destination.sqlite",
                existing_pool=pool,
                source_factory=factory,
            ) as h:
                await h.production.activate(account_id=h.item.config.account_id)
                source = h.production.offerings[0].producer._source
                source.authorized = False
                denied = await source_turn(h.production)
                assert not denied.revisions_committed and not source.dispatched
                await assert_unpublished(h)

                source.authorized = True
                put_on = seals._put_on
                inserted = asyncio.Event()

                async def interrupted_put(connection, prepared):
                    result = await put_on(connection, prepared)
                    if interruption == "storage_error":
                        raise psycopg.OperationalError("private-storage-detail")
                    if interruption == "cancelled":
                        inserted.set()
                        await asyncio.Event().wait()
                    return result

                monkeypatch.setattr(seals, "_put_on", interrupted_put)
                source.release.clear()
                running = asyncio.create_task(source_turn(h.production))
                try:
                    await asyncio.wait_for(source.started.wait(), 10)
                    if interruption == "revoked":
                        source.authorized = False
                finally:
                    source.release.set()
                if interruption == "cancelled":
                    await asyncio.wait_for(inserted.wait(), 10)
                    running.cancel()
                    with pytest.raises(asyncio.CancelledError):
                        await asyncio.wait_for(running, 10)
                elif interruption == "storage_error":
                    with pytest.raises(InlineStorageError) as caught:
                        await asyncio.wait_for(running, 10)
                    assert caught.value.code == "RESOURCE_UNAVAILABLE"
                    assert caught.value.__context__ is None and caught.value.__cause__ is None
                    assert "private-storage-detail" not in str(caught.value)
                else:
                    revoked = await asyncio.wait_for(running, 10)
                    assert not revoked.revisions_committed
                await assert_unpublished(h)
                request = source.dispatched[0]
                assert (
                    await seals.get(
                        account_id=request.identity.account_id,
                        source_execution_key=request.identity.source_execution_key,
                    )
                    is None
                )

                monkeypatch.setattr(seals, "_put_on", put_on)
                source.authorized = True
                source.rows = reference_rows(2)
                restored = await asyncio.wait_for(source_turn(h.production), 10)
                assert len(restored.revisions_committed) == 1
                assert (await revisions(h))[0].row_count == 2
                assert source.cancelled == (interruption == "cancelled")
                assert len(source.requests) == 2
