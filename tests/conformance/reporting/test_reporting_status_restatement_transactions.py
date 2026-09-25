"""Restatement scheduling joins the same source transaction as status evidence."""

import asyncio

import pytest

from adcp.reporting.ledger import PgReportingLedgerStore, RestatementCheckpoint
from adcp.reporting.outbox import PgStatusNotificationStore

from . import test_reporting_status_projection_contract as _contract
from ._generation_support import configuration, isolated_reporting_pool, obligation_for
from ._reliable_support import SimulatedCrash

status_harness = _contract.status_harness


async def test_restatement_checkpoint_commits_and_rolls_back_with_source_transaction(
    status_harness,
):
    h = status_harness
    obligation, _, _ = await h.seed()
    await h.status.baseline(account_id="acct_a")
    dirty = await h.status.outbox.read_status_dirty(account_id="acct_a")
    projected = await h.status.checkpoints(account_id="acct_a")
    events = await h.status.outbox.list_events(account_id="acct_a")
    checkpoint = RestatementCheckpoint(
        account_id="acct_a",
        reporting_obligation_id=obligation.reporting_obligation_id,
        checked_at=h.clock.now,
        next_observation=1,
    )

    async def turn(*, fail):
        async with h.ledger.transaction():
            written = await h.ledger.record_restatement_checkpoint(checkpoint)
            assert written == checkpoint
            assert (
                await h.ledger.get_restatement_checkpoint(
                    account_id="acct_a",
                    reporting_obligation_id=obligation.reporting_obligation_id,
                )
                == checkpoint
            )
            if fail:
                raise SimulatedCrash("checkpoint_before_source_commit")

    with pytest.raises(SimulatedCrash, match="checkpoint_before_source_commit"):
        await asyncio.wait_for(turn(fail=True), 5)
    assert (
        await h.ledger.get_restatement_checkpoint(
            account_id="acct_a", reporting_obligation_id=obligation.reporting_obligation_id
        )
        is None
    )
    await asyncio.wait_for(turn(fail=False), 5)
    assert (
        await h.ledger.get_restatement_checkpoint(
            account_id="acct_a", reporting_obligation_id=obligation.reporting_obligation_id
        )
        == checkpoint
    )
    # Scheduling progress alone changes neither public status nor its dirty journal.
    assert await h.status.outbox.read_status_dirty(account_id="acct_a") == dirty
    assert await h.status.checkpoints(account_id="acct_a") == projected
    assert await h.status.outbox.list_events(account_id="acct_a") == events


@pytest.mark.parametrize("first_operation", ["read", "write"])
async def test_checkpoint_uses_bound_connection_for_uncommitted_obligation(
    first_operation, monkeypatch
):
    async with isolated_reporting_pool(autocommit=True) as owner:
        from psycopg_pool import AsyncConnectionPool

        async with AsyncConnectionPool(
            owner.conninfo,
            kwargs=owner.kwargs,
            min_size=1,
            max_size=1,
            open=False,
        ) as pool:
            ledger = PgReportingLedgerStore(pool=pool, notifications=True)
            await PgStatusNotificationStore(ledger).create_schema()
            config = configuration()
            await ledger.put_configuration(config)
            acquired = set()
            backends = []
            get, put = pool.getconn, pool.putconn

            async def getconn(*args, **kwargs):
                task = asyncio.current_task()
                assert task not in acquired, "checkpoint attempted a nested pool acquisition"
                acquired.add(task)
                connection = await get(*args, **kwargs)
                backends.append(connection.info.backend_pid)
                return connection

            async def putconn(connection):
                acquired.remove(asyncio.current_task())
                return await put(connection)

            monkeypatch.setattr(pool, "getconn", getconn)
            monkeypatch.setattr(pool, "putconn", putconn)

            async def turn():
                async with ledger.transaction():
                    obligation = await ledger.commit_obligation(obligation_for(config))
                    checkpoint = RestatementCheckpoint(
                        account_id="acct_a",
                        reporting_obligation_id=obligation.reporting_obligation_id,
                        checked_at=obligation.period.end,
                        next_observation=1,
                    )
                    if first_operation == "read":
                        assert (
                            await ledger.get_restatement_checkpoint(
                                account_id="acct_a",
                                reporting_obligation_id=obligation.reporting_obligation_id,
                            )
                            is None
                        )
                    written = await ledger.record_restatement_checkpoint(checkpoint)
                    assert written == checkpoint
                    assert (
                        await ledger.get_restatement_checkpoint(
                            account_id="acct_a",
                            reporting_obligation_id=obligation.reporting_obligation_id,
                        )
                        == checkpoint
                    )

            await asyncio.wait_for(turn(), 5)
            assert len(backends) == 1
            assert not acquired
