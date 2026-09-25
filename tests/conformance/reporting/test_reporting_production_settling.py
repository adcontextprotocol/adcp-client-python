"""Production progress must retain unfinished source restatement policy across turns."""

from datetime import timedelta
from functools import partial

import pytest

from adcp.reporting.inline_source import InlineReportingSource
from adcp.reporting.source import (
    ReportingSourceCapabilitiesV1,
    reporting_source_capabilities_sha256_v1,
)

from ._generation_support import END
from ._materializer_support import reference_rows
from ._production_support import Source, SQLiteSeals, production_harness
from .test_reporting_production_lock_order import source_turn


class SettlingSource(Source):
    """Declare the complete policy before the production service binds its hash."""

    def __init__(self, key, path, rows=None, *, close_officially=True, **kwargs):
        super().__init__(key, path, rows, **kwargs)
        raw = self.capabilities.model_dump(mode="json")
        raw["offerings"][0].update(
            restatement_window="PT3H",
            restatement_cadence="PT1H",
            official_close_lag="PT4H" if close_officially else None,
        )
        self.official_source_id = None
        if close_officially:
            official = Source(key, path.with_name("official-contract"), rows, official=True)
            self.official_source_id = official.source_id
            raw["offerings"].append(official.capabilities.offerings[0].model_dump(mode="json"))
        raw["capabilities_sha256"] = reporting_source_capabilities_sha256_v1(raw)
        self.capabilities = ReportingSourceCapabilitiesV1.model_validate(raw)
        self.official_ready = False
        self.inline = InlineReportingSource(
            capabilities=self.capabilities,
            fetch=self.fetch,
            staging=self.inline.staging,
            seals=SQLiteSeals(path.with_suffix(".seals")),
            constituent_of=lambda row, req: req.coverage.constituents[0].constituent_id,
            clock=kwargs.get("clock") or (lambda: END),
        )

    async def fetch(self, request):
        if request.publication_class == "AUTHORITATIVE" and not self.official_ready:
            self.requests.append(request)
            return None
        return await super().fetch(request)


async def revisions(h):
    return await h.store.list_revisions(
        account_id=h.item.config.account_id,
        reporting_obligation_id=h.item.obligation.reporting_obligation_id,
    )


async def pending(h):
    producer = h.production.offerings[0].producer
    token = h.production._producer_turn.set(producer)
    try:
        return await h.store.next_producer_obligations(
            h.item.config, now=h.source_clock(), limit=64
        )
    finally:
        h.production._producer_turn.reset(token)


@pytest.mark.parametrize("backend", ["memory", "postgres"])
@pytest.mark.parametrize("close_officially", [False, True])
async def test_progress_retains_policy_until_terminal_publication(
    backend, close_officially, tmp_path
):
    async with production_harness(
        backend,
        tmp_path / "destination.sqlite",
        count=1,
        source_publication=True,
        periods=1,
        source_factory=partial(SettlingSource, close_officially=close_officially),
    ) as h:
        await h.production.activate(account_id=h.item.config.account_id)
        source = h.production.offerings[0].producer._source
        first = await source_turn(h.production)
        assert len(first.revisions_committed) == 1
        assert [r.finality for r in await revisions(h)] == ["snapshot"]
        production_operation_1 = await pending(h)
        assert production_operation_1 == (h.item.obligation.reporting_obligation_id,)

        h.source_clock.now = END + timedelta(minutes=59)
        await source_turn(h.production)
        assert len(source.requests) == 1
        h.source_clock.now = END + timedelta(hours=1)
        unchanged = await source_turn(h.production)
        assert len(unchanged.revisions_committed) == 1
        assert len(source.requests) == 2
        checkpoint = await h.store.get_restatement_checkpoint(
            account_id=h.item.config.account_id,
            reporting_obligation_id=h.item.obligation.reporting_obligation_id,
        )
        assert checkpoint is not None and checkpoint.next_observation == 2
        await source_turn(h.production)
        assert len(source.requests) == 2

        source.rows = reference_rows(2)
        h.source_clock.now = END + timedelta(hours=2)
        changed = await source_turn(h.production)
        assert len(changed.revisions_committed) == 1
        history = await revisions(h)
        assert len(history) == 3
        assert history[1].supersedes_reporting_revision_id == history[0].reporting_revision_id
        assert history[2].supersedes_reporting_revision_id == history[1].reporting_revision_id

        h.source_clock.now = END + timedelta(hours=3)
        await source_turn(h.production)
        assert len(source.requests) == (3 if close_officially else 4)
        if not close_officially:
            production_operation_5 = await pending(h)
            assert production_operation_5 == ()
            return
        production_operation_2 = await pending(h)
        assert production_operation_2 == (h.item.obligation.reporting_obligation_id,)
        h.source_clock.now = END + timedelta(hours=4)
        not_ready = await source_turn(h.production)
        assert not not_ready.revisions_committed
        production_operation_3 = await pending(h)
        assert production_operation_3 == (h.item.obligation.reporting_obligation_id,)
        source.official_ready = True
        completed = await source_turn(h.production)
        assert len(completed.revisions_committed) == 1
        assert [r.finality for r in await revisions(h)] == [
            "snapshot",
            "snapshot",
            "snapshot",
            "official",
        ]
        production_operation_4 = await pending(h)
        assert production_operation_4 == ()
        count = len(source.requests)
        await source_turn(h.production)
        assert len(source.requests) == count


@pytest.mark.parametrize("backend", ["memory", "postgres"])
async def test_settling_checkpoint_and_pending_work_survive_fresh_service(backend, tmp_path):
    path = tmp_path / "restart.sqlite"
    async with production_harness(
        backend,
        path,
        count=1,
        source_publication=True,
        periods=1,
        source_factory=SettlingSource,
    ) as h:
        await h.production.activate(account_id=h.item.config.account_id)
        initial = await source_turn(h.production)
        assert len(initial.revisions_committed) == 1
        h.source_clock.now = END + timedelta(hours=1)
        noop = await source_turn(h.production)
        assert len(noop.revisions_committed) == 1
        original_source = h.production.offerings[0].producer._source
        assert len(original_source.requests) == 2
        prior_keys = {r.identity.source_execution_key for r in original_source.requests}
        await h.production.aclose()
        existing = {"existing_store": h.store} if h.pool is None else {"existing_pool": h.pool}
        async with production_harness(
            backend,
            path,
            count=1,
            source_publication=True,
            periods=1,
            source_factory=SettlingSource,
            **existing,
        ) as fresh:
            source = fresh.production.offerings[0].producer._source
            assert source is not original_source
            if h.pool is not None:
                assert fresh.store is not h.store
            checkpoint = await fresh.store.get_restatement_checkpoint(
                account_id=fresh.item.config.account_id,
                reporting_obligation_id=fresh.item.obligation.reporting_obligation_id,
            )
            assert checkpoint is not None and checkpoint.next_observation == 2
            assert not source.requests
            source.rows = reference_rows(2)
            fresh.source_clock.now = END + timedelta(hours=2)
            changed = await source_turn(fresh.production)
            assert len(changed.revisions_committed) == 1
            assert len(source.requests) == 1
            assert source.requests[0].identity.source_execution_key not in prior_keys
            checkpoint = await fresh.store.get_restatement_checkpoint(
                account_id=fresh.item.config.account_id,
                reporting_obligation_id=fresh.item.obligation.reporting_obligation_id,
            )
            assert checkpoint is not None and checkpoint.next_observation == 3
            assert len(await revisions(fresh)) == 3
            unfinished = await pending(fresh)
            assert unfinished == (fresh.item.obligation.reporting_obligation_id,)
