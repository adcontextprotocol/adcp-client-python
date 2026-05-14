"""Conformance tests for :class:`adcp.decisioning.pg.PgProposalStore`.

Requires a real PostgreSQL instance. To run locally::

    docker run --rm -d -p 5432:5432 -e POSTGRES_PASSWORD=pg postgres:16
    export ADCP_PG_TEST_URL=postgresql://postgres:pg@localhost:5432/postgres
    pytest tests/conformance/decisioning/test_pg_proposal_store.py -v

The entire module skips when ``ADCP_PG_TEST_URL`` is unset. Each test
runs against a freshly-created ``adcp_proposal_drafts_<random>`` table
so parallel runs don't collide.

These tests mirror the behavioural guarantees of
``tests/test_proposal_store.py`` (InMemoryProposalStore) against a real
Postgres engine to catch SQL-level divergence — especially the
``SELECT ... FOR UPDATE`` CAS in :meth:`try_reserve_consumption` that
has no equivalent in the in-memory ref.
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
        "ADCP_PG_TEST_URL not set — skipping PgProposalStore conformance tests",
        allow_module_level=True,
    )

from adcp.decisioning.pg import PgProposalStore  # noqa: E402
from adcp.decisioning.proposal_store import ProposalState  # noqa: E402
from adcp.decisioning.recipe import Recipe  # noqa: E402
from adcp.decisioning.types import AdcpError  # noqa: E402


def _utc(dt_str: str) -> datetime:
    return datetime.fromisoformat(dt_str).replace(tzinfo=timezone.utc)


@pytest.fixture()
async def store() -> AsyncIterator[PgProposalStore]:
    table = f"adcp_proposals_{secrets.token_hex(6)}"
    async with psycopg_pool.AsyncConnectionPool(
        TEST_URL,
        min_size=2,
        max_size=8,
        open=False,
    ) as pool:
        await pool.open()
        s = PgProposalStore(pool=pool, table_name=table)
        await s.create_schema()
        try:
            yield s
        finally:
            async with pool.connection() as conn:
                await conn.execute(f"DROP TABLE IF EXISTS {table}")  # noqa: S608


# -- put_draft / get -------------------------------------------------------


@pytest.mark.asyncio
async def test_put_draft_then_get_round_trips(store: PgProposalStore) -> None:
    recipes = {"prod_1": Recipe()}
    payload = {"proposal_id": "p1", "v": 1}
    await store.put_draft(
        proposal_id="p1",
        account_id="acct_a",
        recipes=recipes,
        proposal_payload=payload,
    )
    record = await store.get("p1")
    assert record is not None
    assert record.proposal_id == "p1"
    assert record.account_id == "acct_a"
    assert record.state == ProposalState.DRAFT
    assert "prod_1" in record.recipes
    assert record.proposal_payload == payload


@pytest.mark.asyncio
async def test_get_cross_tenant_returns_none(store: PgProposalStore) -> None:
    await store.put_draft(
        proposal_id="p1",
        account_id="acct_a",
        recipes={},
        proposal_payload={},
    )
    assert await store.get("p1", expected_account_id="acct_b") is None
    assert await store.get("p1", expected_account_id="acct_a") is not None


@pytest.mark.asyncio
async def test_commit_promotes_draft_to_committed(store: PgProposalStore) -> None:
    await store.put_draft(
        proposal_id="p1",
        account_id="acct_a",
        recipes={},
        proposal_payload={},
    )
    expires = _utc("2099-01-02T00:00:00")
    await store.commit(
        "p1",
        expires_at=expires,
        proposal_payload={"committed": True},
        expected_account_id="acct_a",
    )
    record = await store.get("p1")
    assert record is not None
    assert record.state == ProposalState.COMMITTED
    assert record.expires_at == expires
    assert record.proposal_payload == {"committed": True}


@pytest.mark.asyncio
async def test_commit_rejects_cross_tenant(store: PgProposalStore) -> None:
    """commit() called with a mismatched expected_account_id MUST fail —
    the cross-tenant write surface (#727 security)."""
    await store.put_draft(
        proposal_id="p1",
        account_id="acct_a",
        recipes={},
        proposal_payload={},
    )
    with pytest.raises(AdcpError) as exc:
        await store.commit(
            "p1",
            expires_at=_utc("2099-01-02T00:00:00"),
            proposal_payload={},
            expected_account_id="acct_b",
        )
    assert exc.value.code == "INTERNAL_ERROR"


@pytest.mark.asyncio
async def test_commit_idempotent_on_equal_values(store: PgProposalStore) -> None:
    await store.put_draft(
        proposal_id="p1",
        account_id="acct_a",
        recipes={},
        proposal_payload={},
    )
    expires = _utc("2099-01-02T00:00:00")
    payload = {"committed": True}
    await store.commit(
        "p1", expires_at=expires, proposal_payload=payload, expected_account_id="acct_a"
    )
    await store.commit(
        "p1", expires_at=expires, proposal_payload=payload, expected_account_id="acct_a"
    )
    record = await store.get("p1")
    assert record is not None
    assert record.state == ProposalState.COMMITTED


@pytest.mark.asyncio
async def test_commit_rejects_diverging_values(store: PgProposalStore) -> None:
    await store.put_draft(
        proposal_id="p1",
        account_id="acct_a",
        recipes={},
        proposal_payload={},
    )
    await store.commit(
        "p1",
        expires_at=_utc("2099-01-02T00:00:00"),
        proposal_payload={"v": 1},
        expected_account_id="acct_a",
    )
    with pytest.raises(AdcpError) as exc:
        await store.commit(
            "p1",
            expires_at=_utc("2099-01-03T00:00:00"),
            proposal_payload={"v": 1},
            expected_account_id="acct_a",
        )
    assert exc.value.code == "INTERNAL_ERROR"


# -- try_reserve_consumption + finalize / release -------------------------


@pytest.mark.asyncio
async def test_reserve_finalize_round_trip(store: PgProposalStore) -> None:
    await store.put_draft(
        proposal_id="p1",
        account_id="acct_a",
        recipes={},
        proposal_payload={},
    )
    await store.commit(
        "p1",
        expires_at=_utc("2099-01-02T00:00:00"),
        proposal_payload={},
        expected_account_id="acct_a",
    )
    reserved = await store.try_reserve_consumption("p1", expected_account_id="acct_a")
    assert reserved.state == ProposalState.CONSUMING
    await store.finalize_consumption("p1", media_buy_id="mb_1", expected_account_id="acct_a")
    record = await store.get("p1")
    assert record is not None
    assert record.state == ProposalState.CONSUMED
    assert record.media_buy_id == "mb_1"


@pytest.mark.asyncio
async def test_concurrent_reserve_serializes(store: PgProposalStore) -> None:
    """Two parallel try_reserve_consumption calls — exactly one wins."""
    await store.put_draft(
        proposal_id="p1",
        account_id="acct_a",
        recipes={},
        proposal_payload={},
    )
    await store.commit(
        "p1",
        expires_at=_utc("2099-01-02T00:00:00"),
        proposal_payload={},
        expected_account_id="acct_a",
    )

    async def attempt() -> tuple[bool, str | None]:
        try:
            await store.try_reserve_consumption("p1", expected_account_id="acct_a")
            return (True, None)
        except AdcpError as e:
            return (False, e.code)

    results = await asyncio.gather(attempt(), attempt())
    successes = [r for r in results if r[0]]
    failures = [r for r in results if not r[0]]
    assert len(successes) == 1
    assert len(failures) == 1
    assert failures[0][1] == "PROPOSAL_NOT_COMMITTED"


@pytest.mark.asyncio
async def test_reserve_cross_tenant_collapses_to_not_found(store: PgProposalStore) -> None:
    await store.put_draft(
        proposal_id="p1",
        account_id="acct_a",
        recipes={},
        proposal_payload={},
    )
    await store.commit(
        "p1",
        expires_at=_utc("2099-01-02T00:00:00"),
        proposal_payload={},
        expected_account_id="acct_a",
    )
    with pytest.raises(AdcpError) as exc:
        await store.try_reserve_consumption("p1", expected_account_id="acct_b")
    assert exc.value.code == "PROPOSAL_NOT_FOUND"


# -- discard cross-tenant safety ------------------------------------------


@pytest.mark.asyncio
async def test_discard_cross_tenant_is_noop(store: PgProposalStore) -> None:
    """discard() with a cross-tenant expected_account_id must NOT delete
    the row (#727 security)."""
    await store.put_draft(
        proposal_id="p1",
        account_id="acct_a",
        recipes={},
        proposal_payload={},
    )
    await store.discard("p1", expected_account_id="acct_b")
    record = await store.get("p1", expected_account_id="acct_a")
    assert record is not None  # not deleted


@pytest.mark.asyncio
async def test_discard_same_tenant_removes(store: PgProposalStore) -> None:
    await store.put_draft(
        proposal_id="p1",
        account_id="acct_a",
        recipes={},
        proposal_payload={},
    )
    await store.discard("p1", expected_account_id="acct_a")
    assert await store.get("p1", expected_account_id="acct_a") is None


# -- get_by_media_buy_id ---------------------------------------------------


@pytest.mark.asyncio
async def test_get_by_media_buy_id_after_finalize(store: PgProposalStore) -> None:
    await store.put_draft(
        proposal_id="p1",
        account_id="acct_a",
        recipes={},
        proposal_payload={},
    )
    await store.commit(
        "p1",
        expires_at=_utc("2099-01-02T00:00:00"),
        proposal_payload={},
        expected_account_id="acct_a",
    )
    await store.try_reserve_consumption("p1", expected_account_id="acct_a")
    await store.finalize_consumption("p1", media_buy_id="mb_1", expected_account_id="acct_a")
    record = await store.get_by_media_buy_id("mb_1", expected_account_id="acct_a")
    assert record is not None
    assert record.proposal_id == "p1"


# -- expires_at round-trips with UTC ---------------------------------------


@pytest.mark.asyncio
async def test_expires_at_round_trip_preserves_utc(store: PgProposalStore) -> None:
    await store.put_draft(
        proposal_id="p1",
        account_id="acct_a",
        recipes={},
        proposal_payload={},
    )
    expires = datetime.now(timezone.utc) + timedelta(days=7)
    await store.commit("p1", expires_at=expires, proposal_payload={}, expected_account_id="acct_a")
    record = await store.get("p1")
    assert record is not None
    assert record.expires_at is not None
    assert record.expires_at.tzinfo is not None
    assert abs((record.expires_at - expires).total_seconds()) < 1
