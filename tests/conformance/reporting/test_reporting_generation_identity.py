"""Run identical tenant-isolation promises against memory and real PostgreSQL."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import FrozenInstanceError, replace
from datetime import timedelta

import pytest

from adcp.reporting.fixtures import redacted_capabilities
from adcp.reporting.inline_source import InlineReportingSource
from adcp.reporting.ledger import (
    ConsumerStatusIngest,
    ConsumerStatusRecord,
    InMemoryReportingLedgerStore,
    LeasedConfiguration,
    LedgerConflictError,
    ProducerOfferings,
    ReportingConfigurationGenerationKey,
    ReportingLedgerStore,
    ReportingProducer,
    ReportingStatusCaller,
    ReportingStatusHandler,
    consumer_mismatch_issue_key,
)
from adcp.reporting.ledger.pg import PgReportingLedgerStore
from adcp.reporting.outbox._schema import schema_objects
from adcp.reporting.source import ReportingSourceSliceRequestV1
from tests.conformance.reporting._generation_support import (
    END,
    NOW,
    START,
    UncalledSource,
    configuration,
    isolated_reporting_pool,
    obligation_for,
    revision_for,
)


@pytest.fixture(params=["memory", "postgres"])
async def store(request: pytest.FixtureRequest) -> AsyncIterator[ReportingLedgerStore]:
    if request.param == "memory":
        yield InMemoryReportingLedgerStore(clock=lambda: NOW)
    else:
        async with isolated_reporting_pool() as pool:
            ledger = PgReportingLedgerStore(pool=pool, clock=lambda: NOW)
            await ledger.create_schema()
            yield ledger


def test_generation_keys_are_public_frozen_and_account_qualified() -> None:
    from adcp.reporting.ledger.models import ReportingConfigurationGenerationKey as ModelKey

    config = configuration()
    key = ReportingConfigurationGenerationKey("acct_a", "daily", 1)
    assert ModelKey is ReportingConfigurationGenerationKey
    assert config.generation_key == obligation_for(config).generation_key == key
    lease = LeasedConfiguration("acct_a", "daily", 1, NOW)
    assert lease.generation_key == key
    assert (
        len({key, configuration("acct_b").generation_key, replace(key, delivery_config_version=2)})
        == 3
    )
    with pytest.raises(FrozenInstanceError):
        setattr(key, "account_id", "acct_b")


@pytest.mark.parametrize("accounts", [("acct_a", "acct_b"), ("Account", "account")])
async def test_same_name_generations_keep_independent_content(
    store: ReportingLedgerStore, accounts: tuple[str, str]
) -> None:
    first, second = (configuration(account) for account in accounts)
    second = replace(
        second,
        feed_purpose="billing",
        required_finality="official",
        schedule=replace(second.schedule, delivery_sla="PT4H"),
        automated_recovery_window=timedelta(hours=12),
    )
    await asyncio.gather(store.put_configuration(first), store.put_configuration(second))
    for config in (first, second):
        assert await store.list_configurations(account_id=config.account_id) == (config,)
        assert await store.list_configurations(
            account_id=config.account_id, delivery_config_ids=["daily"]
        ) == (config,)
        assert (
            await store.list_configurations(
                account_id=config.account_id, delivery_config_ids=["absent"]
            )
            == ()
        )
        await asyncio.gather(*(store.put_configuration(config) for _ in range(4)))
        with pytest.raises(LedgerConflictError) as caught:
            await store.put_configuration(replace(config, media_buy_ids=("changed",)))
        assert caught.value.code == "CONFIGURATION_GENERATION_IMMUTABLE"
    assert await store.list_configurations(account_id="unavailable") == ()

    new_version = replace(first, delivery_config_version=2, media_buy_ids=("new_buy",))
    await store.put_configuration(new_version)
    assert set(await store.list_configurations(account_id=first.account_id)) == {
        first,
        new_version,
    }
    assert await store.list_configurations(account_id=second.account_id) == (second,)


async def test_concurrent_changed_writes_cannot_silently_succeed(
    store: ReportingLedgerStore,
) -> None:
    candidates = [replace(configuration(), media_buy_ids=(f"mb_{index}",)) for index in range(8)]
    results = await asyncio.gather(
        *(store.put_configuration(config) for config in candidates), return_exceptions=True
    )
    assert sum(result is None for result in results) == 1
    for result in results:
        if result is not None:
            assert isinstance(result, LedgerConflictError)
            assert result.code == "CONFIGURATION_GENERATION_IMMUTABLE"
    winner = candidates[results.index(None)]
    assert await store.list_configurations(account_id=winner.account_id) == (winner,)


async def test_concurrent_leases_and_releases_keep_accounts_separate(
    store: ReportingLedgerStore,
) -> None:
    configs = (configuration(), configuration("acct_b"))
    await asyncio.gather(*(store.put_configuration(config) for config in configs))
    # Default producer ids may be shared. Releasing one tenant must not clear
    # every same-name generation held by that worker.
    leases = await asyncio.gather(
        *(store.lease_period_close(worker_id="shared", now=NOW, lease_seconds=60) for _ in range(6))
    )
    held = [lease for lease in leases if lease is not None]
    assert len(held) == 2
    assert {lease.generation_key for lease in held} == {config.generation_key for config in configs}
    first, second = held
    await asyncio.gather(
        store.release_period_close(first, worker_id="shared"),
        store.release_period_close(first, worker_id="shared"),
        store.release_period_close(second, worker_id="wrong-worker"),
        store.release_period_close(replace(second, account_id="unavailable"), worker_id="shared"),
    )
    replacement = await store.lease_period_close(worker_id="next", now=NOW, lease_seconds=60)
    assert replacement is not None and replacement.generation_key == first.generation_key
    receipt_operation_1 = await store.lease_period_close(
        worker_id="extra", now=NOW, lease_seconds=60
    )
    assert receipt_operation_1 is None
    await asyncio.gather(
        store.release_period_close(second, worker_id="shared"),
        store.release_period_close(replacement, worker_id="next"),
    )
    again = await asyncio.gather(
        *(store.lease_period_close(worker_id="again", now=NOW, lease_seconds=60) for _ in range(2))
    )
    assert {lease.generation_key for lease in again if lease is not None} == {
        config.generation_key for config in configs
    }


async def test_an_expired_lease_cannot_release_a_replacement_with_the_same_worker_id(
    store: ReportingLedgerStore,
) -> None:
    await store.put_configuration(configuration())
    await store.put_configuration(configuration("acct_b"))
    expired = await store.lease_period_close(worker_id="shared", now=NOW, lease_seconds=1)
    other = await store.lease_period_close(worker_id="shared", now=NOW, lease_seconds=60)
    assert expired is not None and other is not None
    later = NOW + timedelta(seconds=5)
    replacement = await store.lease_period_close(worker_id="shared", now=later, lease_seconds=60)
    assert replacement is not None and replacement.generation_key == expired.generation_key
    await store.release_period_close(expired, worker_id="shared")
    receipt_operation_2 = await store.lease_period_close(
        worker_id="extra", now=later, lease_seconds=60
    )
    assert receipt_operation_2 is None
    await store.release_period_close(replacement, worker_id="shared")
    reclaimed = await store.lease_period_close(worker_id="new", now=later, lease_seconds=60)
    assert reclaimed is not None and reclaimed.generation_key == expired.generation_key
    receipt_operation_3 = await store.lease_period_close(
        worker_id="extra", now=later, lease_seconds=60
    )
    assert receipt_operation_3 is None


async def test_a_worker_that_releases_each_turn_reaches_every_accounts_generation(
    store: ReportingLedgerStore,
) -> None:
    """A single worker loop must not starve the accounts it did not lease first.

    ``ReportingProducer.run_worker`` releases in a ``finally``, so a store that
    always hands back the first leasable generation would close periods for one
    account forever and never reach the others -- invisible until two accounts
    share a ``delivery_config_id``, which is exactly what this change allows.

    Release clears ``lease_expires_at``, so ``ORDER BY lease_expires_at NULLS
    FIRST`` alone is not a total order and the winner is whatever plan order
    happens to apply; CI caught nine consecutive ``acct_a`` leases this way
    while the in-memory store, which breaks the tie on turn, stayed fair. Both
    stores now rank on a persisted turn advanced at acquisition, so reverting
    that ordering fails this test deterministically rather than occasionally.
    """
    accounts = ("acct_a", "acct_b", "acct_c")
    await asyncio.gather(*(store.put_configuration(configuration(name)) for name in accounts))
    worked: list[str] = []
    for _ in range(len(accounts) * 3):
        lease = await store.lease_period_close(worker_id="solo", now=NOW, lease_seconds=60)
        assert lease is not None
        assert lease.generation_key == configuration(lease.account_id).generation_key
        worked.append(lease.account_id)
        await store.release_period_close(lease, worker_id="solo")
    assert set(worked) == set(accounts)
    assert set(worked[: len(accounts)]) == set(accounts)


async def test_lease_turn_advances_on_acquisition_so_a_crashed_worker_cannot_starve_peers(
    store: ReportingLedgerStore,
) -> None:
    """Acquisition, not release, is where a generation loses its place in line.

    ``release_period_close`` only clears the lease, so the turn has to be
    recorded when the lease is taken -- otherwise a worker that crashes
    mid-close keeps the minimum turn forever and every expiry sweep hands the
    same generation back.

    Ordering by expiry with ``NULLS FIRST`` hides that on its own: a never
    worked generation always outranks an expired one, so the turn is only the
    deciding term once two eligible generations share an expiry. This builds
    exactly that state. ``acct_b`` is leased first and ``acct_a`` second, both
    crash unreleased with the same expiry, so the generation that went longest
    without a turn is ``acct_b`` even though ``acct_a`` sorts lower by key. A
    store that advances the turn only on release ranks them equal and reaches
    for ``acct_a``.
    """
    # Created in this order so the later-acquired generation sorts lower by key.
    await store.put_configuration(configuration("acct_b"))
    crashed_first = await store.lease_period_close(worker_id="crash-1", now=NOW, lease_seconds=30)
    assert crashed_first is not None and crashed_first.account_id == "acct_b"
    await store.put_configuration(configuration("acct_a"))
    crashed_second = await store.lease_period_close(worker_id="crash-2", now=NOW, lease_seconds=30)
    assert crashed_second is not None and crashed_second.account_id == "acct_a"
    assert crashed_first.lease_expires_at == crashed_second.lease_expires_at
    # Never worked, so it must outrank both expired generations.
    await store.put_configuration(configuration("acct_c"))
    later = NOW + timedelta(seconds=31)
    swept = []
    for _ in range(3):
        lease = await store.lease_period_close(worker_id="solo", now=later, lease_seconds=30)
        assert lease is not None
        swept.append(lease.account_id)
    assert swept == ["acct_c", "acct_b", "acct_a"]


async def test_lease_order_is_total_and_does_not_depend_on_acceptance_order(
    store: ReportingLedgerStore,
) -> None:
    """Both backends must choose the same generation, not this process's history.

    Never-leased generations all share the minimum turn, so something has to
    break the tie. Ranking on "whichever generation this store accepted first"
    is not reproducible: the SQL store cannot express it, it differs from the
    in-memory store, and it does not survive a restart or a second worker. The
    generation key is part of the rank instead, so these configurations are
    handed out in key order even though they are accepted in the reverse.
    """
    # Accepted in reverse key order, deliberately.
    for name in ("acct_c", "acct_b", "acct_a"):
        await store.put_configuration(configuration(name))
    worked = []
    for _ in range(3):
        lease = await store.lease_period_close(worker_id="solo", now=NOW, lease_seconds=60)
        assert lease is not None
        worked.append(lease.account_id)
        await store.release_period_close(lease, worker_id="solo")
    assert worked == ["acct_a", "acct_b", "acct_c"]


async def test_a_crashed_generation_is_not_starved_by_a_peer_that_keeps_releasing(
    store: ReportingLedgerStore,
) -> None:
    """A permanently unheld peer must not outrank a lower-turn expired generation.

    The acquisition filter already drops every live lease, so among the
    survivors the expiry carries no fairness information. If unheld sorted
    ahead of expired, a peer that is leased and released on every turn would be
    NULL forever and win every comparison, while the generation whose worker
    died would stay expired and never close another period -- starvation with
    no expiry sweep able to clear it.
    """
    await asyncio.gather(
        *(store.put_configuration(configuration(name)) for name in ("acct_a", "acct_b"))
    )
    crashed = await store.lease_period_close(worker_id="crashes", now=NOW, lease_seconds=30)
    assert crashed is not None and crashed.account_id == "acct_a"
    # acct_a is still live here, so this can only take acct_b; it releases.
    released = await store.lease_period_close(worker_id="polite", now=NOW, lease_seconds=30)
    assert released is not None and released.account_id == "acct_b"
    await store.release_period_close(released, worker_id="polite")
    # Past acct_a's expiry both are leasable: acct_b unheld, acct_a expired.
    later = NOW + timedelta(seconds=31)
    recovered = await store.lease_period_close(worker_id="solo", now=later, lease_seconds=30)
    assert recovered is not None
    assert recovered.account_id == "acct_a"


async def test_lease_fairness_migrates_onto_an_already_installed_older_schema() -> None:
    """The additive upgrade reaches an existing install and stays idempotent.

    The rank lives in a private `adcp_`-prefixed table precisely so that
    `schema_objects()` -- which enumerates every `reporting_*` table in the
    schema -- keeps reporting the exact object set that older binaries
    validate. So the upgrade has to be proven on an install that predates it.
    """
    async with isolated_reporting_pool() as pool:
        store = PgReportingLedgerStore(pool=pool, clock=lambda: NOW)
        await store.create_schema()
        async with pool.connection() as connection:
            # Reduce the install to the pre-fairness shape an older binary left.
            await connection.execute(
                "DROP TABLE IF EXISTS adcp_reporting_configuration_lease_turns"
            )
            await connection.execute(
                "DROP SEQUENCE IF EXISTS adcp_reporting_configuration_lease_turn_seq"
            )
        for name in ("acct_a", "acct_b"):
            await store.put_configuration(configuration(name))
        # Repeated migration is safe and restores the durable fairness rank.
        await store.create_schema()
        await store.create_schema()
        async with pool.connection() as connection:
            ranks = await (
                await connection.execute(
                    "SELECT count(*) FROM adcp_reporting_configuration_lease_turns"
                )
            ).fetchone()
        # Generations accepted before the upgrade have no rank row at all, which
        # is the never-leased rank rather than a privileged one.
        assert ranks is not None and ranks[0] == 0
        worked = []
        for _ in range(2):
            lease = await store.lease_period_close(worker_id="solo", now=NOW, lease_seconds=60)
            assert lease is not None
            worked.append(lease.account_id)
            await store.release_period_close(lease, worker_id="solo")
        assert worked == ["acct_a", "acct_b"]
        # A generation accepted by an older writer that knows nothing about the
        # private table still ranks as never leased, so it is served before the
        # generations that already took a turn rather than starved behind them.
        async with pool.connection() as connection:
            await connection.execute(
                "INSERT INTO reporting_configurations"
                " (delivery_config_id, delivery_config_version, account_id,"
                "  report_definition_id, reporting_profile, feed_purpose, required_finality,"
                "  account_timezone, schedule, media_buy_ids, activated_at,"
                "  automated_recovery_seconds, status_retention_days, content_sha256)"
                " SELECT delivery_config_id, delivery_config_version, 'acct_legacy',"
                "  report_definition_id, reporting_profile, feed_purpose, required_finality,"
                "  account_timezone, schedule, media_buy_ids, activated_at,"
                "  automated_recovery_seconds, status_retention_days, 'f' || content_sha256"
                " FROM reporting_configurations WHERE account_id = %s",
                ("acct_a",),
            )
        legacy = await store.lease_period_close(worker_id="solo", now=NOW, lease_seconds=60)
        assert legacy is not None
        assert legacy.account_id == "acct_legacy"


async def test_lease_fairness_adds_no_enumerated_reporting_catalog_object() -> None:
    """Exact `reporting_*` object identity is the A/B+C compatibility contract.

    `schema_objects()` enumerates every current-schema table whose name starts
    with `reporting_`, plus that table's columns, constraints, indexes and
    triggers, and the status suites compare the installed set to their
    manifests exhaustively. The fairness rank must therefore add nothing to
    that set -- not a column on `reporting_configurations`, and not a new
    `reporting_*` table either.
    """
    async with isolated_reporting_pool() as pool:
        store = PgReportingLedgerStore(pool=pool, clock=lambda: NOW)
        await store.create_schema()
        async with pool.connection() as connection:
            installed = await schema_objects(connection)
            await connection.execute(
                "DROP TABLE IF EXISTS adcp_reporting_configuration_lease_turns"
            )
            await connection.execute(
                "DROP SEQUENCE IF EXISTS adcp_reporting_configuration_lease_turn_seq"
            )
            without = await schema_objects(connection)
        assert installed == without
        assert not [k for k in installed if "lease_turn" in k or "fairness" in k]
        # And the private objects really are the ones carrying the rank.
        await store.create_schema()
        for name in ("acct_a", "acct_b"):
            await store.put_configuration(configuration(name))
        lease = await store.lease_period_close(worker_id="solo", now=NOW, lease_seconds=60)
        assert lease is not None
        async with pool.connection() as connection:
            assert await schema_objects(connection) == installed
            rows = await (
                await connection.execute(
                    "SELECT account_id, lease_turn" " FROM adcp_reporting_configuration_lease_turns"
                )
            ).fetchall()
        assert [(r[0], r[1] > 0) for r in rows] == [("acct_a", True)]


async def test_lease_and_its_fairness_rank_commit_or_roll_back_together() -> None:
    """The rank lives in another table, so it must share the lease transaction.

    If the two statements could commit separately, a crash between them would
    either hand out a lease whose generation never lost its place in line, or
    advance the rank for a lease nobody holds. The rank update is forced to fail
    here; the acquisition must roll back with it.

    The pool is deliberately autocommit: that is the mode in which an implicit
    per-block transaction does not exist, so it is the only mode that can
    witness the explicit transaction actually doing the work.
    """
    async with isolated_reporting_pool(autocommit=True) as pool:
        store = PgReportingLedgerStore(pool=pool, clock=lambda: NOW)
        await store.create_schema()
        await store.put_configuration(configuration("acct_a"))
        async with pool.connection() as connection:
            await connection.execute(
                "ALTER TABLE adcp_reporting_configuration_lease_turns"
                " ADD CONSTRAINT reject_rank CHECK (lease_turn < 0)"
            )
        with pytest.raises(Exception):  # noqa: B017,PT011 - driver integrity error
            await store.lease_period_close(worker_id="solo", now=NOW, lease_seconds=60)
        async with pool.connection() as connection:
            held = await (
                await connection.execute(
                    "SELECT count(*) FROM reporting_configurations"
                    " WHERE lease_worker_id IS NOT NULL OR lease_expires_at IS NOT NULL"
                )
            ).fetchone()
            ranked = await (
                await connection.execute(
                    "SELECT count(*) FROM adcp_reporting_configuration_lease_turns"
                )
            ).fetchone()
        # Neither half survived.
        assert held is not None and held[0] == 0
        assert ranked is not None and ranked[0] == 0
        # With the rank writable again the generation is still leasable.
        async with pool.connection() as connection:
            await connection.execute(
                "ALTER TABLE adcp_reporting_configuration_lease_turns DROP CONSTRAINT reject_rank"
            )
        lease = await store.lease_period_close(worker_id="solo", now=NOW, lease_seconds=60)
        assert lease is not None and lease.account_id == "acct_a"


async def test_a_stale_fairness_rank_row_cannot_affect_another_generation() -> None:
    """An orphan rank row is inert, and a re-put generation keeps its own rank.

    The rank table carries no foreign key, so a generation removed by an
    operator can leave a row behind. The lazy join only matches on the exact
    generation key, so such a row is never consulted for anyone else, and
    ``put_configuration`` is immutable by key, so re-accepting the same
    generation is the same generation and legitimately keeps its place in line.
    """
    async with isolated_reporting_pool() as pool:
        store = PgReportingLedgerStore(pool=pool, clock=lambda: NOW)
        await store.create_schema()
        for name in ("acct_a", "acct_b"):
            await store.put_configuration(configuration(name))
        async with pool.connection() as connection:
            await connection.execute(
                "INSERT INTO adcp_reporting_configuration_lease_turns"
                " (account_id, delivery_config_id, delivery_config_version, lease_turn)"
                " VALUES ('acct_vanished', 'daily', 1, 999999)"
            )
        worked = []
        for _ in range(2):
            lease = await store.lease_period_close(worker_id="solo", now=NOW, lease_seconds=60)
            assert lease is not None
            worked.append(lease.account_id)
            await store.release_period_close(lease, worker_id="solo")
        assert worked == ["acct_a", "acct_b"]
        # Re-accepting acct_a's exact generation does not reset its rank.
        await store.put_configuration(configuration("acct_a"))
        again = await store.lease_period_close(worker_id="solo", now=NOW, lease_seconds=60)
        assert again is not None and again.account_id == "acct_a"


async def test_concurrent_period_closes_converge_within_each_account(
    store: ReportingLedgerStore,
) -> None:
    configs = (configuration(), configuration("acct_b"))
    await asyncio.gather(*(store.put_configuration(config) for config in configs))
    producer = ReportingProducer(
        source=UncalledSource(), offerings=ProducerOfferings(), store=store
    )
    await asyncio.gather(
        *(producer.close_elapsed_periods(config, now=NOW) for config in configs for _ in range(4))
    )
    ids = set()
    for config in configs:
        found = await store.find_obligation(
            account_id=config.account_id,
            delivery_config_id="daily",
            delivery_config_version=1,
            period_start=START,
            period_end=END,
        )
        assert found is not None
        assert found.generation_key == config.generation_key
        assert found.media_buy_ids == config.media_buy_ids
        assert found.definition == config.definition
        ids.add(found.reporting_obligation_id)
        snapshot = await store.open_snapshot(account_id=config.account_id, filters_fingerprint="")
        page = await store.read_page(
            snapshot=snapshot,
            consumer_id=None,
            delivery_config_ids=["daily"],
            media_buy_ids=None,
            offset=0,
            limit=10,
            changes_after_sequence=None,
        )
        assert page.total_count == 1
        assert page.obligations == (found,)
        assert page.revisions == ()
    assert len(ids) == 2


async def test_an_obligation_id_cannot_overwrite_another_accounts_period(
    store: ReportingLedgerStore,
) -> None:
    first, second = configuration(), configuration("acct_b")
    await asyncio.gather(store.put_configuration(first), store.put_configuration(second))
    original = await store.commit_obligation(obligation_for(first))
    with pytest.raises(LedgerConflictError) as caught:
        await store.commit_obligation(
            replace(
                obligation_for(second), reporting_obligation_id=original.reporting_obligation_id
            )
        )
    assert caught.value.code == "OBLIGATION_IDENTITY_CONFLICT"
    assert (
        await store.find_obligation(
            account_id=first.account_id,
            delivery_config_id="daily",
            delivery_config_version=1,
            period_start=START,
            period_end=END,
        )
        == original
    )
    assert (
        await store.find_obligation(
            account_id=second.account_id,
            delivery_config_id="daily",
            delivery_config_version=1,
            period_start=START,
            period_end=END,
        )
        is None
    )


async def test_concurrent_workers_use_their_leased_generation(
    store: ReportingLedgerStore,
) -> None:
    configs = (configuration(), configuration("acct_b"))
    await asyncio.gather(*(store.put_configuration(config) for config in configs))
    entered = {config.account_id: asyncio.Event() for config in configs}
    finish = {config.account_id: asyncio.Event() for config in configs}
    calls: list[ReportingSourceSliceRequestV1] = []

    async def fetch(request: ReportingSourceSliceRequestV1) -> None:
        calls.append(request)
        entered[request.identity.account_id].set()
        await finish[request.identity.account_id].wait()

    source = InlineReportingSource(
        capabilities=redacted_capabilities(), fetch=fetch, clock=lambda: NOW
    )
    producer = ReportingProducer(
        source=source,
        offerings=ProducerOfferings(
            snapshot_offering_id="FIXTURE_PULSE_V1",
            requested_dimensions=("campaign_id",),
            source_scope=dict(source.capabilities.source_scope),
        ),
        store=store,
        clock=lambda: NOW,
    )
    tasks = [asyncio.create_task(producer.run_worker()) for _ in range(2)]
    try:
        await asyncio.wait_for(asyncio.gather(*(event.wait() for event in entered.values())), 10)
        finish["acct_a"].set()
        done, pending = await asyncio.wait(tasks, timeout=10, return_when=asyncio.FIRST_COMPLETED)
        assert len(done) == len(pending) == 1
        finished = next(iter(done)).result()
        assert finished.leased is not None and finished.leased.account_id == "acct_a"
        probe = await store.lease_period_close(worker_id="probe", now=NOW, lease_seconds=60)
        assert probe is not None and probe.generation_key == configs[0].generation_key
        receipt_operation_4 = await store.lease_period_close(
            worker_id="extra", now=NOW, lease_seconds=60
        )
        assert receipt_operation_4 is None
        await store.release_period_close(probe, worker_id="probe")
        finish["acct_b"].set()
        turns = await asyncio.wait_for(asyncio.gather(*tasks), 10)
        assert {turn.leased.generation_key for turn in turns if turn.leased} == {
            config.generation_key for config in configs
        }
        for turn in turns:
            assert len(turn.obligations_committed) == 1
            assert turn.slices_failed == turn.obligations_committed
        assert len({call.identity.reporting_obligation_id for call in calls}) == 2
        for call in calls:
            expected = next(
                config for config in configs if config.account_id == call.identity.account_id
            )
            assert call.identity.delivery_config_id == expected.delivery_config_id
            assert call.identity.delivery_config_version == expected.delivery_config_version
            assert [item.constituent_id for item in call.coverage.constituents] == list(
                expected.media_buy_ids
            )
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


async def test_source_acquisition_rejects_a_different_account_or_generation(
    store: ReportingLedgerStore,
) -> None:
    config = configuration()
    obligation = obligation_for(config)
    producer = ReportingProducer(
        source=UncalledSource(), offerings=ProducerOfferings(), store=store
    )
    for foreign in (configuration("acct_b"), replace(config, delivery_config_version=2)):
        with pytest.raises(LedgerConflictError) as caught:
            await producer.acquire_obligation(foreign, obligation)
        assert caught.value.code == "CONFIGURATION_GENERATION_MISMATCH"


@pytest.mark.parametrize("names_foreign_obligation", [False, True])
async def test_missing_obligation_statements_attach_only_within_their_account(
    store: ReportingLedgerStore,
    names_foreign_obligation: bool,
) -> None:
    first, second = configuration(), configuration("acct_b")
    await asyncio.gather(store.put_configuration(first), store.put_configuration(second))
    foreign = await store.commit_obligation(obligation_for(second))
    statement = ConsumerStatusRecord(
        reporting_status_id="rps_missing_a",
        account_id=first.account_id,
        consumer_id="shared-buyer",
        delivery_config_id="daily",
        delivery_config_version=1,
        report_definition_id=first.report_definition_id,
        period_start=START,
        period_end=END,
        period_source_timezone="UTC",
        consumer_status="obligation_missing",
        status_as_of=NOW,
        recorded_at=NOW,
        reporting_obligation_id=(
            foreign.reporting_obligation_id if names_foreign_obligation else None
        ),
    )
    assert statement.generation_key == first.generation_key
    await store.record_consumer_status(statement)
    assert (
        await store.list_consumer_statuses(
            account_id=first.account_id,
            consumer_id=statement.consumer_id,
            reporting_obligation_ids=[foreign.reporting_obligation_id],
        )
        == ()
    )
    own = await store.commit_obligation(obligation_for(first))
    assert await store.list_consumer_statuses(
        account_id=first.account_id,
        consumer_id=statement.consumer_id,
        reporting_obligation_ids=[own.reporting_obligation_id],
    ) == (statement,)
    assert (
        await store.list_consumer_statuses(
            account_id=second.account_id, consumer_id=statement.consumer_id
        )
        == ()
    )


async def test_status_and_issue_projection_use_each_accounts_own_generation(
    store: ReportingLedgerStore,
) -> None:
    first, second = configuration(), configuration("acct_b")
    first = replace(first, schedule=replace(first.schedule, delivery_sla="PT30M"))
    second = replace(second, schedule=replace(second.schedule, delivery_sla="PT4H"))
    await asyncio.gather(store.put_configuration(first), store.put_configuration(second))
    for config in (first, second):
        obligation = await store.commit_obligation(obligation_for(config))
        original, rows = revision_for(obligation)
        await store.commit_revision(original, rows)
        await store.record_consumer_status(
            ConsumerStatusRecord(
                reporting_status_id=f"rps_{config.account_id}",
                account_id=config.account_id,
                consumer_id="shared-buyer",
                delivery_config_id="daily",
                delivery_config_version=1,
                report_definition_id=config.report_definition_id,
                period_start=START,
                period_end=END,
                period_source_timezone="UTC",
                consumer_status="received",
                status_as_of=END,
                recorded_at=END,
                reporting_obligation_id=obligation.reporting_obligation_id,
                reporting_revision_id=original.reporting_revision_id,
                observed_revision_content_sha256=original.revision_content_sha256,
            )
        )
        restatement, rows = revision_for(obligation, suffix="restated")
        await store.commit_revision(
            replace(
                restatement,
                supersedes_reporting_revision_id=original.reporting_revision_id,
                created_at=END + timedelta(minutes=10),
            ),
            rows,
        )

    handler = ReportingStatusHandler(store, consumer_status_enabled=True)
    callers = [
        ReportingStatusCaller(config.account_id, "shared-buyer") for config in (first, second)
    ]
    summaries = await asyncio.gather(
        *(handler.handle({"view": "summary"}, caller=caller) for caller in callers)
    )
    assert [summary["health"] for summary in summaries] == ["action_required", "delayed"]
    assert summaries[0]["issues"][0]["issue_id"] != summaries[1]["issues"][0]["issue_id"]
    for config, caller, summary in zip((first, second), callers, summaries):
        assert summary["account_id"] == config.account_id
        assert summary["coverage"]["media_buy_ids"] == list(config.media_buy_ids)
        assert summary["obligation_counts"]["total"] == 1
        periods = await handler.handle(
            {"view": "periods", "delivery_config_ids": ["daily"]}, caller=caller
        )
        assert periods["pagination"]["total_count"] == 4
        assert periods["periods"][0]["health"] == summary["health"]
        assert periods["periods"][0]["account_id"] == config.account_id
        assert {revision["account_id"] for revision in periods["revisions"]} == {config.account_id}
        assert [status["reporting_status_id"] for status in periods["consumer_statuses"]] == [
            f"rps_{config.account_id}"
        ]
        assert config.definition is not None
        for revision in periods["revisions"]:
            assert revision["report_definition_uri"] == config.definition.report_definition_uri
        own_revision_id = f"rpr_{config.account_id}_restated"
        exact = await handler.handle(
            {"view": "revision", "reporting_revision_id": own_revision_id}, caller=caller
        )
        assert exact["revision"]["media_buy_ids"] == list(config.media_buy_ids)
        assert (
            await store.get_revision(
                account_id="unavailable", reporting_revision_id=own_revision_id
            )
            is None
        )

    first_key = consumer_mismatch_issue_key(
        account_id=first.account_id,
        consumer_id="shared-buyer",
        delivery_config_id="daily",
        delivery_config_version=1,
        report_definition_id=first.report_definition_id,
        period_start=START,
        period_end=END,
    )
    assert await store.get_issue(issue_key=first_key, account_id=second.account_id) is None
    await store.set_issue_state(
        issue_key=first_key,
        account_id=first.account_id,
        state="waived",
        at=NOW,
        external_ref="private-case-a",
    )
    after = await asyncio.gather(
        *(handler.handle({"view": "summary"}, caller=caller) for caller in callers)
    )
    assert after[0]["issues"] == []
    assert after[1]["issues"] == summaries[1]["issues"]
    for unavailable_id in ("rpr_acct_b_restated", "nonexistent"):
        with pytest.raises(LedgerConflictError) as caught:
            await handler.handle(
                {"view": "revision", "reporting_revision_id": unavailable_id}, caller=callers[0]
            )
        assert caught.value.code == "LOOKUP_UNAVAILABLE"


async def test_ingest_resolves_the_accounts_own_configuration(
    store: ReportingLedgerStore,
) -> None:
    configs = (
        configuration(),
        replace(configuration("acct_b"), report_definition_id="other_definition"),
    )
    await asyncio.gather(*(store.put_configuration(config) for config in configs))
    ingest = ConsumerStatusIngest(store, enabled=True, clock=lambda: NOW)
    for config in configs:
        result = await ingest.handle(
            {
                "statuses": [
                    {
                        "reporting_status_id": f"rps_{config.account_id}_missing",
                        "delivery_config_id": "daily",
                        "delivery_config_version": 1,
                        "report_definition_id": config.report_definition_id,
                        "period": {
                            "start": START.isoformat(),
                            "end": END.isoformat(),
                            "source_timezone": "UTC",
                        },
                        "consumer_status": "obligation_missing",
                        "status_as_of": NOW.isoformat(),
                    }
                ]
            },
            account_id=config.account_id,
            consumer_id="shared-buyer",
        )
        assert result["results"][0]["result"] == "recorded"
