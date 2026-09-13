"""Conformance tests for :class:`PgReportingLedgerStore` against real Postgres.

The in-memory store and the Postgres store must agree, because an adopter who
develops against the first and deploys against the second will discover any
divergence in production, on retained evidence, at the worst possible moment.
So these tests assert the *same* invariants
``tests/test_reporting_ledger.py`` asserts in memory -- plus the ones that only
exist at the SQL layer: partial unique indexes, concurrent period closes, and
the ordered change feed.

Requires a real PostgreSQL instance::

    docker run --rm -d -p 5432:5432 -e POSTGRES_PASSWORD=pg postgres:16
    export ADCP_PG_TEST_URL=postgresql://postgres:pg@localhost:5432/postgres
    pytest tests/conformance/reporting -v

The module skips when ``ADCP_PG_TEST_URL`` is unset, so the default matrix
stays green without a database dependency.  Each test runs against a freshly
created schema so parallel runs cannot collide.
"""

from __future__ import annotations

import asyncio
import os
import secrets
from collections.abc import AsyncIterator
from datetime import datetime, timedelta, timezone

import pytest

psycopg = pytest.importorskip("psycopg")
psycopg_pool = pytest.importorskip("psycopg_pool")

TEST_URL = os.environ.get("ADCP_PG_TEST_URL")
if not TEST_URL:
    pytest.skip(
        "ADCP_PG_TEST_URL not set — skipping reporting ledger conformance tests",
        allow_module_level=True,
    )

from adcp.reporting.ledger import (  # noqa: E402
    ConsumerStatusIngest,
    LedgerConflictError,
    ReportingAdjustmentRecord,
    ReportingConfiguration,
    ReportingObligationRecord,
    ReportingRevisionRecord,
    ReportingScheduleSpec,
    ReportingStatusCaller,
    ReportingStatusHandler,
    derive_period,
    revision_content_sha256,
)
from adcp.reporting.ledger.pg import PgReportingLedgerStore  # noqa: E402

ACCOUNT = "acct_pg"
CALLER = ReportingStatusCaller(account_id=ACCOUNT, consumer_id="buyer_pg")
HOURLY = ReportingScheduleSpec(period_duration="PT1H", delivery_sla="PT1H", alignment="utc")

_TABLES = (
    "reporting_ledger_changes",
    "reporting_consumer_statuses",
    "reporting_adjustments",
    "reporting_revision_rows",
    "reporting_revisions",
    "reporting_obligations",
    "reporting_configurations",
)


@pytest.fixture()
async def store() -> AsyncIterator[PgReportingLedgerStore]:
    """A ledger in its own schema, dropped on exit."""
    schema = f"adcp_rpt_{secrets.token_hex(6)}"
    async with psycopg_pool.AsyncConnectionPool(
        TEST_URL, min_size=2, max_size=8, open=False
    ) as pool:
        await pool.open()
        async with pool.connection() as connection:
            await connection.execute(f"CREATE SCHEMA {schema}")
        await pool.close()

    async with psycopg_pool.AsyncConnectionPool(
        f"{TEST_URL}?options=-csearch_path%3D{schema}", min_size=2, max_size=8, open=False
    ) as scoped:
        await scoped.open()
        ledger = PgReportingLedgerStore(pool=scoped)
        await ledger.create_schema()
        try:
            yield ledger
        finally:
            async with scoped.connection() as connection:
                for table in _TABLES:
                    await connection.execute(f"DROP TABLE IF EXISTS {table} CASCADE")
    async with psycopg_pool.AsyncConnectionPool(
        TEST_URL, min_size=1, max_size=2, open=False
    ) as cleanup:
        await cleanup.open()
        async with cleanup.connection() as connection:
            await connection.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")


def _configuration(**overrides) -> ReportingConfiguration:
    defaults = dict(
        delivery_config_id="daily_reporting",
        delivery_config_version=1,
        account_id=ACCOUNT,
        report_definition_id="daily_delivery_v1",
        reporting_profile="paid_media_delivery",
        feed_purpose="analytics",
        schedule=HOURLY,
        required_finality="official",
        activated_at=datetime(2026, 9, 1, 0, 20, tzinfo=timezone.utc),
        media_buy_ids=("mb_1",),
    )
    defaults.update(overrides)
    return ReportingConfiguration(**defaults)  # type: ignore[arg-type]


def _boundary(ordinal: int = 0):
    from adcp.reporting.ledger.models import first_ordinal_after

    base = first_ordinal_after(
        HOURLY,
        account_timezone="UTC",
        activated_at=datetime(2026, 9, 1, 0, 20, tzinfo=timezone.utc),
    )
    return derive_period(HOURLY, account_timezone="UTC", ordinal=base + ordinal)


def _obligation(configuration: ReportingConfiguration, ordinal: int = 0, **overrides):
    boundary = _boundary(ordinal)
    defaults = dict(
        reporting_obligation_id=f"rpo_pg_{ordinal}",
        account_id=configuration.account_id,
        delivery_config_id=configuration.delivery_config_id,
        delivery_config_version=configuration.delivery_config_version,
        report_definition_id=configuration.report_definition_id,
        reporting_profile=configuration.reporting_profile,
        feed_purpose=configuration.feed_purpose,
        period=boundary,
        scope_resolved_at=boundary.end,
        media_buy_ids=configuration.media_buy_ids,
        required_finality=configuration.required_finality,
        automated_recovery_deadline_at=boundary.expected_at + timedelta(hours=6),
        schedule=configuration.schedule,
        created_at=boundary.end,
    )
    defaults.update(overrides)
    return ReportingObligationRecord(**defaults)  # type: ignore[arg-type]


def _revision(obligation, *, revision_id="rpr_pg_1", finality="official", rows=None, **overrides):
    rows = [{"media_buy_id": "mb_1", "impressions": 5}] if rows is None else rows
    totals = (("impressions", str(sum(int(r.get("impressions", 0)) for r in rows))),)
    defaults = dict(
        reporting_revision_id=revision_id,
        account_id=obligation.account_id,
        reporting_obligation_id=obligation.reporting_obligation_id,
        finality=finality,
        revision_content_sha256=revision_content_sha256(
            reporting_revision_id=revision_id,
            row_count=len(rows),
            control_totals=totals,
            reporting_rows=rows,
        ),
        row_count=len(rows),
        control_totals=totals,
        observed_at=obligation.period.end,
        data_through=obligation.period.end,
        created_at=obligation.period.expected_at,
    )
    if finality == "official":
        defaults.update(
            finality_basis="source_final",
            finality_policy_id="policy_1",
            finalized_at=obligation.period.end,
        )
    defaults.update(overrides)
    return ReportingRevisionRecord(**defaults), rows  # type: ignore[arg-type]


# -- schema and round trips -------------------------------------------------


async def test_create_schema_is_idempotent(store: PgReportingLedgerStore) -> None:
    await store.create_schema()
    await store.create_schema()


async def test_configuration_round_trips(store: PgReportingLedgerStore) -> None:
    configuration = _configuration()
    await store.put_configuration(configuration)
    loaded = await store.list_configurations(account_id=ACCOUNT)
    assert len(loaded) == 1
    assert loaded[0].schedule.period_duration == "PT1H"
    assert loaded[0].media_buy_ids == ("mb_1",)
    assert loaded[0].activated_at == configuration.activated_at


async def test_a_configuration_generation_is_immutable(store: PgReportingLedgerStore) -> None:
    await store.put_configuration(_configuration())
    await store.put_configuration(_configuration())  # exact replay is fine
    with pytest.raises(LedgerConflictError, match="publish a new version"):
        await store.put_configuration(_configuration(reporting_profile="changed"))


async def test_obligation_round_trips_with_its_period(store: PgReportingLedgerStore) -> None:
    await store.put_configuration(_configuration())
    obligation = _obligation(_configuration())
    stored = await store.commit_obligation(obligation)
    loaded = await store.get_obligation(
        account_id=ACCOUNT, reporting_obligation_id=stored.reporting_obligation_id
    )
    assert loaded is not None
    assert loaded.period.start == obligation.period.start
    assert loaded.period.expected_at == obligation.period.expected_at
    assert loaded.scope_resolved_at == obligation.period.end


async def test_obligations_are_account_scoped(store: PgReportingLedgerStore) -> None:
    await store.put_configuration(_configuration())
    obligation = await store.commit_obligation(_obligation(_configuration()))
    assert (
        await store.get_obligation(
            account_id="someone-else",
            reporting_obligation_id=obligation.reporting_obligation_id,
        )
        is None
    )


# -- SQL-level invariants ---------------------------------------------------


async def test_concurrent_period_closes_converge_on_one_obligation(
    store: PgReportingLedgerStore,
) -> None:
    # Two workers racing a period close must not produce two obligations, or a
    # seller could publish twice and pick a winner.
    await store.put_configuration(_configuration())
    configuration = _configuration()
    racers = [
        store.commit_obligation(
            _obligation(configuration, reporting_obligation_id=f"rpo_race_{index}")
        )
        for index in range(4)
    ]
    results = await asyncio.gather(*racers)
    assert len({item.reporting_obligation_id for item in results}) == 1


async def test_one_official_revision_per_obligation(store: PgReportingLedgerStore) -> None:
    await store.put_configuration(_configuration())
    obligation = await store.commit_obligation(_obligation(_configuration()))
    first, rows = _revision(obligation, revision_id="rpr_a")
    await store.commit_revision(first, rows)
    second, rows2 = _revision(obligation, revision_id="rpr_b")
    with pytest.raises(LedgerConflictError) as error:
        await store.commit_revision(second, rows2)
    assert error.value.code == "OFFICIAL_REVISION_TERMINAL"


async def test_concurrent_official_commits_leave_exactly_one(
    store: PgReportingLedgerStore,
) -> None:
    # The partial unique index, not the Python check, is what holds here.
    await store.put_configuration(_configuration())
    obligation = await store.commit_obligation(_obligation(_configuration()))
    attempts = [
        store.commit_revision(*_revision(obligation, revision_id=f"rpr_race_{index}"))
        for index in range(4)
    ]
    outcomes = await asyncio.gather(*attempts, return_exceptions=True)
    succeeded = [item for item in outcomes if not isinstance(item, BaseException)]
    assert len(succeeded) == 1
    assert all(
        isinstance(item, LedgerConflictError)
        for item in outcomes
        if isinstance(item, BaseException)
    )


async def test_recommitting_a_revision_with_changed_content_conflicts(
    store: PgReportingLedgerStore,
) -> None:
    await store.put_configuration(_configuration())
    obligation = await store.commit_obligation(_obligation(_configuration()))
    revision, rows = _revision(obligation)
    await store.commit_revision(revision, rows)
    replayed = await store.commit_revision(revision, rows)
    assert replayed.reporting_revision_id == revision.reporting_revision_id

    changed, changed_rows = _revision(
        obligation, rows=[{"media_buy_id": "mb_1", "impressions": 99}]
    )
    with pytest.raises(LedgerConflictError, match="different content"):
        await store.commit_revision(changed, changed_rows)


async def test_supersession_cannot_fork_the_chain(store: PgReportingLedgerStore) -> None:
    configuration = _configuration(required_finality="snapshot")
    await store.put_configuration(configuration)
    obligation = await store.commit_obligation(_obligation(configuration))
    first, rows = _revision(obligation, revision_id="rpr_s1", finality="snapshot")
    await store.commit_revision(first, rows)
    second, rows2 = _revision(
        obligation,
        revision_id="rpr_s2",
        finality="snapshot",
        supersedes_reporting_revision_id="rpr_s1",
    )
    await store.commit_revision(second, rows2)
    third, rows3 = _revision(
        obligation,
        revision_id="rpr_s3",
        finality="snapshot",
        supersedes_reporting_revision_id="rpr_s1",
    )
    with pytest.raises(LedgerConflictError) as error:
        await store.commit_revision(third, rows3)
    assert error.value.code == "SUPERSEDES_STALE"


async def test_an_adjustment_requires_an_official_predecessor(
    store: PgReportingLedgerStore,
) -> None:
    configuration = _configuration(required_finality="snapshot")
    await store.put_configuration(configuration)
    obligation = await store.commit_obligation(_obligation(configuration))
    snapshot, rows = _revision(obligation, revision_id="rpr_snap", finality="snapshot")
    await store.commit_revision(snapshot, rows)
    with pytest.raises(LedgerConflictError) as error:
        await store.commit_adjustment(
            ReportingAdjustmentRecord(
                reporting_adjustment_id="rpa_1",
                account_id=ACCOUNT,
                adjusts_reporting_revision_id="rpr_snap",
                reason_code="source_correction",
                accounting_period_start=obligation.period.start,
                accounting_period_end=obligation.period.end,
                control_total_deltas=(("impressions", "-1"),),
                correction_observed_at=obligation.period.end,
                created_at=obligation.period.end,
            )
        )
    assert error.value.code == "ADJUSTMENT_REQUIRES_OFFICIAL"


# -- rows and the binding ---------------------------------------------------


async def test_an_exact_read_reproduces_the_revision_binding(
    store: PgReportingLedgerStore,
) -> None:
    await store.put_configuration(_configuration())
    obligation = await store.commit_obligation(_obligation(_configuration()))
    rows = [{"media_buy_id": "mb_1", "day": index, "impressions": index} for index in range(7)]
    revision, _ = _revision(obligation, rows=rows)
    await store.commit_revision(revision, rows)

    walked: list[dict[str, object]] = []
    cursor = None
    while True:
        page = await store.read_revision_rows(
            account_id=ACCOUNT,
            reporting_revision_id=revision.reporting_revision_id,
            cursor=cursor,
            limit=3,
        )
        assert page.total_count == 7
        walked.extend(page.rows)
        if not page.has_more:
            break
        cursor = page.cursor
    assert walked == rows
    assert (
        revision_content_sha256(
            reporting_revision_id=revision.reporting_revision_id,
            row_count=len(walked),
            control_totals=revision.control_totals,
            reporting_rows=walked,
        )
        == revision.revision_content_sha256
    )


async def test_a_zero_row_revision_commits_and_reads_as_zero(
    store: PgReportingLedgerStore,
) -> None:
    await store.put_configuration(_configuration())
    obligation = await store.commit_obligation(_obligation(_configuration()))
    revision, rows = _revision(obligation, rows=[])
    await store.commit_revision(revision, rows)
    page = await store.read_revision_rows(
        account_id=ACCOUNT, reporting_revision_id=revision.reporting_revision_id
    )
    assert page.total_count == 0
    assert page.rows == ()


async def test_row_reads_are_account_scoped(store: PgReportingLedgerStore) -> None:
    await store.put_configuration(_configuration())
    obligation = await store.commit_obligation(_obligation(_configuration()))
    revision, rows = _revision(obligation)
    await store.commit_revision(revision, rows)
    with pytest.raises(LedgerConflictError, match="no such revision"):
        await store.read_revision_rows(
            account_id="someone-else", reporting_revision_id=revision.reporting_revision_id
        )


# -- the change feed --------------------------------------------------------


async def test_the_change_feed_orders_every_record_kind(store: PgReportingLedgerStore) -> None:
    await store.put_configuration(_configuration())
    obligation = await store.commit_obligation(_obligation(_configuration()))
    revision, rows = _revision(obligation)
    await store.commit_revision(revision, rows)
    await store.commit_adjustment(
        ReportingAdjustmentRecord(
            reporting_adjustment_id="rpa_feed",
            account_id=ACCOUNT,
            adjusts_reporting_revision_id=revision.reporting_revision_id,
            reason_code="late_attribution",
            accounting_period_start=obligation.period.start,
            accounting_period_end=obligation.period.end,
            control_total_deltas=(("impressions", "1"),),
            correction_observed_at=obligation.period.end,
            created_at=obligation.period.end,
        )
    )
    snapshot = await store.open_snapshot(account_id=ACCOUNT, filters_fingerprint="x")
    page = await store.read_page(
        snapshot=snapshot,
        consumer_id=None,
        delivery_config_ids=None,
        media_buy_ids=None,
        offset=0,
        limit=50,
        changes_after_sequence=None,
    )
    assert page.total_count == 3
    assert len(page.obligations) == len(page.revisions) == len(page.adjustments) == 1


async def test_changes_after_returns_only_later_records(store: PgReportingLedgerStore) -> None:
    await store.put_configuration(_configuration())
    obligation = await store.commit_obligation(_obligation(_configuration()))
    first = await store.open_snapshot(account_id=ACCOUNT, filters_fingerprint="x")

    revision, rows = _revision(obligation)
    await store.commit_revision(revision, rows)
    later = await store.open_snapshot(account_id=ACCOUNT, filters_fingerprint="x")
    page = await store.read_page(
        snapshot=later,
        consumer_id=None,
        delivery_config_ids=None,
        media_buy_ids=None,
        offset=0,
        limit=50,
        changes_after_sequence=first.max_sequence,
    )
    assert [item.reporting_revision_id for item in page.revisions] == [
        revision.reporting_revision_id
    ]
    assert page.obligations == ()


async def test_the_feed_is_account_isolated(store: PgReportingLedgerStore) -> None:
    await store.put_configuration(_configuration())
    await store.commit_obligation(_obligation(_configuration()))
    snapshot = await store.open_snapshot(account_id="other_account", filters_fingerprint="x")
    page = await store.read_page(
        snapshot=snapshot,
        consumer_id=None,
        delivery_config_ids=None,
        media_buy_ids=None,
        offset=0,
        limit=50,
        changes_after_sequence=None,
    )
    assert page.total_count == 0


# -- leasing ----------------------------------------------------------------


async def test_a_lease_excludes_a_second_worker(store: PgReportingLedgerStore) -> None:
    await store.put_configuration(_configuration())
    now = datetime.now(timezone.utc)
    first = await store.lease_period_close(worker_id="w1", now=now, lease_seconds=60)
    assert first is not None and first.account_id == ACCOUNT
    assert await store.lease_period_close(worker_id="w2", now=now, lease_seconds=60) is None

    await store.release_period_close(first, worker_id="w1")
    assert await store.lease_period_close(worker_id="w2", now=now, lease_seconds=60) is not None


async def test_an_expired_lease_is_reclaimable(store: PgReportingLedgerStore) -> None:
    # A worker that dies mid-close must not wedge the period forever.
    await store.put_configuration(_configuration())
    now = datetime.now(timezone.utc)
    assert await store.lease_period_close(worker_id="w1", now=now, lease_seconds=1) is not None
    assert (
        await store.lease_period_close(
            worker_id="w2", now=now + timedelta(seconds=5), lease_seconds=60
        )
        is not None
    )


# -- status projection over Postgres ----------------------------------------


async def test_the_status_handler_projects_a_postgres_ledger(
    store: PgReportingLedgerStore,
) -> None:
    await store.put_configuration(_configuration())
    obligation = await store.commit_obligation(_obligation(_configuration()))
    revision, rows = _revision(obligation)
    await store.commit_revision(revision, rows)
    handler = ReportingStatusHandler(store)

    summary = await handler.handle({"view": "summary"}, caller=CALLER)
    assert summary["obligation_counts"]["total"] == 1
    assert summary["obligation_counts"]["satisfied"] == 1
    assert summary["health"] == "complete"

    periods = await handler.handle({"view": "periods"}, caller=CALLER)
    assert periods["periods"][0]["reporting_obligation_id"] == obligation.reporting_obligation_id
    assert periods["revisions"][0]["revision_content_sha256"] == revision.revision_content_sha256

    exact = await handler.handle(
        {"view": "revision", "reporting_revision_id": revision.reporting_revision_id},
        caller=CALLER,
    )
    assert exact["revision"]["row_count"] == 1


async def test_a_missing_report_is_visible_over_postgres(store: PgReportingLedgerStore) -> None:
    # The whole point of committing obligations before reports.
    await store.put_configuration(_configuration())
    await store.commit_obligation(_obligation(_configuration()))
    summary = await ReportingStatusHandler(store).handle({"view": "summary"}, caller=CALLER)
    assert summary["health"] == "action_required"
    assert summary["issues"][0]["code"] == "REPORT_OVERDUE"


# -- consumer status over Postgres ------------------------------------------


def _statement(**overrides) -> dict[str, object]:
    boundary = _boundary(0)
    payload: dict[str, object] = {
        "reporting_status_id": "status_pg_000000000001",
        "delivery_config_id": "daily_reporting",
        "delivery_config_version": 1,
        "report_definition_id": "daily_delivery_v1",
        "period": {
            "start": boundary.start.isoformat().replace("+00:00", "Z"),
            "end": boundary.end.isoformat().replace("+00:00", "Z"),
            "source_timezone": "UTC",
        },
        "consumer_status": "obligation_missing",
        "status_as_of": boundary.expected_at.isoformat().replace("+00:00", "Z"),
    }
    payload.update(overrides)
    return payload


async def test_consumer_status_records_and_supersedes_atomically(
    store: PgReportingLedgerStore,
) -> None:
    await store.put_configuration(_configuration())
    ingest = ConsumerStatusIngest(store, enabled=True)
    first = await ingest.handle(
        {"statuses": [_statement()]}, account_id=ACCOUNT, consumer_id="buyer_pg"
    )
    assert first["results"][0]["result"] == "recorded"
    replay = await ingest.handle(
        {"statuses": [_statement()]}, account_id=ACCOUNT, consumer_id="buyer_pg"
    )
    assert replay["results"][0]["result"] == "unchanged"

    repaired = await ingest.handle(
        {
            "statuses": [
                _statement(
                    reporting_status_id="status_pg_000000000002",
                    supersedes_reporting_status_id="status_pg_000000000001",
                    consumer_status="revision_missing",
                    reporting_obligation_id="rpo_pg_0",
                )
            ]
        },
        account_id=ACCOUNT,
        consumer_id="buyer_pg",
    )
    assert repaired["results"][0]["result"] == "recorded"

    stale = await ingest.handle(
        {
            "statuses": [
                _statement(
                    reporting_status_id="status_pg_000000000003",
                    supersedes_reporting_status_id="status_pg_000000000001",
                )
            ]
        },
        account_id=ACCOUNT,
        consumer_id="buyer_pg",
    )
    assert stale["results"][0]["errors"][0]["code"] == "STATUS_SUPERSEDES_STALE"


async def test_concurrent_status_updates_cannot_fork_a_chain(
    store: PgReportingLedgerStore,
) -> None:
    # The partial unique index on the unsuperseded leaf is the enforcement.
    await store.put_configuration(_configuration())
    ingest = ConsumerStatusIngest(store, enabled=True)
    attempts = [
        ingest.handle(
            {"statuses": [_statement(reporting_status_id=f"status_pg_00000000000{index}")]},
            account_id=ACCOUNT,
            consumer_id="buyer_pg",
        )
        for index in range(1, 5)
    ]
    outcomes = await asyncio.gather(*attempts, return_exceptions=True)
    recorded = [
        item
        for item in outcomes
        if not isinstance(item, BaseException) and item["results"][0]["result"] == "recorded"
    ]
    assert len(recorded) == 1


async def test_a_chain_filed_before_the_obligation_attaches_after_repair(
    store: PgReportingLedgerStore,
) -> None:
    await store.put_configuration(_configuration())
    ingest = ConsumerStatusIngest(store, enabled=True)
    await ingest.handle({"statuses": [_statement()]}, account_id=ACCOUNT, consumer_id="buyer_pg")
    obligation = await store.commit_obligation(_obligation(_configuration()))
    attached = await store.list_consumer_statuses(
        account_id=ACCOUNT,
        consumer_id="buyer_pg",
        reporting_obligation_ids=[obligation.reporting_obligation_id],
    )
    assert [item.reporting_status_id for item in attached] == ["status_pg_000000000001"]


async def test_consumer_statuses_are_not_disclosed_across_callers(
    store: PgReportingLedgerStore,
) -> None:
    await store.put_configuration(_configuration())
    await ConsumerStatusIngest(store, enabled=True).handle(
        {"statuses": [_statement()]}, account_id=ACCOUNT, consumer_id="buyer_pg"
    )
    assert (
        await store.list_consumer_statuses(
            account_id=ACCOUNT, consumer_id="another_buyer", reporting_obligation_ids=None
        )
        == ()
    )


async def test_a_conflicting_statement_degrades_only_its_own_caller(
    store: PgReportingLedgerStore,
) -> None:
    await store.put_configuration(_configuration())
    obligation = await store.commit_obligation(_obligation(_configuration()))
    revision, rows = _revision(obligation)
    await store.commit_revision(revision, rows)
    await ConsumerStatusIngest(store, enabled=True).handle(
        {"statuses": [_statement()]}, account_id=ACCOUNT, consumer_id="buyer_pg"
    )
    handler = ReportingStatusHandler(store, consumer_status_enabled=True)

    mine = await handler.handle({"view": "periods"}, caller=CALLER)
    assert mine["periods"][0]["health"] == "action_required"
    assert mine["periods"][0]["issues"][0]["code"] == "CONSUMER_STATUS_MISMATCH"
    assert mine["periods"][0]["consumer_status_count"] == 1

    theirs = await handler.handle(
        {"view": "periods"},
        caller=ReportingStatusCaller(account_id=ACCOUNT, consumer_id="another_buyer"),
    )
    assert theirs["periods"][0]["health"] == "complete"
    assert theirs["periods"][0]["issues"] == []
    assert theirs["consumer_statuses"] == []
