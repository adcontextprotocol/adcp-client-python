"""Installed activation, SIGKILL, and immutable legacy/new representation walks."""

import asyncio
import hashlib
import importlib
import json
import sys
from dataclasses import asdict
from importlib.resources import files
from pathlib import Path
from types import SimpleNamespace


async def main(settings):
    root = Path(settings["fixtures"])
    assert not (root / "src").exists() and not (root / "adcp").exists()
    sys.path.insert(0, str(root))
    assert sys.version_info[:2] == (3, 10)
    origins = {}
    for name, digest in settings["modules"].items():
        path = Path(importlib.import_module(name).__file__).resolve()
        assert path.is_relative_to(Path(sys.prefix))
        assert hashlib.sha256(path.read_bytes()).hexdigest() == digest
        origins[name] = str(path)
    for name, digest in settings["assets"].items():
        assert (
            hashlib.sha256(files("adcp.reporting").joinpath(name).read_bytes()).hexdigest()
            == digest
        )

    from psycopg_pool import AsyncConnectionPool
    from pydantic import TypeAdapter

    from adcp.reporting.ledger import ReportingMaterializationAttempt
    from adcp.reporting.ledger.delivery_models import ReportingDeliveryPrincipal
    from adcp.reporting.ownership import page_revision_ownership
    from tests.conformance.reporting._production_support import production_harness
    from tests.conformance.reporting._production_transport import MountedProduction
    from tests.conformance.reporting._projection_support import drain
    from tests.conformance.reporting.test_reporting_production_lock_order import source_turn

    async with AsyncConnectionPool(
        settings["conninfo"], kwargs=settings["kwargs"], min_size=2, max_size=4, open=False
    ) as pool:
        async with production_harness(
            "postgres",
            Path(settings["destination"]),
            notifications=settings["notifications"],
            count=0,
            reconciled=True,
            identity_prefix="b24-",
            existing_pool=pool,
            adcp_version="3.2-rc.3",
        ) as h:
            mount = MountedProduction(h)
            subject = SimpleNamespace(
                obligation=SimpleNamespace(account_id=settings["caller"]["account_id"]),
                binding=SimpleNamespace(consumer_id=settings["caller"]["consumer_id"]),
            )
            mount.authorize(subject)
            historical = settings["historical_pending"]
            original_attempt = TypeAdapter(ReportingMaterializationAttempt).validate_python(
                historical["attempt"]
            )
            original_id = original_attempt.reporting_materialization_id

            async def pending_identity():
                async with pool.connection() as c:
                    row = await (
                        await c.execute(
                            "SELECT external_id,generation,admission_epoch,state"
                            " FROM reporting_materializer_work"
                            " WHERE account_id=%s AND consumer_id=%s"
                            " AND reporting_materialization_id=%s",
                            (
                                original_attempt.scope.principal.account_id,
                                original_attempt.scope.consumer_id,
                                original_id,
                            ),
                        )
                    ).fetchone()
                assert row[:3] == (
                    historical["external_id"],
                    historical["generation"],
                    historical["epoch"],
                )
                records = await h.store.read_reconciliation_snapshot(
                    caller=original_attempt.scope.principal
                )
                assert original_attempt in records.records
                return row[3]

            if settings["pause"]:
                adoption_operation_1 = await pending_identity() == "pending"
                assert adoption_operation_1
                production_operation_1 = await h.production.activate(account_id="acct_a")
                assert production_operation_1
                # Run the real producer's lease acquisition/release after
                # activation while the actual parent's attempt is pending.
                # The already-killed parent's lease deliberately spans setup.
                # Persist its expiry as a bounded crash-recovery fault, without
                # changing any original attempt, generation or external identity.
                production_operation_2 = await source_turn(h.production)
                assert (production_operation_2).leased is not None
                async with pool.connection() as c, c.transaction():
                    account = original_attempt.scope.principal.account_id
                    await h.store._lock_account(c, account)
                    await c.execute(
                        "UPDATE reporting_materializer_work SET lease_until=clock_timestamp(),"
                        " due_at=clock_timestamp() WHERE account_id=%s AND consumer_id=%s"
                        " AND reporting_materialization_id=%s AND state='pending'",
                        (account, original_attempt.scope.consumer_id, original_id),
                    )
                    await c.execute(
                        "UPDATE reporting_materializer_accounts SET due_at=clock_timestamp()"
                        " WHERE account_id=%s",
                        (account,),
                    )
                for _ in range(40):
                    turn = await h.production.materializer.run_once()
                    if turn.state == "verified":
                        assert turn.reporting_materialization_id == original_id
                        break
                    assert turn.state in {"idle", "discovered", "parked", "pending"}
                    await asyncio.sleep(0.05)
                else:
                    raise AssertionError("historical pending work did not resume to verified")
                assert h.item.writer.writes == 1
                adoption_operation_2 = await pending_identity() == "acked"
                assert adoption_operation_2
                async with pool.connection() as c:
                    rows = await (
                        await c.execute(
                            "SELECT (SELECT count(*)"
                            " FROM reporting_materializer_notification_events"
                            " WHERE reporting_materialization_id=%s AND admission_epoch=0),"
                            " (SELECT count(*)"
                            " FROM reporting_materializer_notification_expansions x"
                            " JOIN reporting_materializer_notification_events e"
                            " USING(account_id,consumer_namespace,notification_id)"
                            " WHERE e.reporting_materialization_id=%s AND x.state='quarantined'),"
                            " (SELECT count(*) FROM reporting_production_notification_events"
                            " WHERE reporting_materialization_id=%s)",
                            (original_id,) * 3,
                        )
                    ).fetchone()
                assert rows == (int(settings["notifications"]), int(settings["notifications"]), 0)
                await drain(h.projection, "acct_a")
            else:
                production_operation_3 = await h.production.activate(account_id="acct_a")
                assert not production_operation_3
                adoption_operation_3 = await pending_identity() == "acked"
                assert adoption_operation_3
                # A new eligible period was committed after both first pages.
                # Finish it through bounded real turns if startup first handled
                # another candidate. Neither snapshot may gain that membership.
                if "new_revision_after_snapshot" in settings:
                    for _ in range(32):
                        outcomes = [
                            r
                            for r in await h.item.outcomes()
                            if r.scope.generation_key == h.item.scope.generation_key
                        ]
                        if any(
                            r.reporting_revision_id == settings["new_revision_after_snapshot"]
                            and r.status == "delivered"
                            for r in outcomes
                        ):
                            break
                        await h.production.materializer.run_once()
                    else:
                        raise AssertionError("new eligible period did not complete after restart")
                    assert {r.reporting_revision_id for r in outcomes} == {
                        h.item.revision.reporting_revision_id,
                        settings["new_revision_after_snapshot"],
                    }
                assert h.item.writer.writes == int("new_revision_after_snapshot" in settings)
            caller = ReportingDeliveryPrincipal(**settings["caller"])
            query = {
                "account": {"account_id": caller.account_id},
                "view": "periods",
                "pagination": {"max_results": 1},
            }

            async def walk(client, request, transport):
                pages = []
                request = json.loads(json.dumps(request))
                for _ in range(1000):
                    _, page = await mount.call(
                        client, "get_reporting_status", request, transport=transport
                    )
                    assert "pagination" in page
                    pages.append(page)
                    if not page["pagination"]["has_more"]:
                        break
                    request["pagination"]["cursor"] = page["pagination"]["cursor"]
                else:
                    raise AssertionError("installed cursor walk exceeded its bound")
                assert len({p["changes_checkpoint"] for p in pages}) == 1
                snapshot = await h.store.read_reporting_feed_snapshot(
                    pages[0]["ledger_snapshot_id"], caller=caller
                )
                return {
                    "pages": pages,
                    "binding": snapshot.binding,
                    "version": snapshot.representation_version,
                    "ownership_mode": snapshot.ownership_mode,
                }

            async with mount.client() as client:
                if settings["pause"]:
                    _, new_first = await mount.call(client, "get_reporting_status", query)
                    assert page_revision_ownership(new_first) is not None
                    expected_new = await walk(
                        client,
                        {
                            **query,
                            "pagination": {
                                "max_results": 1,
                                "cursor": new_first["pagination"]["cursor"],
                            },
                        },
                        "mcp",
                    )
                    print(
                        json.dumps(
                            {
                                "point": "activated",
                                "origins": origins,
                                "first": new_first,
                                "new_remaining": expected_new,
                                "verification_key": asdict(h.item.verifier.key),
                                "pending_continuation": {
                                    "state": "verified",
                                    "epoch": 0,
                                    "external_id": historical["external_id"],
                                    "generation": historical["generation"],
                                    "materialization_id": original_id,
                                    "original_attempt_unchanged": True,
                                    "original_quarantine_preserved": True,
                                    "expiry_control": "persisted expiry after parent SIGKILL",
                                },
                            }
                        ),
                        flush=True,
                    )
                    await asyncio.to_thread(sys.stdin.readline)
                    raise AssertionError("activated process must be killed")
                old_walks = []
                new_walks = []
                for transport in ("mcp", "a2a-0.3", "a2a-1.0"):
                    _, caps = await mount.call(
                        client, "get_adcp_capabilities", {}, transport=transport
                    )
                    assert caps["media_buy"]["reporting_delivery"]["reconciled_billing"] is True
                    old_walks.append(await walk(client, settings["continuation"], transport))
                    new_walks.append(await walk(client, settings["new_continuation"], transport))
                    _, replay = await mount.call(
                        client,
                        "sync_reporting_receipts",
                        settings["receipt_request"],
                        transport=transport,
                    )
                    assert replay == settings["receipt_response"]
                assert old_walks[0] == old_walks[1] == old_walks[2]
                assert new_walks[0] == new_walks[1] == new_walks[2]
                assert all(page_revision_ownership(p) is None for p in old_walks[0]["pages"])
                assert all(page_revision_ownership(p) is not None for p in new_walks[0]["pages"])
                mount.grants.clear()
                h.authorized_bindings.clear()
                _, refused = await mount.call(
                    client, "get_reporting_status", settings["continuation"]
                )
                assert "UNAUTHORIZED" in json.dumps(refused)
            for name, module in tuple(sys.modules.items()):
                if (name == "adcp" or name.startswith("adcp.")) and getattr(
                    module, "__file__", None
                ):
                    assert Path(module.__file__).resolve().is_relative_to(Path(sys.prefix))
            return {
                "point": "done",
                "legacy": old_walks[0],
                "new": new_walks[0],
                "origins": origins,
                "fresh_external_writes": h.item.writer.writes,
            }


if __name__ == "__main__":
    print(json.dumps(asyncio.run(main(json.loads(sys.stdin.readline())))), flush=True)
