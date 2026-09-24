"""Copied out of the checkout and run with -I in an installed frozen wheel."""

import asyncio
import hashlib
import importlib
import json
import sys
import traceback
from dataclasses import replace
from pathlib import Path


async def main(settings):
    from psycopg_pool import AsyncConnectionPool

    import adcp.reporting.ledger as ledger_module
    from adcp.reporting.ledger import PgReportingLedgerStore

    origins = {}
    for name, expected in settings["modules"].items():
        module = importlib.import_module(name)
        assert hashlib.sha256(Path(module.__file__).read_bytes()).hexdigest() == expected
        origins[name] = str(Path(module.__file__).resolve())
    workspace = Path(settings["workspace"]).resolve()
    assert not any(Path(p).resolve().is_relative_to(workspace) for p in sys.path)
    for name, module in tuple(sys.modules.items()):
        if name == "adcp" or name.startswith("adcp."):
            if getattr(module, "__file__", None):
                assert not Path(module.__file__).resolve().is_relative_to(workspace)
                assert "site-packages" in module.__file__
    has_records = hasattr(ledger_module, "PgReportingReconciliationStore")
    store_type = (
        ledger_module.PgReportingReconciliationStore if has_records else PgReportingLedgerStore
    )
    async with AsyncConnectionPool(
        settings["conninfo"], kwargs=settings["kwargs"], min_size=2, max_size=4, open=False
    ) as pool:
        async with pool.connection() as connection:
            database = await (
                await connection.execute(
                    "SELECT current_setting('server_version_num')::int,"
                    " current_setting('server_encoding'),datcollate,datctype"
                    " FROM pg_database WHERE datname=current_database()"
                )
            ).fetchone()
        assert 160000 <= database[0] < 170000 and database[1] == "UTF8"
        store = store_type(pool=pool)
        if settings["action"] == "install":
            await store.create_schema()
            if settings["artifact"] in {"c", "b1"}:
                from adcp.reporting.outbox import PgStatusNotificationStore

                await PgStatusNotificationStore(
                    store_type(pool=pool, notifications=True)
                ).create_schema()
            if settings["artifact"] in {"a", "b", "c", "b1"}:
                from adcp.reporting.outbox._schema import validate_schema

                async with pool.connection() as connection:
                    await validate_schema(connection)
            return {
                "artifact": settings["artifact"],
                "sha": settings["sha"],
                "installed": True,
                "origins": origins,
                "module_hashes": settings["modules"],
                "database": database,
                "notifications_ready": (
                    True if settings["artifact"] in {"a", "b", "c", "b1"} else None
                ),
            }

        notifications_ready = None
        if settings["artifact"] in {"a", "b", "c", "b1"}:
            from adcp.reporting.ledger.notification_models import ReportingNotificationError
            from adcp.reporting.outbox._schema import validate_schema

            async with pool.connection() as connection:
                try:
                    await validate_schema(connection)
                    notifications_ready = True
                except ReportingNotificationError:
                    notifications_ready = False
            # Before AND after B2: exactly the reviewed C/B1 envelope. A's
            # aggregate closes; B/C/B1 per-object required manifests stay ready.
            assert notifications_ready == (settings["artifact"] != "a")

        configs = await store.list_configurations(account_id="acct_a")
        assert len(configs) == 1
        obligation = await store.get_obligation(
            account_id="acct_a", reporting_obligation_id="rpo_acct_a"
        )
        assert obligation is not None
        revisions = await store.list_revisions(
            account_id="acct_a", reporting_obligation_id=obligation.reporting_obligation_id
        )
        assert len(revisions) == 1 and revisions[0].row_count == 3
        snapshot = await store.open_snapshot(account_id="acct_a", filters_fingerprint="frozen-b2")
        page = await store.read_page(
            snapshot=snapshot,
            consumer_id=settings["consumer"],
            delivery_config_ids=None,
            media_buy_ids=None,
            offset=0,
            limit=100,
            changes_after_sequence=None,
        )
        assert len(page.obligations) == len(page.revisions) == 1
        assert not page.has_more
        records = 0
        if has_records:
            principal = ledger_module.ReportingDeliveryPrincipal("acct_a", settings["consumer"])
            retained = await store.read_reconciliation_snapshot(caller=principal)
            assert len(retained.records) == (1 if settings["action"] == "baseline" else 4)
            assert [r.status for r in retained.records if r.kind == "materialization"] == [
                "available"
            ] * (settings["action"] != "baseline")
            records = len(retained.records)
        # Permitted ordinary old writes remain functional; legacy materializer
        # writers are deliberately drained, not run beside the new worker.
        status = None
        projector_turns = 0
        if settings["artifact"] in {"c", "b1"} and settings["action"] != "baseline":
            from adcp.reporting.outbox import PgStatusNotificationStore

            store = store_type(pool=pool, notifications=True)
            status = PgStatusNotificationStore(store)
            await status.baseline(account_id="acct_a")
        await store.put_configuration(
            replace(configs[0], status_retention_days=configs[0].status_retention_days + 1)
        )
        for readable in (False, True):
            await store.set_revision_readable(
                account_id="acct_a",
                reporting_revision_id=revisions[0].reporting_revision_id,
                readable=readable,
            )
            if status is not None:
                while (await status.project_one(account_id="acct_a")).did_work:
                    projector_turns += 1
                    assert projector_turns < 32
        assert (
            await store.get_revision(
                account_id="acct_a", reporting_revision_id=revisions[0].reporting_revision_id
            )
        ).readable
        workers = {}
        event_identities = {}
        if settings["artifact"] in {"a", "b", "c", "b1"} and settings["action"] != "baseline":
            from adcp.reporting.ledger.notification_models import decode_event
            from adcp.reporting.outbox import (
                PgReportingOutbox,
                ReportingEnvelopeCipher,
                ReportingNotificationWorker,
            )

            class EmptySubscriptions:
                async def list_active(self, **kwargs):
                    assert kwargs["notification_type"] != "reporting.delivery_ready"
                    return ()

                async def get_active(self, **kwargs):
                    raise AssertionError("empty recipient membership has no HTTP delivery")

            outboxes = {"ordinary": PgReportingOutbox(pool=pool)}
            if settings["artifact"] in {"c", "b1"}:
                from adcp.reporting.outbox.status_pg import PgReportingStatusOutbox

                outboxes["status"] = PgReportingStatusOutbox(pool=pool)
            for name, outbox in outboxes.items():
                events = await outbox.list_events(account_id="acct_a")
                if name == "ordinary":
                    assert len(events) == 1
                    event = events[0]
                    assert event.account_id == "acct_a" and event.consumer_namespace == ""
                    assert event.notification_type == "reporting.ledger_changed"
                    assert event.cause.kind == "revision_published"
                    assert event.cause.reporting_revision_id == "revision-acct_a"
                    assert event.cause.finality == "snapshot" and event.cause_generation == 1
                    assert event.cause.supersedes_reporting_revision_id is None
                else:
                    # Six known Core views (configuration + obligation for the
                    # seller and two callers), each complete -> action -> complete
                    # for this closed period with snapshot-required finality.
                    # Materialization captures add no C reconciliation semantics.
                    expected = {
                        (
                            ("acct_a", consumer, "daily", 1, kind, obligation_id),
                            generation,
                            previous,
                            health,
                        )
                        for consumer in (
                            "",
                            settings["consumer"],
                            "https://buyer.example.test/isolated",
                        )
                        for kind, obligation_id in (
                            ("configuration", ""),
                            ("obligation", "rpo_acct_a"),
                        )
                        for generation, previous, health in (
                            (1, "complete", "action_required"),
                            (2, "action_required", "complete"),
                        )
                    }
                    assert len(events) == len(expected) == 12
                    assert all(
                        e.notification_type == "reporting.status_changed"
                        and e.cause.kind == "status_changed"
                        and e.cause_generation == e.cause.checkpoint_generation
                        and len(e.cause.issue_ids) == int(e.cause.health == "action_required")
                        for e in events
                    )
                    assert {
                        (
                            e.cause.scope.checkpoint_key,
                            e.cause_generation,
                            e.cause.previous_health,
                            e.cause.health,
                        )
                        for e in events
                    } == expected
                assert len({e.causal_key for e in events}) == len(events)
                identities = {
                    (e.account_id, e.consumer_namespace, e.notification_id): e for e in events
                }
                assert len(identities) == len(events)
                seen = set()

                class ObservedOutbox:
                    def __getattr__(self, attribute):
                        return getattr(outbox, attribute)

                    async def claim_expansion(self, **kwargs):
                        lease = await outbox.claim_expansion(**kwargs)
                        if lease is not None:
                            identity = (
                                lease.account_id,
                                lease.consumer_namespace,
                                lease.notification_id,
                            )
                            assert identity in identities and identity not in seen
                            assert decode_event(lease.event) == identities[identity]
                            seen.add(identity)
                        return lease

                worker = ReportingNotificationWorker(
                    outbox=ObservedOutbox(),
                    subscriptions=EmptySubscriptions(),
                    cipher=ReportingEnvelopeCipher(b"e" * 32),
                )
                for _ in events:
                    materializer_operation_3 = await worker.expand_one(account_id="acct_a")
                    assert materializer_operation_3
                assert seen == identities.keys()
                materializer_operation_1 = await worker.deliver_one(account_id="acct_a")
                assert not materializer_operation_1
                materializer_operation_2 = await worker.expand_one(account_id="acct_a")
                assert not materializer_operation_2
                assert await outbox.list_events(account_id="acct_a") == events
                workers[name] = len(seen)
                event_identities[name] = [
                    {"notification_id": e.notification_id, "causal_key": e.causal_key}
                    for e in events
                ]
            assert workers["ordinary"] == 1  # Positive control: an actual Core event was claimed.
            if status is not None:
                assert projector_turns > 0 and workers["status"] > 0
        return {
            "artifact": settings["artifact"],
            "sha": settings["sha"],
            "installed": True,
            "core_records": 2,
            "managed_records": records,
            "ordinary_writes": True,
            "workers": workers,
            "event_identities": event_identities,
            "projector_turns": projector_turns,
            "notifications_ready": notifications_ready,
            "origins": origins,
            "module_hashes": settings["modules"],
            "database": database,
        }


if __name__ == "__main__":
    try:
        result = asyncio.run(main(json.load(sys.stdin)))
    except Exception as error:
        result = {
            "failure": type(error).__name__,
            "frames": [
                [Path(frame.filename).name, frame.lineno]
                for frame in traceback.extract_tb(error.__traceback__)
            ],
        }
    print(json.dumps(result))
