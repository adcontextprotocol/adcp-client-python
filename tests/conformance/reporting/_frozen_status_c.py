"""JSON-line control for the exact reviewed C artifact, imported with python -I.

This file deliberately imports no workspace SDK before choosing the frozen
source. It is a test driver, not a production migration or recovery service.
"""

import asyncio
import json
import sys
from dataclasses import asdict
from datetime import datetime
from pathlib import Path


def emit(value):
    print(json.dumps(value, default=lambda v: v.isoformat()), flush=True)


async def read():
    return json.loads(await asyncio.to_thread(sys.stdin.readline))


async def main():
    config = await read()
    root = Path(config["source"]).resolve()
    sys.path.insert(0, str(root / "src"))
    from psycopg import AsyncConnection, Error
    from psycopg_pool import AsyncConnectionPool

    import adcp.reporting.outbox.status_pg as module
    from adcp.reporting.ledger import PgReportingReconciliationStore
    from adcp.reporting.ledger.notification_models import ReportingNotificationError
    from adcp.reporting.outbox import PgStatusNotificationStore

    assert Path(module.__file__).is_relative_to(root)
    gate = None
    now = datetime.fromisoformat(config["now"])

    class HeldConnection(AsyncConnection):
        async def execute(self, query, params=None, **kwargs):
            nonlocal gate
            result = await super().execute(query, params, **kwargs)
            if (
                gate
                and isinstance(query, str)
                and query.startswith("UPDATE reporting_status_scope_checkpoints SET")
            ):
                if gate == "project" or "SET lease_token=" in query:
                    gate = None
                    emit({"held": True})
                    assert (await read())["action"] == "release_hold"
            return result

    async with AsyncConnectionPool(
        config["conninfo"],
        kwargs=config["kwargs"],
        min_size=1,
        max_size=1,
        connection_class=HeldConnection,
        open=False,
    ) as pool:
        ledger = PgReportingReconciliationStore(pool=pool, notifications=True, clock=lambda: now)
        status = PgStatusNotificationStore(ledger)
        leases = []
        emit({"origin": str(module.__file__)})
        while True:
            command = await read()
            action = command["action"]
            account = command.get("account", "acct_a")
            if "now" in command:
                now = datetime.fromisoformat(command["now"])
            gate = command.get("hold")
            try:
                if action == "stop":
                    emit({"stopped": True})
                    return
                if action == "schema":
                    await status.create_schema()
                    result = True
                elif action == "baseline":
                    result = await status.baseline(account_id=account)
                elif action == "ready":
                    result = await status.baseline_ready(account_id=account)
                elif action == "project":
                    result = asdict(await status.project_one(account_id=account))
                elif action == "claim":
                    lease = await status.claim_due(account_id=account, lease_seconds=60)
                    leases.append(lease)
                    result = None if lease is None else asdict(lease)
                elif action == "complete":
                    result = asdict(await status.complete_due(leases[command.get("lease", -1)]))
                elif action == "release":
                    result = await status.release_due(leases[command.get("lease", -1)])
                elif action == "source":
                    await ledger.set_revision_readable(
                        account_id=account,
                        reporting_revision_id=command["revision"],
                        readable=command["readable"],
                    )
                    result = True
                else:
                    raise AssertionError("unknown test command")
                emit({"result": result})
            except ReportingNotificationError as exc:
                emit({"error": exc.code})
            except Error as exc:
                emit({"error": "database_fence", "sqlstate": exc.sqlstate})


if __name__ == "__main__":
    asyncio.run(main())
