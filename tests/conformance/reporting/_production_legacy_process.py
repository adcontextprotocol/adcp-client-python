"""Executed by the real historical binary: incompatible C writers fail closed."""

import asyncio
import json
import sys
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path


async def main(settings):
    import hashlib

    from psycopg_pool import AsyncConnectionPool
    from pydantic import TypeAdapter

    import adcp.reporting.materializer.pg as materializer
    import adcp.reporting.outbox.status_pg as implementation
    from adcp.reporting.feed import PgReportingFeedStore
    from adcp.reporting.materializer import ReportingVerificationKey
    from adcp.reporting.materializer.work import ReportingMaterializerLease
    from adcp.reporting.outbox.status_pg import PgStatusNotificationStore

    path = Path(implementation.__file__).resolve()
    assert path.is_relative_to(Path(sys.prefix))
    assert hashlib.sha256(path.read_bytes()).hexdigest() == settings["module_sha256"]
    materializer_path = Path(materializer.__file__).resolve()
    assert materializer_path.is_relative_to(Path(sys.prefix))
    assert (
        hashlib.sha256(materializer_path.read_bytes()).hexdigest()
        == settings["materializer_module_sha256"]
    )
    async with AsyncConnectionPool(
        settings["conninfo"], kwargs=settings["kwargs"], min_size=1, max_size=1, open=False
    ) as pool:
        store = PgReportingFeedStore(
            pool=pool,
            clock=lambda: datetime(2099, 1, 1, tzinfo=timezone.utc),
            # This is an intentionally incompatible historical notification
            # writer, even when the new deployment uses polling only.
            notifications=settings["notifications"] if settings.get("pending") else True,
        )
        key = TypeAdapter(ReportingVerificationKey).validate_python(settings["verification_key"])
        await store.materializer_ready()
        if settings.get("pending"):
            # The actual parent binary reserves on its original schema before
            # any B2.4 migration. Its durable identity, not a reconstruction,
            # must survive the process being killed and the child's activation.
            for _ in range(16):
                lease = await store.claim_materialization(keys=(key,), lease_seconds=300)
                if isinstance(lease, ReportingMaterializerLease):
                    assert lease.attempt.reporting_revision_id == settings["revision_id"]
                    # The historical public lease has no epoch attribute.
                    # Read its real durable work row instead of assuming a
                    # child's newer dataclass shape or inventing a default.
                    async with pool.connection() as c:
                        identity = await (
                            await c.execute(
                                "SELECT admission_epoch,generation,external_id"
                                " FROM reporting_materializer_work WHERE account_id=%s"
                                " AND consumer_id=%s AND reporting_materialization_id=%s",
                                (
                                    lease.scope.principal.account_id,
                                    lease.scope.consumer_id,
                                    lease.attempt.reporting_materialization_id,
                                ),
                            )
                        ).fetchone()
                    assert identity == (0, lease.generation, lease.request.external_id)
                    return {
                        "point": "pending",
                        "attempt": TypeAdapter(type(lease.attempt)).dump_python(
                            lease.attempt, mode="json"
                        ),
                        "external_id": lease.request.external_id,
                        "generation": lease.generation,
                        "epoch": identity[0],
                        "lease_expires_at": lease.expires_at.isoformat(),
                        "scope": TypeAdapter(type(lease.scope)).dump_python(
                            lease.scope, mode="json"
                        ),
                        "verification_key": asdict(key),
                        "materializer_origin": str(materializer_path),
                        "origin": str(path),
                    }
            raise AssertionError("actual parent did not reserve eligible pending work")
        old = PgStatusNotificationStore(store)
        checkpoints = await old.checkpoints(account_id="acct_a")
        assert checkpoints
        try:
            async with old._transaction("acct_a") as connection:
                await old._write_on(connection, checkpoints[0])
        except Exception as error:
            assert getattr(error, "sqlstate", None) == "23514"
            assert "status_projection_writer_fenced" in str(error)
        else:
            raise AssertionError("historical projector changed a v2 boundary")
        try:
            lease = await old.claim_due(account_id="acct_a")
        except Exception as error:
            # A legacy policy refusal or the actual row trigger is closed.
            assert (
                getattr(error, "sqlstate", None) == "23514"
                or getattr(error, "code", None) == "status_policy_conflict"
            )
            sweeper = "fenced"
        else:
            assert lease is None
            sweeper = "no_mutation"
        try:
            async with pool.connection() as connection, connection.transaction():
                await store._lock_account(connection, "acct_a")
                # Execute the historical reserve algorithm, including its real
                # discovery and selection. Keep the bounded turns in one
                # transaction so the trigger's refusal proves full rollback.
                for _ in range(16):
                    turn = await store._claim_account_on(connection, "acct_a", (key,), 30)
                    assert not hasattr(turn, "token"), "historical worker acquired new work"
                raise AssertionError("historical reservation did not reach the production fence")
        except Exception as error:
            assert getattr(error, "sqlstate", None) == "23514"
            assert "reporting_production_old_worker_fenced" in str(error)
    return {
        "historical_projection": "trigger_fenced",
        "historical_sweeper": sweeper,
        "historical_materializer": "reservation_trigger_fenced",
        "materializer_origin": str(materializer_path),
        "origin": str(path),
    }


if __name__ == "__main__":
    settings = json.loads(sys.stdin.readline())
    print(json.dumps(asyncio.run(main(settings))), flush=True)
    if settings.get("pending"):
        sys.stdin.readline()
        raise AssertionError("historical pending worker must be killed")
