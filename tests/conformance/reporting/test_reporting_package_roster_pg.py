"""Frozen package roster admission through the PostgreSQL obligation codec."""

from __future__ import annotations

import pytest
from adcp_reporting_storage.ledger.pg import PgReportingLedgerStore

from adcp.reporting.ledger import LedgerConflictError
from tests.conformance.reporting._generation_support import isolated_reporting_pool
from tests.test_reporting_ledger import _configuration, _obligation, _revision

psycopg = pytest.importorskip("psycopg")


@pytest.mark.parametrize(
    ("package_ids", "rows", "code"),
    [
        (
            ("pkg-a",),
            [{"media_buy_id": "mb_1", "package_id": "pkg-outside", "impressions": 5}],
            "PACKAGE_ROW_OUTSIDE_ROSTER",
        ),
        (
            ("pkg-a", "pkg-b"),
            [{"media_buy_id": "mb_1", "package_id": "pkg-a", "impressions": 5}],
            "PACKAGE_ROW_MISSING",
        ),
    ],
)
async def test_direct_store_rejects_incomplete_package_rows(
    package_ids: tuple[str, ...], rows: list[dict], code: str
) -> None:
    async with isolated_reporting_pool() as pool:
        store = PgReportingLedgerStore(pool=pool)
        await store.create_schema()
        configuration = _configuration()
        await store.put_configuration(configuration)
        obligation = await store.commit_obligation(
            _obligation(configuration, package_ids=package_ids, package_reporting=True)
        )
        retained = await store.get_obligation(
            account_id=obligation.account_id,
            reporting_obligation_id=obligation.reporting_obligation_id,
        )
        assert retained is not None and retained.package_reporting is True
        revision, committed_rows = _revision(obligation, rows=rows)
        with pytest.raises(LedgerConflictError) as conflict:
            await store.commit_revision(revision, committed_rows)
        assert conflict.value.code == code
        assert (
            await store.list_revisions(
                account_id=obligation.account_id,
                reporting_obligation_id=obligation.reporting_obligation_id,
            )
            == ()
        )


@pytest.mark.parametrize("coverage_status", ["partial", "none", "unknown"])
async def test_direct_store_requires_full_package_roster(coverage_status: str) -> None:
    async with isolated_reporting_pool() as pool:
        store = PgReportingLedgerStore(pool=pool)
        await store.create_schema()
        configuration = _configuration()
        await store.put_configuration(configuration)
        obligation = await store.commit_obligation(
            _obligation(
                configuration,
                package_ids=("pkg-a",),
                package_reporting=True,
                coverage_status=coverage_status,
            )
        )
        revision, rows = _revision(
            obligation, rows=[{"media_buy_id": "mb_1", "package_id": "pkg-a", "impressions": 5}]
        )
        with pytest.raises(LedgerConflictError) as conflict:
            await store.commit_revision(revision, rows)
        assert conflict.value.code == "PACKAGE_ROSTER_UNAVAILABLE"


async def test_package_mode_is_immutable_in_postgres() -> None:
    async with isolated_reporting_pool() as pool:
        store = PgReportingLedgerStore(pool=pool)
        await store.create_schema()
        configuration = _configuration()
        await store.put_configuration(configuration)
        obligation = await store.commit_obligation(
            _obligation(configuration, package_ids=("pkg-a",), package_reporting=True)
        )
        async with pool.connection() as connection:
            with pytest.raises(psycopg.errors.CheckViolation) as refused:
                async with connection.transaction():
                    await connection.execute(
                        "UPDATE reporting_obligations SET package_reporting=FALSE"
                        " WHERE reporting_obligation_id=%s",
                        (obligation.reporting_obligation_id,),
                    )
        assert getattr(refused.value, "sqlstate", None) == "23514"


async def test_unmarked_retained_package_obligation_fails_closed() -> None:
    from adcp_reporting_storage.migrations import migrate

    async with isolated_reporting_pool() as pool:
        store = PgReportingLedgerStore(pool=pool)
        await store.create_schema()
        configuration = _configuration()
        await store.put_configuration(configuration)
        obligation = await store.commit_obligation(
            _obligation(configuration, package_ids=("pkg-a",), package_reporting=True)
        )
        async with pool.connection() as connection, connection.transaction():
            await connection.execute(
                "ALTER TABLE reporting_obligations DROP COLUMN package_reporting CASCADE"
            )
        await migrate(pool)
        retained = await store.get_obligation(
            account_id=obligation.account_id,
            reporting_obligation_id=obligation.reporting_obligation_id,
        )
        assert retained is not None and retained.package_reporting is None
        revision, rows = _revision(
            obligation, rows=[{"media_buy_id": "mb_1", "package_id": "pkg-a", "impressions": 5}]
        )
        with pytest.raises(LedgerConflictError) as conflict:
            await store.commit_revision(revision, rows)
        assert conflict.value.code == "PACKAGE_MODE_UNAVAILABLE"
