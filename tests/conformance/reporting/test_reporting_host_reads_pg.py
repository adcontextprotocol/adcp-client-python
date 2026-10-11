"""Current-revision reads for in-process consumers: one lookup per obligation, paged by period."""

from __future__ import annotations

import base64
import json
from datetime import timedelta
from typing import Any

import pytest

from adcp.reporting.ledger import PgReportingLedgerStore

from ._generation_support import START, configuration, isolated_reporting_pool, obligation_for
from .test_reporting_row_storage_pg import fetch_all, many_rows, typed_revision


async def obligations(store: PgReportingLedgerStore, count: int) -> list[Any]:
    created = []
    for index in range(count):
        config = configuration("acct_a", consumer_id=f"buyer{index}")
        await store.put_configuration(config)
        created.append(await store.commit_obligation(obligation_for(config)))
    return created


async def test_the_current_revision_is_the_unsuperseded_snapshot_then_the_official_one() -> None:
    async with isolated_reporting_pool() as pool:
        store = PgReportingLedgerStore(pool=pool)
        await store.create_schema()
        config = configuration()
        await store.put_configuration(config)
        obligation = await store.commit_obligation(obligation_for(config))
        key = {
            "account_id": obligation.account_id,
            "reporting_obligation_id": obligation.reporting_obligation_id,
        }
        assert await store.get_current_revision(**key) is None

        rows = many_rows(2)
        first = typed_revision(obligation, rows, suffix="a")
        await store.commit_revision(first, rows)
        assert (
            await store.get_current_revision(**key)
        ).reporting_revision_id == first.reporting_revision_id
        second = typed_revision(
            obligation, rows, suffix="b", supersedes=first.reporting_revision_id
        )
        await store.commit_revision(second, rows)
        assert (
            await store.get_current_revision(**key)
        ).reporting_revision_id == second.reporting_revision_id

        # Scoped to the account: another account's lookup finds nothing.
        assert (
            await store.get_current_revision(
                account_id="acct_other", reporting_obligation_id=key["reporting_obligation_id"]
            )
            is None
        )


async def test_current_revisions_page_by_period_start_with_a_javascript_compatible_cursor() -> None:
    async with isolated_reporting_pool() as pool:
        store = PgReportingLedgerStore(pool=pool, change_feed=True)
        await store.create_schema()
        created = await obligations(store, 5)
        rows = many_rows(1)
        for index, obligation in enumerate(created[:4]):  # the fifth has no revision yet
            await store.commit_revision(typed_revision(obligation, rows, suffix=f"s{index}"), rows)

        window = {
            "account_id": "acct_a",
            "period_start_from": START - timedelta(days=1),
            "period_start_to": START + timedelta(days=1),
        }
        first = await store.list_current_revisions(**window, limit=3)
        assert len(first.revisions) == 3 and first.cursor is not None
        # The cursor is the format the JavaScript SDK reads.
        padded = first.cursor + "=" * (-len(first.cursor) % 4)
        decoded = json.loads(base64.urlsafe_b64decode(padded))
        assert set(decoded) == {"start", "id"} and decoded["start"].endswith("Z")
        second = await store.list_current_revisions(**window, limit=3, cursor=first.cursor)
        assert len(second.revisions) == 1 and second.cursor is None
        seen = [r.reporting_revision_id for r in (*first.revisions, *second.revisions)]
        assert len(set(seen)) == 4

        outside = await store.list_current_revisions(
            account_id="acct_a",
            period_start_from=START + timedelta(days=1),
            period_start_to=START + timedelta(days=2),
        )
        assert outside.revisions == () and outside.cursor is None
        none = await store.list_current_revisions(**{**window, "account_id": "acct_z"})
        assert none.revisions == ()
        filtered = await store.list_current_revisions(**window, delivery_config_id="nope")
        assert filtered.revisions == ()
        for bad in (0, 1001):
            with pytest.raises(ValueError):
                await store.list_current_revisions(**window, limit=bad)
        with pytest.raises(ValueError):
            await store.list_current_revisions(**window, cursor="!!")

        def cursor_of(body: object) -> str:
            return base64.urlsafe_b64encode(json.dumps(body).encode()).rstrip(b"=").decode()

        # Well-formed JSON that is not a usable position fails as a cursor error, never as a
        # driver error from the timestamp cast.
        for body in (
            {"start": "garbage", "id": "x"},
            {"start": "2026-09-01T00:00:00", "id": "x"},  # no zone
            {"start": 5, "id": "x"},
            {"start": "2026-09-01T00:00:00Z", "id": 7},
            {"id": "x"},
            ["start", "id"],
        ):
            with pytest.raises(ValueError, match="cursor is invalid"):
                await store.list_current_revisions(**window, cursor=cursor_of(body))
        # Hosts that enable the change feed get an index on the account's period
        # starts behind the page order.
        assert await fetch_all(
            pool,
            "SELECT 1 FROM pg_indexes WHERE schemaname = current_schema()"
            " AND indexname = 'reporting_obligations_account_period_idx'",
        ) == [(1,)]
