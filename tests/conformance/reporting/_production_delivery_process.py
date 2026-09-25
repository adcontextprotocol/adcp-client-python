"""Real SIGKILL after receiver acceptance; cold retry uses the durable deadline."""

import asyncio
import hashlib
import json
import sys
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace


async def main(settings):
    import pytest
    from psycopg_pool import AsyncConnectionPool

    import adcp.reporting.production.delivery_window as window_module
    from adcp.reporting.outbox.routing import ReportingEnvelopeCipher
    from adcp.reporting.production.notifications import (
        ReportingProductionSigning,
        production_notification_workers,
    )
    from adcp.reporting.production.pg import PgReportingProductionStore
    from adcp.reporting.projection.pg import PgReportingStatusProjection

    from ._reliable_support import (
        Barrier,
        DeterministicReceiverStore,
        FailurePlan,
        ManualClock,
        ScriptedNotificationReceiver,
        ScriptedSigning,
        ScriptedSubscriptions,
        _BytesStore,
        notification_subscription,
    )
    from .test_reporting_production_notifications import EVENTS, retained_windows

    origin = Path(window_module.__file__).resolve()
    assert hashlib.sha256(origin.read_bytes()).hexdigest() == settings["module_sha256"]
    if settings["installed"]:
        assert origin.is_relative_to(Path(sys.prefix)) and "site-packages" in str(origin)
    clock = ManualClock(datetime.fromisoformat(settings["at"]))
    async with AsyncConnectionPool(
        settings["conninfo"], kwargs=settings["kwargs"], min_size=2, max_size=4, open=False
    ) as pool:
        store = PgReportingProductionStore(pool=pool, clock=clock, notifications=True)
        await store.create_schema()
        projection = PgReportingStatusProjection(store, revision_ownership=True)
        failures = FailurePlan()
        subscriptions = ScriptedSubscriptions(failures)
        subscriptions.put(
            notification_subscription(
                subscriber="buyer",
                principal=settings["consumer"],
                events=EVENTS,
                url="https://receiver.example.test/reporting",
            )
        )
        resolver = ScriptedSigning(failures)
        resolver.generation = 1 if settings["pause"] else 2
        signing = ReportingProductionSigning(
            resolver, ("ed25519",), brand_json_url="https://seller.example.test/brand.json"
        )
        worker = production_notification_workers(
            store,
            projection,
            subscriptions=subscriptions,
            cipher=ReportingEnvelopeCipher(b"b" * 32),
            signing=signing,
        )[settings["queue"]]
        blobs = _BytesStore(pool)
        await blobs.create_schema()
        received = DeterministicReceiverStore(blobs, failures)
        receiver = ScriptedNotificationReceiver(
            SimpleNamespace(clock=clock, failures=failures, receiver=received)
        )
        with pytest.MonkeyPatch.context() as patch:
            receiver.install(patch)
            if settings["pause"]:
                accepted = Barrier()
                failures.at("http.accepted", accepted)
                task = asyncio.create_task(worker.deliver_one(account_id="acct_a"))
                await accepted.wait()
                windows = await retained_windows(SimpleNamespace(pool=pool))
                assert len(windows) == 1
                row = windows[0]
                print(
                    json.dumps(
                        {
                            "point": "accepted_before_ack",
                            "started_at": row[4].isoformat(),
                            "expires_at": row[5].isoformat(),
                            "key_sha256": hashlib.sha256(row[1].encode()).hexdigest(),
                            "body_sha256": row[3],
                            "origin": str(origin),
                        }
                    ),
                    flush=True,
                )
                await asyncio.to_thread(sys.stdin.readline)
                accepted.release()
                await task
                raise AssertionError("paused child must be killed")
            before = await retained_windows(SimpleNamespace(pool=pool))
            production_operation_1 = await worker.deliver_one(account_id="acct_a")
            assert production_operation_1
            assert await retained_windows(SimpleNamespace(pool=pool)) == before
            row = before[0]
            states = await worker.outbox.list_deliveries(account_id="acct_a")
            target = next(s for s in states if s.delivery.binding.idempotency_key == row[1])
            body = await received.read("acct_a", row[1])
            assert body is not None and hashlib.sha256(body).hexdigest() == row[3]
            attempts = await worker.outbox.list_activity(
                account_id="acct_a", consumer_id=settings["consumer"]
            )
            result = {
                "point": "done",
                "http_calls": len(receiver.received),
                "state": target.state,
                "error_code": target.error_code,
                "activity_count": len(attempts),
                "key_sha256": hashlib.sha256(row[1].encode()).hexdigest(),
                "body_sha256": row[3],
                "started_at": row[4].isoformat(),
                "expires_at": row[5].isoformat(),
                "origin": str(origin),
            }
            if receiver.received:
                assert "key-2" in receiver.received[0].headers["signature-input"]
            return result


if __name__ == "__main__":
    print(json.dumps(asyncio.run(main(json.loads(sys.stdin.readline())))), flush=True)
