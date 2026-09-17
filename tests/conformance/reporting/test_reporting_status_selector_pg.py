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
    assert_c_collated_rolling_database,
    configuration,
    isolated_reporting_pool,
    obligation_for,
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
C_SHA = "ea150fabd5ad90e3abf93f89729d2919f1c61798"


@pytest.fixture(scope="module")
def frozen_c(tmp_path_factory):
    assert_c_collated_rolling_database()
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
        assert Path((await child.receive())["origin"]).is_relative_to(source)
        yield child
        assert await child.call(action="stop") == {"stopped": True}
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
    assert await status.outbox.reserve_attempt(
        lease, request=ActivityRequest("https://receiver.example.test/reporting", 1), now=now
    )
    assert await status.outbox.finish_delivery(lease, state="pending", retry_at=now, now=now)


async def test_actual_c_claim_fence_pool_marker_two_projectors_two_sweepers_and_source(frozen_c):
    from psycopg_pool import AsyncConnectionPool

    async with isolated_reporting_pool(autocommit=True) as owner, old_c(owner, frozen_c) as old:
        assert await old.call(action="schema") == {"result": True}
        clock = [NOW]
        ledger = PgReportingReconciliationStore(
            pool=owner, notifications=True, clock=lambda: clock[0]
        )
        waiting, _ = await seed(ledger)
        _, ready = await seed(ledger, readable=True, name="ready")
        await seed(ledger, account="acct_b")
        baseline_at = waiting.period.expected_at - timedelta(seconds=1)
        for account in ("acct_a", "acct_b"):
            assert await old.call(action="baseline", account=account, now=baseline_at) == {
                "result": True
            }
            assert await old.call(action="ready", account=account) == {"result": True}
        # A populated C event and every C activity table exist before cutover.
        await old.call(
            action="source", revision=ready.reporting_revision_id, readable=False, now=baseline_at
        )
        assert (await old.call(action="project"))["result"]["events"] == 2
        status = PgStatusNotificationStore(ledger)
        await populate_status_activity(status)
        immutable = await retained_physical_rows(owner)
        queue_rows = await physical_rows(owner)
        assert all(queue_rows.values())
        await status.create_schema()
        assert await retained_physical_rows(owner) == immutable
        assert await physical_rows(owner) == queue_rows
        assert not await status.baseline_ready(account_id="acct_a")
        assert await old.call(action="ready") == {"result": True}  # Schema alone isn't cutover.
        # Old claim owns only a checkpoint row. The new fence owns the account
        # then waits for that row; the old trigger must never acquire account.
        await old.send(action="claim", now=NOW, hold="claim")
        assert await old.receive() == {"held": True}
        fencing = asyncio.create_task(status.project_one(account_id="acct_a"))
        await asyncio.sleep(0.02)
        assert not fencing.done()
        await old.send(action="release_hold")
        old_lease = (await old.receive())["result"]
        assert old_lease is not None
        assert (await asyncio.wait_for(fencing, 10)).events == 0
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
        assert (await old.call(action="claim"))["error"] == "database_fence"
        assert (await old.call(action="complete", lease=0))["error"] == "status_policy_conflict"
        assert (await old.call(action="release", lease=0))["error"] == "database_fence"
        assert (await old.call(action="ready"))["error"] == "status_policy_conflict"
        assert (await old.call(action="project"))["error"] == "status_policy_conflict"
        assert await physical_rows(owner, tables=tuple(fenced_rows)) == fenced_rows
        assert await old.call(action="schema") == {"result": True}
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
        assert (await old.call(action="ready"))["error"] == "status_policy_conflict"
        assert await old.call(action="ready", account="acct_b") == {"result": True}
        clock[0] = waiting.automated_recovery_deadline_at
        assert (await ReportingStatusSweeper(status).run_once(account_id="acct_a")).did_work
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
        assert await status.baseline(account_id="brand-new")
        assert await status.baseline_ready(account_id="brand-new")
        assert not await status.outbox.list_events(account_id="brand-new")


async def test_actual_c_unique_looking_snapshot_corruption_corrects_without_baseline_reset(
    frozen_c,
):
    async with isolated_reporting_pool(autocommit=True) as pool, old_c(pool, frozen_c) as old:
        assert await old.call(action="schema") == {"result": True}
        ledger = PgReportingReconciliationStore(pool=pool, notifications=True, clock=lambda: NOW)
        obligation, _ = await seed(ledger, readable=True)
        second, rows = revision_for(obligation, suffix="disconnected")
        await ledger.commit_revision(second, rows)  # v1 persistence accepts another snapshot root.
        assert await old.call(action="baseline") == {"result": True}
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
        assert not (await status.rebuild_one()).did_work
        assert not (await status.project_one(account_id="acct_a")).did_work
        assert await physical_rows(pool, tables=tuple(physical)) == physical


async def test_actual_c_inflight_projector_commits_before_v2_fence_and_source_serializes(frozen_c):
    async with isolated_reporting_pool(autocommit=True) as pool, old_c(pool, frozen_c) as old:
        assert await old.call(action="schema") == {"result": True}
        ledger = PgReportingReconciliationStore(pool=pool, notifications=True, clock=lambda: NOW)
        _, revision = await seed(ledger, readable=True)
        assert await old.call(action="baseline") == {"result": True}
        await old.call(action="source", revision=revision.reporting_revision_id, readable=False)
        status = PgStatusNotificationStore(ledger)
        await status.create_schema()
        await old.send(action="project", hold="project")
        assert await old.receive() == {"held": True}
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
        assert (await old.receive())["result"]["events"] == 2
        assert (await asyncio.wait_for(fence, 10)).events == 0
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
        assert (await old.call(action="project"))["error"] == "status_policy_conflict"


async def test_populated_selector_sql_rollback_and_repeated_concurrent_install(frozen_c):
    async with isolated_reporting_pool(autocommit=True) as pool, old_c(pool, frozen_c) as old:
        assert await old.call(action="schema") == {"result": True}
        ledger = PgReportingReconciliationStore(pool=pool, notifications=True, clock=lambda: NOW)
        await seed(ledger, readable=True)
        assert await old.call(action="baseline") == {"result": True}
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
        assert await old.call(action="ready") == {"result": True}
        status = PgStatusNotificationStore(ledger)
        await asyncio.wait_for(asyncio.gather(status.create_schema(), status.create_schema()), 30)
        await status.create_schema()
        assert await retained_physical_rows(pool) == immutable
        assert not await status.baseline_ready(account_id="acct_a")
        assert await old.call(action="ready") == {"result": True}
        await drain(status)
        assert await status.baseline_ready(account_id="acct_a")
        assert not await status.outbox.list_events(account_id="acct_a")
