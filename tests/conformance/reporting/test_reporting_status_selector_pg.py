"""Populated real-C cutover, immutable retention and old row-only lease races."""

import asyncio
import json
import subprocess
import sys
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from importlib.resources import files
from pathlib import Path

import pytest

from adcp.reporting.ledger import PgReportingReconciliationStore
from adcp.reporting.outbox import (
    ActivityRequest,
    PgStatusNotificationStore,
    ReportingEnvelopeCipher,
    ReportingNotificationWorker,
    ReportingStatusSweeper,
)
from adcp.reporting.outbox.status_schema import validate_status_schema

from ._generation_support import (
    NOW,
    configuration,
    isolated_reporting_pool,
    obligation_for,
    require_rolling_database,
    revision_for,
)
from ._reliable_support import (
    FailurePlan,
    ScriptedSigning,
    ScriptedSubscriptions,
    notification_subscription,
)
from .test_reporting_notification_migration import retained_physical_rows
from .test_reporting_status_migration import C_QUEUES, physical_rows

ROOT = Path(__file__).resolve().parents[3]
C_SHA = "967b6e286301d7e5d089aea6fdbb90bea8ee5a16"


@pytest.fixture(scope="module")
def frozen_c(tmp_path_factory):
    require_rolling_database()
    root = tmp_path_factory.mktemp("frozen-c-selector") / "source"
    subprocess.run(
        ["git", "worktree", "add", "--detach", str(root), C_SHA],
        cwd=ROOT,
        check=True,
        capture_output=True,
        timeout=60,
    )
    try:
        yield root
    finally:
        subprocess.run(
            ["git", "worktree", "remove", "--force", str(root)],
            cwd=ROOT,
            check=True,
            capture_output=True,
            timeout=60,
        )


class OldC:
    def __init__(self, process):
        self.process = process

    async def send(self, **command):
        self.process.stdin.write(
            (json.dumps(command, default=lambda v: v.isoformat()) + "\n").encode()
        )
        await self.process.stdin.drain()

    async def receive(self):
        line = await asyncio.wait_for(self.process.stdout.readline(), 30)
        assert line, "frozen C process exited before replying"
        return json.loads(line)

    async def call(self, **command):
        await self.send(**command)
        return await self.receive()


@asynccontextmanager
async def old_c(pool, source, *, now=NOW):
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-I",
        str(Path(__file__).with_name("_frozen_status_c.py")),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    child = OldC(process)
    try:
        await child.send(source=str(source), conninfo=pool.conninfo, kwargs=pool.kwargs, now=now)
        destination_operation_3 = await child.receive()
        assert Path((destination_operation_3)["origin"]).is_relative_to(source)
        yield child
        destination_operation_4 = await child.call(action="stop")
        assert destination_operation_4 == {"stopped": True}
        await asyncio.wait_for(process.wait(), 10)
        assert process.returncode == 0
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()
        error = await process.stderr.read()
        assert not error, error.decode()


async def seed(ledger, *, account="acct_a", readable=False, name="daily"):
    config = replace(configuration(account), delivery_config_id=name)
    await ledger.put_configuration(config)
    obligation = replace(obligation_for(config), reporting_obligation_id=f"rpo_{account}_{name}")
    await ledger.commit_obligation(obligation)
    revision, rows = revision_for(obligation, suffix=name)
    if readable:
        await ledger.commit_revision(revision, rows)
    return obligation, revision


async def drain(status, account="acct_a"):
    for _ in range(40):
        if not (await status.project_one(account_id=account)).did_work:
            return
    pytest.fail("selector migration failed to converge")


async def populate_status_activity(status):
    failures = FailurePlan()
    subscriptions = ScriptedSubscriptions(failures)
    subscriptions.put(notification_subscription(events=("reporting.status_changed",)))
    worker = ReportingNotificationWorker(
        outbox=status.outbox,
        subscriptions=subscriptions,
        signing=ScriptedSigning(failures),
        cipher=ReportingEnvelopeCipher(b"e" * 32),
        activity=status.outbox,
    )
    while await worker.expand_one(account_id="acct_a"):
        pass
    now = datetime.now(timezone.utc)
    lease = await status.outbox.claim_delivery(account_id="acct_a", now=now, lease_seconds=60)
    assert lease is not None
    destination_operation_1 = await status.outbox.reserve_attempt(
        lease, request=ActivityRequest("https://receiver.example.test/reporting", 1), now=now
    )
    assert destination_operation_1
    destination_operation_2 = await status.outbox.finish_delivery(
        lease, state="pending", retry_at=now, now=now
    )
    assert destination_operation_2


async def test_actual_c_claim_fence_pool_marker_two_projectors_two_sweepers_and_source(frozen_c):
    from psycopg_pool import AsyncConnectionPool

    async with isolated_reporting_pool(autocommit=True) as owner, old_c(owner, frozen_c) as old:
        destination_operation_5 = await old.call(action="schema")
        assert destination_operation_5 == {"result": True}
        clock = [NOW]
        ledger = PgReportingReconciliationStore(
            pool=owner, notifications=True, clock=lambda: clock[0]
        )
        waiting, _ = await seed(ledger)
        _, ready = await seed(ledger, readable=True, name="ready")
        await seed(ledger, account="acct_b")
        baseline_at = waiting.period.expected_at - timedelta(seconds=1)
        for account in ("acct_a", "acct_b"):
            destination_operation_34 = await old.call(
                action="baseline", account=account, now=baseline_at
            )
            assert destination_operation_34 == {"result": True}
            destination_operation_35 = await old.call(action="ready", account=account)
            assert destination_operation_35 == {"result": True}
        # A populated C event and every C activity table exist before cutover.
        await old.call(
            action="source", revision=ready.reporting_revision_id, readable=False, now=baseline_at
        )
        destination_operation_6 = await old.call(action="project")
        assert (destination_operation_6)["result"]["events"] == 2
        status = PgStatusNotificationStore(ledger)
        await populate_status_activity(status)
        immutable = await retained_physical_rows(owner)
        queue_rows = await physical_rows(owner)
        assert all(queue_rows.values())
        await status.create_schema()
        assert await retained_physical_rows(owner) == immutable
        assert await physical_rows(owner) == queue_rows
        assert not await status.baseline_ready(account_id="acct_a")
        destination_operation_7 = await old.call(action="ready")
        assert destination_operation_7 == {"result": True}  # Schema alone isn't cutover.
        # Old claim owns only a checkpoint row. The new fence owns the account
        # then waits for that row; the old trigger must never acquire account.
        await old.send(action="claim", now=NOW, hold="claim")
        destination_operation_8 = await old.receive()
        assert destination_operation_8 == {"held": True}
        fencing = asyncio.create_task(status.project_one(account_id="acct_a"))
        await asyncio.sleep(0.02)
        assert not fencing.done()
        await old.send(action="release_hold")
        old_lease = (await old.receive())["result"]
        assert old_lease is not None
        destination_operation_9 = await asyncio.wait_for(fencing, 10)
        assert (destination_operation_9).events == 0
        # The fence preserves existing lease identity; old claims/completion
        # fail before a write, while v2 migration doesn't wait for lease expiry.
        assert any(
            c.lease_token == old_lease["token"]
            for c in await status.checkpoints(account_id="acct_a")
        )
        fenced_rows = await physical_rows(
            owner,
            tables=("reporting_status_accounts", "reporting_status_scope_checkpoints", *C_QUEUES),
        )
        destination_operation_10 = await old.call(action="claim")
        assert (destination_operation_10)["error"] == "database_fence"
        destination_operation_11 = await old.call(action="complete", lease=0)
        assert (destination_operation_11)["error"] == "status_policy_conflict"
        destination_operation_12 = await old.call(action="release", lease=0)
        assert (destination_operation_12)["error"] == "database_fence"
        destination_operation_13 = await old.call(action="ready")
        assert (destination_operation_13)["error"] == "status_policy_conflict"
        destination_operation_14 = await old.call(action="project")
        assert (destination_operation_14)["error"] == "status_policy_conflict"
        assert await physical_rows(owner, tables=tuple(fenced_rows)) == fenced_rows
        destination_operation_15 = await old.call(action="schema")
        assert destination_operation_15 == {"result": True}
        assert await physical_rows(owner, tables=tuple(fenced_rows)) == fenced_rows
        async with owner.connection() as c:
            await validate_status_schema(c, activity=True)
            assert (
                await (
                    await c.execute(
                        "SELECT current_setting('adcp.reporting.selector_semantics_version',true)"
                    )
                ).fetchone()
            )[0] in (None, "")
        # Four distinct size-one pools exercise the account lock across every
        # new projector/sweeper path; source dirties arrive concurrently.
        pools = [
            AsyncConnectionPool(
                owner.conninfo, kwargs=owner.kwargs, min_size=1, max_size=1, open=False
            )
            for _ in range(4)
        ]
        try:
            for pool in pools:
                await pool.open(wait=True)
            stores = [
                PgStatusNotificationStore(
                    PgReportingReconciliationStore(
                        pool=p, notifications=True, clock=lambda: clock[0]
                    )
                )
                for p in pools
            ]

            async def sweep(s):
                for _ in range(12):
                    await ReportingStatusSweeper(s).run_once(account_id="acct_a")

            await asyncio.wait_for(
                asyncio.gather(
                    drain(stores[0]),
                    drain(stores[1]),
                    sweep(stores[2]),
                    sweep(stores[3]),
                    ledger.set_revision_readable(
                        account_id="acct_a",
                        reporting_revision_id=ready.reporting_revision_id,
                        readable=True,
                    ),
                ),
                25,
            )
            await drain(stores[0])
            for pool in pools:
                async with pool.connection() as c:
                    assert (
                        await (
                            await c.execute(
                                "SELECT current_setting("
                                "'adcp.reporting.selector_semantics_version',true)"
                            )
                        ).fetchone()
                    )[0] in (None, "")
        finally:
            for pool in pools:
                await pool.close()
        assert await status.baseline_ready(account_id="acct_a")
        assert not await status.baseline_ready(account_id="acct_b")
        destination_operation_16 = await old.call(action="ready")
        assert (destination_operation_16)["error"] == "status_policy_conflict"
        destination_operation_17 = await old.call(action="ready", account="acct_b")
        assert destination_operation_17 == {"result": True}
        clock[0] = waiting.automated_recovery_deadline_at
        destination_operation_18 = await ReportingStatusSweeper(status).run_once(
            account_id="acct_a"
        )
        assert (destination_operation_18).did_work
        events = await status.outbox.list_events(account_id="acct_a")
        late = sorted(
            (
                e
                for e in events
                if e.cause.scope.reporting_obligation_id == waiting.reporting_obligation_id
            ),
            key=lambda e: e.cause.checkpoint_generation,
        )
        assert [(e.cause.previous_health, e.cause.health) for e in late] == [
            ("waiting", "delayed"),
            ("delayed", "action_required"),
        ]
        assert len(
            {(e.cause.scope.checkpoint_key, e.cause.checkpoint_generation) for e in events}
        ) == len(events)
        after = await physical_rows(owner)
        assert all(all(row in after[table] for row in rows) for table, rows in queue_rows.items())
        destination_operation_19 = await status.baseline(account_id="brand-new")
        assert destination_operation_19
        assert await status.baseline_ready(account_id="brand-new")
        assert not await status.outbox.list_events(account_id="brand-new")


async def test_actual_c_unique_looking_snapshot_corruption_corrects_without_baseline_reset(
    frozen_c,
):
    async with isolated_reporting_pool(autocommit=True) as pool, old_c(pool, frozen_c) as old:
        destination_operation_20 = await old.call(action="schema")
        assert destination_operation_20 == {"result": True}
        ledger = PgReportingReconciliationStore(pool=pool, notifications=True, clock=lambda: NOW)
        obligation, _ = await seed(ledger, readable=True)
        second, rows = revision_for(obligation, suffix="disconnected")
        await ledger.commit_revision(second, rows)  # v1 persistence accepts another snapshot root.
        destination_operation_21 = await old.call(action="baseline")
        assert destination_operation_21 == {"result": True}
        status = PgStatusNotificationStore(ledger)
        immutable = await retained_physical_rows(pool)
        await status.create_schema()
        before = await status.checkpoints(account_id="acct_a")
        assert all(
            c.snapshot["health"] == "complete" and c.selector_semantics_version == 1 for c in before
        )
        await drain(status)
        after = await status.checkpoints(account_id="acct_a")
        events = await status.outbox.list_events(account_id="acct_a")
        assert len(events) == 2
        assert all(
            e.cause.previous_health == "complete" and e.cause.health == "action_required"
            for e in events
        )
        assert all(c.snapshot["issues"][0]["code"] == "HISTORY_UNAVAILABLE" for c in after)
        assert [(c.scope, c.baseline, c.source_sequence) for c in after] == [
            (c.scope, c.baseline, c.source_sequence) for c in before
        ]
        assert await retained_physical_rows(pool) == immutable
        physical = await physical_rows(
            pool,
            tables=("reporting_status_accounts", "reporting_status_scope_checkpoints", *C_QUEUES),
        )
        destination_operation_22 = await status.rebuild_one()
        assert not (destination_operation_22).did_work
        destination_operation_23 = await status.project_one(account_id="acct_a")
        assert not (destination_operation_23).did_work
        assert await physical_rows(pool, tables=tuple(physical)) == physical


async def test_actual_c_inflight_projector_commits_before_v2_fence_and_source_serializes(frozen_c):
    async with isolated_reporting_pool(autocommit=True) as pool, old_c(pool, frozen_c) as old:
        destination_operation_24 = await old.call(action="schema")
        assert destination_operation_24 == {"result": True}
        ledger = PgReportingReconciliationStore(pool=pool, notifications=True, clock=lambda: NOW)
        _, revision = await seed(ledger, readable=True)
        destination_operation_25 = await old.call(action="baseline")
        assert destination_operation_25 == {"result": True}
        await old.call(action="source", revision=revision.reporting_revision_id, readable=False)
        status = PgStatusNotificationStore(ledger)
        await status.create_schema()
        await old.send(action="project", hold="project")
        destination_operation_26 = await old.receive()
        assert destination_operation_26 == {"held": True}
        fence = asyncio.create_task(status.project_one(account_id="acct_a"))
        publication = asyncio.create_task(
            ledger.set_revision_readable(
                account_id="acct_a",
                reporting_revision_id=revision.reporting_revision_id,
                readable=True,
            )
        )
        await asyncio.sleep(0.02)
        assert not fence.done() and not publication.done()
        await old.send(action="release_hold")
        destination_operation_27 = await old.receive()
        assert (destination_operation_27)["result"]["events"] == 2
        destination_operation_28 = await asyncio.wait_for(fence, 10)
        assert (destination_operation_28).events == 0
        await asyncio.wait_for(publication, 10)
        old_events = await physical_rows(pool)
        await drain(status)
        assert await status.baseline_ready(account_id="acct_a")
        events = await status.outbox.list_events(account_id="acct_a")
        assert len(events) == 4
        assert all(
            c.snapshot["health"] == "complete"
            for c in await status.checkpoints(account_id="acct_a")
        )
        after = await physical_rows(pool)
        assert all(all(row in after[table] for row in rows) for table, rows in old_events.items())
        destination_operation_29 = await old.call(action="project")
        assert (destination_operation_29)["error"] == "status_policy_conflict"


async def test_populated_selector_sql_rollback_and_repeated_concurrent_install(frozen_c):
    async with isolated_reporting_pool(autocommit=True) as pool, old_c(pool, frozen_c) as old:
        destination_operation_30 = await old.call(action="schema")
        assert destination_operation_30 == {"result": True}
        ledger = PgReportingReconciliationStore(pool=pool, notifications=True, clock=lambda: NOW)
        await seed(ledger, readable=True)
        destination_operation_31 = await old.call(action="baseline")
        assert destination_operation_31 == {"result": True}
        rows = await physical_rows(
            pool,
            tables=("reporting_status_accounts", "reporting_status_scope_checkpoints", *C_QUEUES),
        )
        immutable = await retained_physical_rows(pool)
        migration = (
            files("adcp.reporting.ledger")
            .joinpath("reporting_status_selector_version.sql")
            .read_text()
        )
        with pytest.raises(RuntimeError, match="interrupt migration"):
            async with pool.connection() as c, c.transaction():
                await c.execute(migration)
                raise RuntimeError("interrupt migration")
        assert await physical_rows(pool, tables=tuple(rows)) == rows
        assert await retained_physical_rows(pool) == immutable
        destination_operation_32 = await old.call(action="ready")
        assert destination_operation_32 == {"result": True}
        status = PgStatusNotificationStore(ledger)
        await asyncio.wait_for(asyncio.gather(status.create_schema(), status.create_schema()), 30)
        await status.create_schema()
        assert await retained_physical_rows(pool) == immutable
        assert not await status.baseline_ready(account_id="acct_a")
        destination_operation_33 = await old.call(action="ready")
        assert destination_operation_33 == {"result": True}
        await drain(status)
        assert await status.baseline_ready(account_id="acct_a")
        assert not await status.outbox.list_events(account_id="acct_a")
