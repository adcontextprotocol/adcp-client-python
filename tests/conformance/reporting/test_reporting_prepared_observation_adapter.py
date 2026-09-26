"""A decorating publisher composes the entire atomic observation contract."""

import asyncio
from dataclasses import replace
from datetime import timedelta

import pytest

from adcp.reporting.ledger import LedgerConflictError

from ._reliable_support import ScriptedSource, complete_fetch, configuration, reliable_factory
from .test_reporting_evidence_currency_integration import frozen_slice


@pytest.mark.parametrize("backend", ["memory", "postgres"])
@pytest.mark.parametrize("failure_point", ["revision.before", "revision.after"])
async def test_prepared_observation_faults_preserve_complete_atomic_state(backend, failure_point):
    async with reliable_factory(backend) as h:
        await h.store.put_configuration(configuration("eur"))
        script = ScriptedSource(eur=[complete_fetch])
        h.failures.at(failure_point, OSError("publisher process loss"))
        with pytest.raises(OSError, match="publisher process loss"):
            await h.producer(h.source(script.async_fetch)).run_worker()
        (request,) = script.requests
        identity = {
            "account_id": "eur",
            "reporting_obligation_id": request.identity.reporting_obligation_id,
        }
        revisions = await h.store.list_revisions(**identity)
        observation = await h.store.get_provisional_observation(**identity)
        checkpoint = await h.store.get_restatement_checkpoint(**identity)
        if failure_point == "revision.before":
            assert revisions == ()
            assert observation is None and checkpoint is None
        else:
            assert len(revisions) == 1
            assert observation is not None and checkpoint is not None
            assert observation.revision_id == revisions[0].reporting_revision_id
            assert observation.acquisition.execution_key == request.identity.source_execution_key
            assert checkpoint.next_observation == observation.acquisition.ordinal + 1
            assert checkpoint.checked_at == observation.checked_at
            assert checkpoint.provisional_until == observation.provisional_until
            assert revisions[0].managed_control_totals is not None
            assert revisions[0].managed_control_totals[-1].unit == "EUR"
            assert revisions[0].canonical_content_digest is not None

        await h.restart()
        replacement = ScriptedSource(eur=[])
        await h.producer(h.source(replacement.async_fetch)).run_worker()
        assert replacement.requests == []
        retained = await h.store.list_revisions(**identity)
        current = await h.store.get_provisional_observation(**identity)
        position = await h.store.get_restatement_checkpoint(**identity)
        assert len(retained) == 1 and current is not None and position is not None
        assert current.revision_id == retained[0].reporting_revision_id
        assert current.acquisition.execution_key == request.identity.source_execution_key
        assert position.next_observation == current.acquisition.ordinal + 1
        assert retained[0].managed_control_totals is not None
        assert retained[0].canonical_content_digest is not None
        if failure_point == "revision.after":
            assert (retained, current, position) == (revisions, observation, checkpoint)


@pytest.mark.parametrize("backend", ["memory", "postgres"])
async def test_prepared_ordinary_revision_keeps_the_existing_commit_seam(backend):
    async with reliable_factory(backend) as h:
        producer, obligation, request = await frozen_slice(h, partial=False)
        result = await h.source(complete_fetch).execute(request, cancel=asyncio.Event())
        revision = await h.commit_slice(producer, obligation, request, result)
        assert revision.managed_control_totals is not None
        assert revision.canonical_content_digest is not None
        assert h.failures.hits.count("revision.before") == 1
        assert h.failures.hits.count("revision.after") == 1
        identity = {
            "account_id": obligation.account_id,
            "reporting_obligation_id": obligation.reporting_obligation_id,
        }
        assert await h.store.get_provisional_observation(**identity) is None
        assert await h.store.get_restatement_checkpoint(**identity) is None


@pytest.mark.parametrize("backend", ["memory", "postgres"])
async def test_prepared_observation_replay_validates_managed_content(backend):
    async with reliable_factory(backend, notifications=True) as h:
        await h.store.put_configuration(configuration("eur"))
        script = ScriptedSource(eur=[complete_fetch])
        producer = h.producer(h.source(script.async_fetch))
        await producer.run_worker()
        (request,) = script.requests
        identity = {
            "account_id": "eur",
            "reporting_obligation_id": request.identity.reporting_obligation_id,
        }
        (revision,) = await h.store.list_revisions(**identity)
        observation = await h.store.get_provisional_observation(**identity)
        checkpoint = await h.store.get_restatement_checkpoint(**identity)
        assert observation is not None and checkpoint is not None
        assert revision.managed_control_totals is not None
        assert revision.canonical_content_digest is not None
        rows = (
            await h.store.read_revision_rows(
                account_id="eur", reporting_revision_id=revision.reporting_revision_id
            )
        ).rows
        assert len(rows) == revision.row_count and rows

        async def effects():
            if h.blobs.pool is not None:
                async with h.blobs.pool.connection() as connection:
                    return await (
                        await connection.execute(
                            "SELECT (SELECT count(*) FROM reporting_ledger_changes),"
                            " (SELECT count(*) FROM reporting_notification_events)"
                        )
                    ).fetchone()
            return (
                len(h.store._changes),
                len(h.store._notification_state.events),
                len(h.store._notification_state.boundaries),
            )

        original_effects = await effects()
        changed_rows = (
            dict(rows[0], impressions=rows[0]["impressions"] + 1),
            *rows[1:],
        )
        for submitted_revision, submitted_rows, code, publisher in (
            (revision, changed_rows, "REVISION_IMMUTABLE", producer._store),
            (revision, (), "ROW_COUNT_MISMATCH", producer._store),
            (
                replace(revision, revision_content_sha256="0" * 64),
                rows,
                "REVISION_CONTENT_MISMATCH",
                h.store,
            ),
        ):
            with pytest.raises(LedgerConflictError) as conflict:
                await publisher.commit_provisional_observation(
                    observation, submitted_revision, submitted_rows
                )
            assert conflict.value.code == code
            assert await h.store.list_revisions(**identity) == (revision,)
            assert (
                await h.store.read_revision_rows(
                    account_id="eur", reporting_revision_id=revision.reporting_revision_id
                )
            ).rows == rows
            assert await h.store.get_provisional_observation(**identity) == observation
            assert await h.store.get_restatement_checkpoint(**identity) == checkpoint
            assert await effects() == original_effects

        h.clock.advance(timedelta(hours=2))
        later = replace(
            observation,
            checked_at=h.clock(),
            next_due_at=h.clock() + timedelta(hours=1),
        )
        assert (
            await producer._store.commit_provisional_observation(later, revision, rows) == revision
        )
        assert await h.store.list_revisions(**identity) == (revision,)
        assert await h.store.get_provisional_observation(**identity) == observation
        assert await h.store.get_restatement_checkpoint(**identity) == checkpoint
        assert await effects() == original_effects
