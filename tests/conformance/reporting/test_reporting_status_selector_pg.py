"""Populated real-C cutover, immutable retention and old row-only lease races."""

import asyncio
import json
import subprocess
import sys
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

import pytest

from adcp.reporting.ledger import PgReportingReconciliationStore
from adcp.reporting.outbox import (
    ActivityRequest,
    ReportingEnvelopeCipher,
    ReportingNotificationWorker,
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


async def test_actual_c_status_rows_are_archived_after_worker_stop(frozen_c):
    from psycopg import sql

    from adcp.reporting.migration import migrate_legacy_reporting

    async with isolated_reporting_pool(autocommit=True) as pool:
        async with old_c(pool, frozen_c) as old:
            assert await old.call(action="schema") == {"result": True}
            async with pool.connection() as connection:
                await connection.execute(
                    (ROOT / "tests/fixtures/reporting_ledger_beta15_data.sql").read_text()
                )
            assert await old.call(action="baseline") == {"result": True}
            retained = await physical_rows(
                pool,
                tables=(
                    "reporting_status_accounts",
                    "reporting_status_scope_checkpoints",
                    *C_QUEUES,
                ),
            )
        # The old process has exited; no concurrent v1/v2 writing is supported.
        archive = "adcp_reporting_quarantine_actual_c"
        async with pool.connection() as connection:
            await migrate_legacy_reporting(connection, archive_schema=archive, workers_stopped=True)
            try:
                for table, rows in retained.items():
                    archived = await (
                        await connection.execute(
                            sql.SQL(
                                "SELECT t.ctid::text,t.xmin::text,to_jsonb(t) FROM {}.{} t ORDER BY t.ctid"
                            ).format(sql.Identifier(archive), sql.Identifier(table))
                        )
                    ).fetchall()
                    assert archived == rows
                await validate_status_schema(connection)
                assert (
                    await (
                        await connection.execute(
                            "SELECT count(*) FROM reporting_status_scope_checkpoints"
                        )
                    ).fetchone()
                )[0] == 0
            finally:
                await connection.execute(
                    sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(archive))
                )


async def test_actual_c_cannot_start_on_owned_schema_or_rewrite_evidence(frozen_c):
    async with isolated_reporting_pool(autocommit=True) as pool:
        ledger = PgReportingReconciliationStore(pool=pool, notifications=True)
        await ledger.create_schema()
        await seed(ledger, readable=True)
        before = await retained_physical_rows(pool)
        async with old_c(pool, frozen_c) as old:
            result = await old.call(action="schema")
            assert result == {"error": "database_fence", "sqlstate": "P0001"}
        assert await retained_physical_rows(pool) == before
