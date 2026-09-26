"""Successful scheduled reads are immutable observations in memory and PostgreSQL."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import timedelta, timezone

import pytest
from pydantic import ValidationError

from adcp.reporting.ledger import (
    InMemoryReportingLedgerStore,
    PgReportingLedgerStore,
    ReportingProducer,
    revision_content_sha256,
)
from adcp.reporting.ledger.store import LedgerConflictError
from adcp.reporting.source import SourceBatchManifestV1
from tests.conformance.reporting._generation_support import isolated_reporting_pool
from tests.test_reporting_settling import (
    ACCOUNT,
    _capabilities,
    _harness,
    _only_obligation,
    _revisions,
)


@pytest.fixture(params=["memory", "postgres"])
async def make_harness(request):
    if request.param == "memory":

        async def build(capabilities):
            return await _harness(
                capabilities,
                store_factory=lambda clock: InMemoryReportingLedgerStore(
                    clock=clock, notifications=True
                ),
            )

        yield build
    else:
        async with isolated_reporting_pool() as pool:

            async def build(capabilities):
                return await _harness(
                    capabilities,
                    store_factory=lambda clock: PgReportingLedgerStore(
                        pool=pool, clock=clock, notifications=True
                    ),
                )

            yield build


async def latest(store):
    obligation = await _only_obligation(store)
    observation = await store.get_provisional_observation(
        account_id=ACCOUNT, reporting_obligation_id=obligation.reporting_obligation_id
    )
    assert observation is not None
    return observation


async def _publication_effect_counts(store):
    if isinstance(store, PgReportingLedgerStore):
        async with store._pool.connection() as connection:
            return await (
                await connection.execute(
                    "SELECT (SELECT count(*) FROM reporting_revisions),"
                    " (SELECT count(*) FROM reporting_revision_rows),"
                    " (SELECT count(*) FROM reporting_provisional_observations),"
                    " (SELECT count(*) FROM reporting_restatement_checkpoints),"
                    " (SELECT count(*) FROM reporting_ledger_changes),"
                    " (SELECT count(*) FROM reporting_notification_events)"
                )
            ).fetchone()
    return (
        len(store._revisions),
        sum(len(rows) for rows in store._rows.values()),
        len(store._provisional_observations),
        len(store._restatement_checkpoints),
        len(store._changes),
        len(store._notification_state.events),
        len(store._notification_state.boundaries),
    )


async def test_adapter_without_window_rereads_with_sdk_default(make_harness):
    producer, store, fetch, clock = await make_harness(_capabilities(restatement_window=None))
    first = await producer.run_worker()
    assert len(first.revisions_committed) == 1
    initial = await latest(store)
    obligation = await _only_obligation(store)
    assert initial.provisional_until == obligation.period.end + timedelta(days=3)

    clock[0] += timedelta(hours=1)
    refreshed = await producer.run_worker()

    assert fetch.calls == ["PROVISIONAL_SNAPSHOT", "PROVISIONAL_SNAPSHOT"]
    assert len(refreshed.revisions_committed) == 1
    assert len(await _revisions(store)) == 2
    assert all(item.finality == "snapshot" for item in await _revisions(store))


async def test_unchanged_scheduled_read_is_a_distinct_immutable_observation(make_harness):
    producer, store, fetch, clock = await make_harness(
        _capabilities(restatement_window="P3D", restatement_cadence="PT1H")
    )
    await producer.run_worker()
    original = (await _revisions(store))[0]
    first = await latest(store)
    original_rows = await store.read_revision_rows(
        account_id=ACCOUNT, reporting_revision_id=original.reporting_revision_id
    )
    clock[0] += timedelta(hours=1)
    refreshed = await producer.run_worker()

    assert fetch.calls == ["PROVISIONAL_SNAPSHOT", "PROVISIONAL_SNAPSHOT"]
    assert len(refreshed.revisions_committed) == 1
    history = await _revisions(store)
    assert len(history) == 2 and history[0] == original
    assert history[1].reporting_revision_id != original.reporting_revision_id
    assert history[1].supersedes_reporting_revision_id == original.reporting_revision_id
    assert history[1].observed_at > original.observed_at
    assert history[1].source_manifest_sha256 == original.source_manifest_sha256
    assert history[1].revision_content_sha256 != original.revision_content_sha256
    second = await latest(store)
    assert second.acquisition.ordinal == first.acquisition.ordinal + 1
    assert second.checked_at > first.checked_at
    assert second.next_due_at == clock[0] + timedelta(hours=1)
    assert second.revision_id == history[1].reporting_revision_id
    current_rows = await store.read_revision_rows(
        account_id=ACCOUNT, reporting_revision_id=second.revision_id
    )
    assert current_rows.rows == original_rows.rows
    # The legacy SQL layout retains every new revision's rows as well.
    if isinstance(store, PgReportingLedgerStore):
        async with store._pool.connection() as connection:
            counts = await (
                await connection.execute(
                    "SELECT reporting_revision_id,count(*) FROM reporting_revision_rows"
                    " GROUP BY reporting_revision_id ORDER BY reporting_revision_id"
                )
            ).fetchall()
        assert sorted(count for _, count in counts) == [1, 1]
    await producer.run_worker()
    assert len(fetch.calls) == 2


@pytest.mark.parametrize("offset", [timedelta(minutes=30), timedelta(days=4)])
async def test_source_boundary_shortens_or_extends_fallback(make_harness, offset):
    producer, store, fetch, clock = await make_harness(_capabilities(restatement_window=None))
    # Deliberately use a non-UTC representation of the same instant.
    boundary = (clock[0] + offset).astimezone(timezone(timedelta(hours=-5)))
    fetch.provisional_until = boundary
    await producer.run_worker()
    first = await latest(store)
    assert first.provisional_until == boundary
    assert first.next_due_at == min(clock[0] + timedelta(hours=1), boundary)
    clock[0] = first.next_due_at
    await producer.run_worker()
    assert len(fetch.calls) == 2
    assert len(await _revisions(store)) == 2


async def test_final_read_at_boundary_and_overdue_restart_never_promote(make_harness):
    producer, store, fetch, clock = await make_harness(_capabilities(restatement_window=None))
    await producer.run_worker()
    first = await latest(store)
    clock[0] = first.provisional_until + timedelta(days=1)
    await producer.run_worker()
    final = await latest(store)
    assert final.next_due_at is None
    assert len(fetch.calls) == 2
    assert len(await _revisions(store)) == 2
    assert all(item.finality == "snapshot" for item in await _revisions(store))
    await producer.run_worker()
    assert len(fetch.calls) == 2


async def test_exact_boundary_gets_one_final_snapshot(make_harness):
    producer, store, fetch, clock = await make_harness(_capabilities(restatement_window=None))
    fetch.provisional_until = clock[0] + timedelta(minutes=30)
    await producer.run_worker()
    clock[0] = fetch.provisional_until
    await producer.run_worker()
    assert len(fetch.calls) == 2
    assert (await latest(store)).next_due_at is None
    await producer.run_worker()
    assert len(fetch.calls) == 2


async def test_new_source_override_can_extend_final_observation(make_harness):
    producer, store, fetch, clock = await make_harness(_capabilities(restatement_window=None))
    fetch.provisional_until = clock[0] + timedelta(minutes=30)
    await producer.run_worker()
    clock[0] = fetch.provisional_until
    fetch.provisional_until = clock[0] + timedelta(days=1)
    await producer.run_worker()
    second = await latest(store)
    assert second.next_due_at == clock[0] + timedelta(hours=1)
    clock[0] = second.next_due_at
    await producer.run_worker()
    assert len(fetch.calls) == 3


async def test_changed_content_and_restart_keep_frozen_cadence(make_harness):
    producer, store, fetch, clock = await make_harness(
        _capabilities(restatement_window="P3D", restatement_cadence="PT1H")
    )
    await producer.run_worker()
    first = await latest(store)
    # A fresh store instance, when PostgreSQL-backed, must restore all policy.
    restored = (
        PgReportingLedgerStore(pool=store._pool, clock=lambda: clock[0], notifications=True)
        if isinstance(store, PgReportingLedgerStore)
        else store
    )
    source = producer._source
    source._capabilities = _capabilities(restatement_window="P1D", restatement_cadence="PT2H")
    fresh = ReportingProducer(
        source=source,
        offerings=producer._offerings,
        store=restored,
        object_reader=producer._object_reader,
        max_periods_per_turn=1,
        clock=lambda: clock[0],
    )
    clock[0] = first.next_due_at
    fetch.impressions = 27
    await fresh.run_worker()
    second = await latest(restored)
    assert second.acquisition.policy == first.acquisition.policy
    assert second.next_due_at == clock[0] + timedelta(hours=1)
    assert len(await _revisions(restored)) == 2
    assert (await _revisions(restored))[0].source_manifest_sha256 != (await _revisions(restored))[
        1
    ].source_manifest_sha256


async def test_checkpoint_failure_rolls_back_observation_and_retry_keeps_identity(
    make_harness, monkeypatch
):
    producer, store, fetch, clock = await make_harness(_capabilities(restatement_window=None))
    original = store.record_restatement_checkpoint

    async def fail_after_checkpoint(checkpoint):
        await original(checkpoint)
        raise RuntimeError("checkpoint fault")

    monkeypatch.setattr(store, "record_restatement_checkpoint", fail_after_checkpoint)
    with pytest.raises(RuntimeError, match="checkpoint fault"):
        await producer.run_worker()
    assert await _revisions(store) == ()
    obligation = await _only_obligation(store)
    assert (
        await store.get_restatement_checkpoint(
            account_id=ACCOUNT, reporting_obligation_id=obligation.reporting_obligation_id
        )
        is None
    )
    assert (
        await store.get_provisional_observation(
            account_id=ACCOUNT, reporting_obligation_id=obligation.reporting_obligation_id
        )
        is None
    )
    monkeypatch.setattr(store, "record_restatement_checkpoint", original)
    clock[0] += timedelta(hours=2)
    await producer.run_worker()
    # Replay after the original deadline consumes the retained source seal.
    assert fetch.calls == ["PROVISIONAL_SNAPSHOT"]
    assert len(await _revisions(store)) == 1
    assert (await latest(store)).acquisition.ordinal == 0


async def test_concurrent_same_acquisition_commits_once(make_harness):
    producer, store, fetch, clock = await make_harness(_capabilities(restatement_window=None))
    await producer.run_worker()
    first = await latest(store)
    clock[0] = first.next_due_at
    config = (await store.list_configurations(account_id=ACCOUNT))[0]
    obligation = await _only_obligation(store)
    await asyncio.gather(
        *[
            producer.acquire_obligation(
                config, obligation, restate=True, now=clock[0], track_settling=True
            )
            for _ in range(2)
        ]
    )
    assert len(await _revisions(store)) == 2
    assert (await latest(store)).acquisition.ordinal == 1
    checkpoint = await store.get_restatement_checkpoint(
        account_id=ACCOUNT, reporting_obligation_id=obligation.reporting_obligation_id
    )
    assert checkpoint.next_observation == 2


async def test_account_cannot_read_or_publish_another_observation(make_harness):
    producer, store, fetch, clock = await make_harness(_capabilities(restatement_window=None))
    await producer.run_worker()
    observation = await latest(store)
    assert (
        await store.get_provisional_observation(
            account_id="other", reporting_obligation_id=observation.acquisition.obligation_id
        )
        is None
    )
    revision = (await _revisions(store))[0]
    # Reserved identity cannot be rebound to a different account at commit.
    with pytest.raises(LedgerConflictError):
        forged = replace(observation, revision_id="forged")
        await store.commit_provisional_observation(
            forged, replace(revision, account_id="other", reporting_revision_id="forged"), []
        )


def test_no_window_cannot_enable_official_close_lag():
    with pytest.raises(ValidationError, match="require restatement_window"):
        _capabilities(restatement_window=None, official_close_lag="P3D")


async def test_unsealed_retry_refreshes_only_execution_deadline(make_harness):
    producer, store, fetch, clock = await make_harness(_capabilities(restatement_window=None))
    source = producer._source
    original_fetch = source._fetch
    requests = []

    def not_ready_once(request):
        assert request.deadline_at > clock[0]
        requests.append(request.model_dump(mode="json"))
        return None if len(requests) == 1 else original_fetch(request)

    source._fetch = not_ready_once
    await producer.run_worker()
    assert await _revisions(store) == ()
    original_deadline = requests[0]["deadline_at"]
    clock[0] += timedelta(hours=2)
    await producer.run_worker()
    assert len(requests) == 2
    assert requests[1]["deadline_at"] != original_deadline
    assert {k: v for k, v in requests[0].items() if k != "deadline_at"} == {
        k: v for k, v in requests[1].items() if k != "deadline_at"
    }
    assert len(await _revisions(store)) == 1
    assert (await latest(store)).acquisition.ordinal == 0


@pytest.mark.parametrize("invalid_kind", ["past", "naive"])
async def test_invalid_source_boundary_commits_nothing(make_harness, invalid_kind):
    producer, store, fetch, clock = await make_harness(_capabilities(restatement_window=None))
    fetch.provisional_until = (
        clock[0] - timedelta(seconds=1) if invalid_kind == "past" else clock[0].replace(tzinfo=None)
    )
    with pytest.raises(ValidationError, match="provisional_until"):
        await producer.run_worker()
    assert await _revisions(store) == ()
    obligation = await _only_obligation(store)
    assert (
        await store.get_restatement_checkpoint(
            account_id=ACCOUNT, reporting_obligation_id=obligation.reporting_obligation_id
        )
        is None
    )
    fetch.provisional_until = clock[0]
    await producer.run_worker()
    assert (await latest(store)).acquisition.ordinal == 0
    assert (await latest(store)).next_due_at is None


async def test_cancellation_after_checkpoint_rolls_back_publication(make_harness, monkeypatch):
    producer, store, fetch, clock = await make_harness(_capabilities(restatement_window=None))
    original = store.record_restatement_checkpoint

    async def cancelled(checkpoint):
        await original(checkpoint)
        raise asyncio.CancelledError

    monkeypatch.setattr(store, "record_restatement_checkpoint", cancelled)
    with pytest.raises(asyncio.CancelledError):
        await producer.run_worker()
    assert await _revisions(store) == ()
    monkeypatch.setattr(store, "record_restatement_checkpoint", original)
    await producer.run_worker()
    assert len(await _revisions(store)) == 1
    assert fetch.calls == ["PROVISIONAL_SNAPSHOT"]


async def test_watermark_and_history_advance_with_unchanged_rows(make_harness):
    producer, store, fetch, clock = await make_harness(_capabilities(restatement_window=None))
    source = producer._source
    original_fetch = source._fetch
    complete = False

    def observed(request):
        result = original_fetch(request)
        return replace(
            result,
            data_through=request.period.end - (timedelta(0) if complete else timedelta(minutes=30)),
        )

    source._fetch = observed
    await producer.run_worker()
    first = (await _revisions(store))[0]
    complete = True
    clock[0] += timedelta(hours=1)
    await producer.run_worker()
    history = await _revisions(store)
    assert len(history) == 2
    assert history[1].data_through > first.data_through
    assert history[1].source_manifest_sha256 == first.source_manifest_sha256
    assert (await latest(store)).revision_id == history[1].reporting_revision_id


async def test_schema_reinstallation_preserves_observations_and_row_layout(make_harness):
    producer, store, fetch, clock = await make_harness(_capabilities(restatement_window=None))
    await producer.run_worker()
    prior = await latest(store)
    prior_revisions = await _revisions(store)
    await store.create_schema()
    assert await latest(store) == prior
    assert await _revisions(store) == prior_revisions
    if isinstance(store, PgReportingLedgerStore):
        async with store._pool.connection() as connection:
            columns = await (
                await connection.execute(
                    "SELECT column_name FROM information_schema.columns"
                    " WHERE table_schema=current_schema() AND table_name='reporting_revision_rows'"
                    " ORDER BY ordinal_position"
                )
            ).fetchall()
        assert [row[0] for row in columns] == ["reporting_revision_id", "ordinal", "row_payload"]
        import psycopg

        async with store._pool.connection() as connection:
            with pytest.raises(psycopg.errors.RaiseException, match="immutable"):
                async with connection.transaction():
                    await connection.execute("DELETE FROM reporting_provisional_observations")
        assert await latest(store) == prior


async def test_generation_and_replay_identity_cannot_be_rebound(make_harness):
    producer, store, fetch, clock = await make_harness(_capabilities(restatement_window=None))
    await producer.run_worker()
    observation = await latest(store)
    acquisition = observation.acquisition
    request = acquisition.request()
    wrong_generation = request.model_copy(
        update={
            "identity": request.identity.model_copy(
                update={"delivery_config_version": request.identity.delivery_config_version + 1}
            )
        }
    )
    forged = replace(acquisition, ordinal=1, request_json=wrong_generation.model_dump_json())
    with pytest.raises(LedgerConflictError, match="generation differs"):
        await store.reserve_provisional_acquisition(forged)
    with pytest.raises(LedgerConflictError, match="already reserved"):
        await store.reserve_provisional_acquisition(replace(acquisition, ordinal=1))
    with pytest.raises(LedgerConflictError, match="replay differs"):
        await store.commit_provisional_observation(
            replace(observation, acquisition=replace(forged, ordinal=0)),
            (await _revisions(store))[0],
            [],
        )
    assert await latest(store) == observation


async def test_recorded_observation_replay_validates_content_and_keeps_original_state(make_harness):
    producer, store, fetch, clock = await make_harness(
        _capabilities(restatement_window="P3D", restatement_cadence="PT1H")
    )
    await producer.run_worker()
    parent = (await _revisions(store))[0]
    clock[0] += timedelta(hours=1)
    fetch.impressions = 27
    await producer.run_worker()
    revision = (await _revisions(store))[1]
    observation = await latest(store)
    assert revision.supersedes_reporting_revision_id == parent.reporting_revision_id
    rows = (
        await store.read_revision_rows(
            account_id=ACCOUNT, reporting_revision_id=revision.reporting_revision_id
        )
    ).rows
    assert len(rows) == revision.row_count == 1
    checkpoint = await store.get_restatement_checkpoint(
        account_id=ACCOUNT, reporting_obligation_id=observation.acquisition.obligation_id
    )
    effects = await _publication_effect_counts(store)
    changed_rows = (dict(rows[0], impressions=rows[0]["impressions"] + 1),)
    changed_revision = replace(
        revision,
        revision_content_sha256=revision_content_sha256(
            reporting_revision_id=revision.reporting_revision_id,
            row_count=revision.row_count,
            control_totals=revision.control_totals,
            reporting_rows=changed_rows,
        ),
    )
    assert changed_revision.revision_content_sha256 != revision.revision_content_sha256
    for submitted_revision, submitted_rows, code in (
        (changed_revision, changed_rows, "REVISION_IMMUTABLE"),
        (revision, (), "ROW_COUNT_MISMATCH"),
    ):
        with pytest.raises(LedgerConflictError) as conflict:
            await store.commit_provisional_observation(
                observation, submitted_revision, submitted_rows
            )
        assert conflict.value.code == code
        assert (await _revisions(store)) == (parent, revision)
        assert (
            await store.read_revision_rows(
                account_id=ACCOUNT, reporting_revision_id=revision.reporting_revision_id
            )
        ).rows == rows
        assert await latest(store) == observation
        assert (
            await store.get_restatement_checkpoint(
                account_id=ACCOUNT, reporting_obligation_id=observation.acquisition.obligation_id
            )
            == checkpoint
        )
        assert await _publication_effect_counts(store) == effects

    # This is the producer's acquisition-supplied branch, which reconstructs
    # a new digest for the same publication ID before calling the observation store.
    manifest = SourceBatchManifestV1.model_validate_json(observation.manifest_json)
    obligation = await _only_obligation(store)
    with pytest.raises(LedgerConflictError) as conflict:
        await producer.commit_revision_from_manifest(
            obligation,
            manifest,
            rows=changed_rows,
            finality="snapshot",
            now=clock[0],
            acquisition=observation.acquisition,
        )
    assert conflict.value.code == "REVISION_IMMUTABLE"
    assert await latest(store) == observation
    assert (
        await store.get_restatement_checkpoint(
            account_id=ACCOUNT, reporting_obligation_id=observation.acquisition.obligation_id
        )
        == checkpoint
    )
    assert await _publication_effect_counts(store) == effects

    clock[0] += timedelta(hours=2)
    replay_store = (
        PgReportingLedgerStore(pool=store._pool, clock=lambda: clock[0], notifications=True)
        if isinstance(store, PgReportingLedgerStore)
        else store
    )
    later_observation = replace(
        observation,
        checked_at=clock[0],
        next_due_at=clock[0] + timedelta(hours=1),
    )
    assert (
        await replay_store.commit_provisional_observation(later_observation, revision, rows)
        == revision
    )
    assert (
        await producer.commit_revision_from_manifest(
            obligation,
            manifest,
            rows=rows,
            finality="snapshot",
            now=clock[0],
            acquisition=observation.acquisition,
        )
        == revision
    )
    assert revision.created_at < clock[0]
    assert revision.supersedes_reporting_revision_id == parent.reporting_revision_id
    assert observation.acquisition.ordinal == 1
    assert observation.checked_at < clock[0]
    assert observation.next_due_at is not None
    assert await latest(replay_store) == observation
    assert (
        await replay_store.get_restatement_checkpoint(
            account_id=ACCOUNT, reporting_obligation_id=observation.acquisition.obligation_id
        )
        == checkpoint
    )
    assert (await _revisions(replay_store)) == (parent, revision)
    assert (
        await replay_store.read_revision_rows(
            account_id=ACCOUNT, reporting_revision_id=revision.reporting_revision_id
        )
    ).rows == rows
    assert await _publication_effect_counts(replay_store) == effects


async def test_notification_failure_rolls_back_revision_checkpoint_and_history(
    make_harness, monkeypatch
):
    producer, store, fetch, clock = await make_harness(_capabilities(restatement_window=None))
    original = store._record_notification
    if isinstance(store, PgReportingLedgerStore):

        async def fail(connection, event):
            await original(connection, event)
            raise RuntimeError("notification fault")

    else:

        def fail(event):
            original(event)
            raise RuntimeError("notification fault")

    monkeypatch.setattr(store, "_record_notification", fail)
    with pytest.raises(RuntimeError, match="notification fault"):
        await producer.run_worker()
    assert await _revisions(store) == ()
    obligation = await _only_obligation(store)
    assert (
        await store.get_restatement_checkpoint(
            account_id=ACCOUNT, reporting_obligation_id=obligation.reporting_obligation_id
        )
        is None
    )
    if isinstance(store, PgReportingLedgerStore):
        async with store._pool.connection() as connection:
            counts = await (
                await connection.execute(
                    "SELECT (SELECT count(*) FROM reporting_notification_events),"
                    " (SELECT count(*) FROM reporting_ledger_changes WHERE record_kind='revision'),"
                    " (SELECT count(*) FROM reporting_provisional_observations)"
                )
            ).fetchone()
        assert counts == (0, 0, 0)
    else:
        assert not store._notification_state.events
        assert not any(change[2] == "revision" for change in store._changes)
        assert not store._provisional_observations
    monkeypatch.setattr(store, "_record_notification", original)
    await producer.run_worker()
    assert len(await _revisions(store)) == 1
    assert fetch.calls == ["PROVISIONAL_SNAPSHOT"]


async def test_reserved_snapshot_retry_survives_explicit_close_boundary(make_harness):
    producer, store, fetch, clock = await make_harness(
        _capabilities(restatement_window="PT3H", official_close_lag="PT4H")
    )
    await producer.run_worker()
    source = producer._source
    original_fetch = source._fetch
    requests = []

    def not_ready_once(request):
        requests.append(request)
        return None if len(requests) == 1 else original_fetch(request)

    source._fetch = not_ready_once
    clock[0] += timedelta(hours=1)
    await producer.run_worker()
    assert len(await _revisions(store)) == 1
    clock[0] += timedelta(hours=4)
    await producer.run_worker()
    assert requests[1].identity == requests[0].identity
    assert requests[1].publication_class == "PROVISIONAL_SNAPSHOT"
    assert [revision.finality for revision in await _revisions(store)] == ["snapshot", "snapshot"]
    await producer.run_worker()
    history = await _revisions(store)
    assert sorted(revision.finality for revision in history) == ["official", "snapshot", "snapshot"]
    official = next(revision for revision in history if revision.finality == "official")
    assert (await latest(store)).revision_id == official.reporting_revision_id
    assert (await latest(store)).acquisition.ordinal == 2


async def test_empty_successful_reads_still_append_observations(make_harness):
    producer, store, fetch, clock = await make_harness(_capabilities(restatement_window=None))
    source = producer._source
    original_fetch = source._fetch
    source._fetch = lambda request: replace(original_fetch(request), rows=[])
    await producer.run_worker()
    clock[0] += timedelta(hours=1)
    await producer.run_worker()
    history = await _revisions(store)
    assert len(history) == 2
    assert all(revision.row_count == 0 and revision.readable for revision in history)
    assert history[1].supersedes_reporting_revision_id == history[0].reporting_revision_id
