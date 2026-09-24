"""Execute reviewed A/B modules, selected before any adcp import, in a process.

Only the shared test socket/barrier helpers come from the working tree. Every
SDK module and every worker/store/decoder is loaded from the detached artifact.
"""

from __future__ import annotations

import asyncio
import json
import sys
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path


async def main(settings, release):
    source = Path(settings["old_source"]).resolve()
    sys.path.insert(0, str(source / "src"))

    from psycopg_pool import AsyncConnectionPool

    import adcp.reporting.ledger.notification_models as models
    import adcp.reporting.outbox.worker as worker_module
    from adcp.reporting.ledger import PgReportingLedgerStore
    from adcp.reporting.outbox import (
        PgReportingOutbox,
        ReportingEnvelopeCipher,
        ReportingNotificationError,
        ReportingNotificationWorker,
    )
    from adcp.reporting.outbox._schema import validate_schema

    assert not hasattr(models, "StatusChanged")
    assert Path(worker_module.__file__).resolve().is_relative_to(source)
    from ._reliable_process import command, emit, install_test_socket
    from ._reliable_support import (
        FailurePlan,
        ScriptedSigning,
        ScriptedSubscriptions,
        notification_subscription,
    )

    for name, module in tuple(sys.modules.items()):
        if name == "adcp" or name.startswith("adcp."):
            if getattr(module, "__file__", None):
                assert Path(module.__file__).resolve().is_relative_to(source), name

    failures = FailurePlan()
    subscriptions = ScriptedSubscriptions(failures)
    subscriptions.put(notification_subscription())
    if "receiver_port" in settings:
        install_test_socket(settings, lambda: datetime.now(timezone.utc))
    async with AsyncConnectionPool(
        settings["conninfo"], kwargs=settings["pool_kwargs"], min_size=1, max_size=1, open=False
    ) as pool:
        # Core bootstrap remains usable even when A's exact trigger manifest
        # deliberately rejects notification readiness after C migration.
        core = PgReportingLedgerStore(pool=pool)
        await core.create_schema()
        outbox = PgReportingOutbox(pool=pool)
        worker_options = {"activity": outbox} if release == "b" else {}
        worker = ReportingNotificationWorker(
            outbox=outbox,
            subscriptions=subscriptions,
            signing=ScriptedSigning(failures),
            cipher=ReportingEnvelopeCipher(b"e" * 32),
            **worker_options,
        )

        async def ready():
            async with pool.connection() as conn:
                try:
                    if release == "b":
                        await validate_schema(conn, activity=True)
                    else:
                        await validate_schema(conn)
                except ReportingNotificationError:
                    assert release == "a"
                    return "a_notifications_closed"
            return "notifications_ready"

        emit(
            "old_ready",
            classification=await ready(),
            module_origin=str(Path(worker_module.__file__).resolve()),
            source_sha=settings["source_sha"],
        )
        while True:
            request = await command(stage="old_worker_control")
            action = request["action"]
            if action == "stop":
                emit("done")
                return
            if action == "schema":
                await core.create_schema()
                emit("old_schema", classification=await ready())
            elif action == "turn":
                expanded = await worker.expand_one(account_id="acct_a")
                delivered = await worker.deliver_one(account_id="acct_a")
                emit("old_turn", did_work=expanded or delivered)
            elif action == "write":
                assert release == "b"
                async with pool.connection() as conn, conn.transaction():

                    class BoundPool:
                        @asynccontextmanager
                        async def connection(self):
                            yield conn

                    writer = PgReportingLedgerStore(pool=BoundPool(), notifications=True)
                    # Both dirty rows belong to one committed source transaction.
                    for readable in (True, False):
                        await writer.set_revision_readable(
                            account_id="acct_a",
                            reporting_revision_id=request["revision_id"],
                            readable=readable,
                        )
                writer = PgReportingLedgerStore(pool=pool, notifications=True)
                # These two separate commits form a real externally visible cycle.
                for readable in (True, False):
                    await writer.set_revision_readable(
                        account_id="acct_a",
                        reporting_revision_id=request["revision_id"],
                        readable=readable,
                    )
                emit("old_write")
            elif action == "orphan":
                assert release == "b"
                from adcp.reporting.ledger import ConsumerStatusRecord

                writer = PgReportingLedgerStore(pool=pool, notifications=True)
                obligation = await writer.get_obligation(
                    account_id="acct_a", reporting_obligation_id="rpo_acct_a"
                )
                assert obligation is not None
                async with pool.connection() as conn:
                    (at,) = await (await conn.execute("SELECT clock_timestamp()")).fetchone()
                # B's store commits this independently of ConsumerStatusIngest's
                # subsequent issue call. Exit after the first committed half.
                await writer.record_consumer_status(
                    ConsumerStatusRecord(
                        reporting_status_id="consumer-status-upgrade-orphan",
                        account_id="acct_a",
                        consumer_id="buyer",
                        delivery_config_id=obligation.delivery_config_id,
                        delivery_config_version=obligation.delivery_config_version,
                        report_definition_id=obligation.report_definition_id,
                        period_start=obligation.period.start,
                        period_end=obligation.period.end,
                        period_source_timezone=obligation.period.source_timezone,
                        consumer_status="unreadable",
                        failure_code="transport_failed",
                        status_as_of=at - timedelta(seconds=1),
                        recorded_at=at,
                        reporting_obligation_id=obligation.reporting_obligation_id,
                        reporting_revision_id=request["revision_id"],
                    )
                )
                emit("old_orphan")
            elif action == "core_write":
                from ._generation_support import configuration

                await core.put_configuration(configuration("legacy-core-only"))
                emit("old_core_write")
            else:
                raise AssertionError("unknown_old_worker_action")


if __name__ == "__main__":
    # No SDK import may precede the artifact path selection in main().
    settings = json.loads(sys.stdin.buffer.readline())
    try:
        asyncio.run(asyncio.wait_for(main(settings, sys.argv[1].removeprefix("old_")), 80))
    except Exception as exc:
        print(json.dumps({"point": "failed", "classification": type(exc).__name__}), flush=True)
        sys.exit(1)
