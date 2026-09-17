"""Installed historical reader/writer probe, copied out and run with python -I."""

import asyncio
import hashlib
import importlib
import json
import sys
import traceback
from dataclasses import replace
from datetime import datetime, timezone
from importlib.resources import files
from pathlib import Path


async def main(settings):
    from psycopg_pool import AsyncConnectionPool

    import adcp.reporting.ledger as ledger

    workspace = Path(settings["workspace"]).resolve()
    origins = {}
    for name, expected in settings["modules"].items():
        module = importlib.import_module(name)
        path = Path(module.__file__).resolve()
        assert hashlib.sha256(path.read_bytes()).hexdigest() == expected
        assert not path.is_relative_to(workspace) and "site-packages" in str(path)
        origins[name] = str(path)
    assert not any(Path(p).resolve().is_relative_to(workspace) for p in sys.path)
    for name, module in tuple(sys.modules.items()):
        if (name == "adcp" or name.startswith("adcp.")) and getattr(module, "__file__", None):
            assert "site-packages" in module.__file__
            assert not Path(module.__file__).resolve().is_relative_to(workspace)
    async with AsyncConnectionPool(
        settings["conninfo"], kwargs=settings["kwargs"], min_size=1, max_size=2, open=False
    ) as pool:
        if settings["artifact"] == "b22":
            from adcp.reporting.receipts import PgReportingReceiptStore

            store_type = PgReportingReceiptStore
        elif settings["artifact"] == "b21":
            from adcp.reporting.materializer import PgReportingMaterializerStore

            store_type = PgReportingMaterializerStore
        else:
            store_type = getattr(
                ledger, "PgReportingReconciliationStore", ledger.PgReportingLedgerStore
            )
        options = (
            {"notifications": settings["notifications"]}
            if settings["artifact"] not in {"beta15", "records", "integration"}
            else {}
        )
        store = store_type(pool=pool, **options)
        if settings["action"] == "install":
            assert settings["artifact"] == "b21"
            await store.create_schema()
            # C's projection is an independently installed optional component.
            # Use this actual parent binary's reviewed Core composition when
            # preparing the rolling baseline; the materializer migration does
            # not implicitly install or activate C's projector.
            from adcp.reporting.outbox import PgStatusNotificationStore

            await PgStatusNotificationStore(
                ledger.PgReportingReconciliationStore(pool=pool, notifications=True)
            ).create_schema()
            assert await store.materializer_ready()
            manifest = json.loads(
                files("adcp.reporting.materializer").joinpath("required_schema.json").read_text()
            )
            assert len(manifest) == 187
            return {"installed": True, "manifest_objects": len(manifest), "origins": origins}
        account, consumer = settings["account"], settings["consumer"]
        configs = await store.list_configurations(account_id=account)
        assert len(configs) == 1
        obligations = await store.get_obligation(
            account_id=account, reporting_obligation_id=settings["obligation"]
        )
        assert obligations is not None
        revisions = await store.list_revisions(
            account_id=account, reporting_obligation_id=settings["obligation"]
        )
        assert len(revisions) == 1 and revisions[0].finality == "official"
        status, projector_turns = None, 0
        if settings["notifications"] and settings["artifact"] in {"c", "b1", "b21", "b22"}:
            from adcp.reporting.outbox import PgStatusNotificationStore

            status = PgStatusNotificationStore(
                ledger.PgReportingReconciliationStore(pool=pool, notifications=True)
            )
            await status.baseline(account_id=account)

        async def project_ordinary_status():
            nonlocal projector_turns
            if status is not None:
                while (await status.project_one(account_id=account)).did_work:
                    projector_turns += 1
                    assert projector_turns < 32

        # All artifacts execute their own default-off ordinary Core read/write
        # path on the exact parent before and after the additive migration.
        await store.put_configuration(
            replace(configs[0], status_retention_days=configs[0].status_retention_days + 1)
        )
        for readable in (False, True):
            await store.set_revision_readable(
                account_id=account,
                reporting_revision_id=revisions[0].reporting_revision_id,
                readable=readable,
            )
            await project_ordinary_status()
        receipt_count, ordinary_materializer = 0, False
        if hasattr(ledger, "ReportingDeliveryPrincipal"):
            caller = ledger.ReportingDeliveryPrincipal(account, consumer)
            snapshot = await store.read_reconciliation_snapshot(caller=caller)
            available = [
                r for r in snapshot.records if r.kind == "materialization" and r.status != "failed"
            ]
            assert len(available) == 1
            receipts = [
                r for r in snapshot.records if r.kind in {"revision_receipt", "adjustment_receipt"}
            ]
            receipt_count = len(receipts)
            assert receipt_count == settings["receipt_count"]
            if receipt_count:
                assert {r.kind for r in receipts} == {"revision_receipt", "adjustment_receipt"}
                for receipt in receipts:
                    assert (await store.get_receipt(receipt.key)) == receipt
                    assert receipt.status == "accepted" and receipt.received_at is not None
            # Public immutable terminal N+1 compatibility is deliberately
            # separate from autonomous retry allocation. This actual old writer
            # is run in isolation, never alongside an autonomous worker.
            attempts = [r for r in snapshot.records if r.kind == "materialization_attempt"]
            now = datetime.now(timezone.utc)
            attempt = replace(
                attempts[0],
                reporting_materialization_id=f"frozen-{settings['phase']}",
                attempt=len(attempts) + 1,
                created_at=now,
            )
            await store.commit_materialization_attempt(attempt)
            outcome = replace(
                available[0],
                reporting_materialization_id=attempt.reporting_materialization_id,
                status="failed",
                resource=None,
                verification=None,
                failure_code="WRITE_FAILED",
                completed_at=now,
            )
            assert (await store.commit_materialization(outcome))[1]
            assert not (await store.commit_materialization(outcome))[1]
            read = await store.get_materialization(outcome.key)
            assert read is not None and read.outcome == outcome
            ordinary_materializer = True
            await project_ordinary_status()
        readiness = None
        if settings["artifact"] in {"a", "b", "c", "b1", "b21", "b22"}:
            from adcp.reporting.ledger.notification_models import ReportingNotificationError
            from adcp.reporting.outbox._schema import validate_schema

            async with pool.connection() as connection:
                try:
                    await validate_schema(connection)
                    readiness = True
                except ReportingNotificationError:
                    readiness = False
            assert readiness == (settings["artifact"] != "a")
        materializer = None
        if settings["artifact"] in {"b21", "b22"}:
            assert await store.materializer_ready()
            boundaries = await store.read_materializer_boundaries(caller=caller)
            assert len(boundaries) == 1
            assert boundaries[0].to_storage()["version"] == 1
            # The approved boundary decoder is closed: epoch is stored on its
            # referenced work/event rows, not inside the captured input blob.
            async with pool.connection() as connection:
                epoch = await (
                    await connection.execute(
                        "SELECT admission_epoch,state FROM reporting_materializer_work"
                        " WHERE account_id=%s AND consumer_id=%s"
                        " AND reporting_materialization_id=%s",
                        (account, consumer, boundaries[0].reporting_materialization_id),
                    )
                ).fetchone()
            assert epoch == (0, "acked")
            materializer = len(
                json.loads(
                    files("adcp.reporting.materializer")
                    .joinpath("required_schema.json")
                    .read_text()
                )
            )
            assert materializer == 187
        workers = {}
        if settings["notifications"]:
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
                    raise AssertionError("empty membership has no HTTP delivery")

            outboxes = {"ordinary": PgReportingOutbox(pool=pool)}
            if status is not None:
                outboxes["status"] = status.outbox
            for name, outbox in outboxes.items():
                events = await outbox.list_events(account_id=account)
                assert events and all(
                    event.notification_type != "reporting.delivery_ready" for event in events
                )
                identities = {
                    (e.account_id, e.consumer_namespace, e.notification_id): e for e in events
                }
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
                while await worker.expand_one(account_id=account):
                    assert len(seen) <= len(events)
                assert not await worker.deliver_one(account_id=account)
                assert await outbox.list_events(account_id=account) == events
                workers[name] = len(seen)
            if settings["phase"] == "before":
                assert workers["ordinary"] > 0  # An actual old Core worker claimed real work.
            if status is not None:
                assert projector_turns > 0 and workers["status"] > 0
        return {
            "ordinary_core": True,
            "ordinary_materializer": ordinary_materializer,
            "receipt_count": receipt_count,
            "notification_readiness": readiness,
            "materializer_manifest": materializer,
            "projector_turns": projector_turns,
            "workers": workers,
            "origins": origins,
            "sha": settings["sha"],
            "artifact": settings["artifact"],
        }


if __name__ == "__main__":
    try:
        result = asyncio.run(main(json.load(sys.stdin)))
    except Exception as error:
        result = {
            "failure": type(error).__name__,
            "safe_code": (
                error.code
                if type(error).__module__ == "adcp.reporting.ledger.notification_models"
                and type(error).__name__ == "ReportingNotificationError"
                else None
            ),
            "frames": [
                [Path(frame.filename).name, frame.lineno]
                for frame in traceback.extract_tb(error.__traceback__)
            ],
        }
    print(json.dumps(result))
