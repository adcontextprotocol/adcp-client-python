"""Nine actual historical binaries on exact B2.2 then the additive feed schema."""

import asyncio
import hashlib
import json
import shutil
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from adcp.reporting.feed import PgReportingFeedStore
from adcp.reporting.ledger import (
    ReportingControlTotalRecord,
    ReportingDeliveryScope,
    ReportingRevisionReceiptRecord,
)
from adcp.reporting.ledger.delivery import receipt_to_wire
from adcp.reporting.materializer import PgReportingMaterializerStore
from adcp.reporting.outbox._schema import schema_objects
from adcp.reporting.receipts import PgReportingReceiptStore

from ._durable_materializer_support import DurableHarness, durable_case
from ._feed_support import walk, without_feed
from ._generation_support import isolated_reporting_pool
from ._receipt_support import adjustment_for
from .test_reporting_feed_packaging import ROOT, run_step
from .test_reporting_materializer_process import worker
from .test_reporting_materializer_rolling import ARTIFACTS, build_frozen, frozen_call
from .test_reporting_receipt_rolling import B21, immutable_parent_rows, receipt_probe

B22 = "09fd87f79a746665d828dea66b3a1dd9d1fc189e"
B22_TREE = "76c64bb94d8b92faed644158317bb5372b64fcb9"


@pytest.fixture(scope="module")
def approved_feed_b21(tmp_path_factory, request):
    return receipt_probe(build_frozen("b21", tmp_path_factory, request, sha=B21))


@pytest.fixture(scope="module")
def approved_feed_b22(tmp_path_factory, request):
    root, python, ordinary, original = receipt_probe(
        build_frozen("b22", tmp_path_factory, request, sha=B22)
    )
    modules = dict(original["modules"])
    for relative in (
        "reporting/receipts/pg.py",
        "reporting/receipts/handler.py",
        "reporting/receipts/wire.py",
        "reporting/receipts/transport.py",
        "server/mcp_tools.py",
        "server/a2a_server.py",
        "server/serve.py",
    ):
        content = run_step(
            ["git", "show", f"{B22}:src/adcp/{relative}"], label="b22-exact-module", cwd=ROOT
        )
        modules["adcp." + relative.removesuffix(".py").replace("/", ".")] = hashlib.sha256(
            content.encode()
        ).hexdigest()
    installer = (
        [shutil.which("uv"), "pip", "install", "--python", str(python)]
        if shutil.which("uv")
        else [str(python), "-m", "pip", "install"]
    )
    run_step([*installer, "asgi-lifespan==2.1.0"], label="b22-mounted-probe-dependency", cwd=root)
    shutil.copy2(Path(__file__).with_name("_feed_process.py"), root / "feed_process.py")
    shutil.copy2(Path(__file__).with_name("_receipt_transport.py"), root / "receipt_transport.py")
    return root, python, ordinary, {**original, "modules": modules, "tree": B22_TREE}


@pytest.fixture(scope="module", params=(*ARTIFACTS, "b21", "b22"))
def installed_feed_history(request, tmp_path_factory, approved_feed_b21, approved_feed_b22):
    if request.param == "b21":
        return approved_feed_b21
    if request.param == "b22":
        return approved_feed_b22
    return receipt_probe(build_frozen(request.param, tmp_path_factory, request))


async def b22_mounted(artifact, pool, action, **kwargs):
    root, python, _, settings = artifact
    value = {
        "installed": settings,
        "receipt_only": True,
        "conninfo": pool.conninfo,
        "kwargs": pool.kwargs,
        "action": action,
        "helper": str(root / "receipt_transport.py"),
        **kwargs,
    }
    result = json.loads(
        await asyncio.to_thread(
            run_step,
            [str(python), "-I", str(root / "feed_process.py")],
            label=f"frozen-b22-mounted-{action}",
            cwd=root,
            value=value,
            timeout=90,
        )
    )
    assert result["point"] == "done", result
    return result


async def other_artifact_consumer(parent, case, outcome):
    consumer = "https://buyer.example.test/post-migration"
    scope = ReportingDeliveryScope(
        case.config.generation_key, consumer, case.obligation.reporting_obligation_id
    )
    snapshot = await parent.read_reconciliation_snapshot(caller=case.scope.principal)
    delivery = next(r for r in snapshot.records if r.kind == "obligation_delivery")
    attempt = next(
        r
        for r in snapshot.records
        if r.kind == "materialization_attempt"
        and r.reporting_materialization_id == outcome.reporting_materialization_id
    )
    await parent.put_destination_binding(replace(case.binding, consumer_id=consumer))
    await parent.bind_obligation_delivery(replace(delivery, scope=scope))
    await parent.commit_materialization_attempt(replace(attempt, scope=scope))
    await parent.commit_materialization(replace(outcome, scope=scope))
    return scope.principal


async def test_nine_actual_artifacts_preserve_ordinary_writes_and_frozen_b22_mounted_replay(
    installed_feed_history, approved_feed_b21, approved_feed_b22, tmp_path
):
    artifact = installed_feed_history[3]["artifact"]
    modes = [False, True] if artifact in {"b", "c", "b1", "b21", "b22"} else [False]
    for notifications in modes:
        async with isolated_reporting_pool(autocommit=True) as pool:
            installed = await b22_mounted(
                approved_feed_b22,
                pool,
                "install",
                notifications=notifications,
                legacy_status_schema=True,
            )
            assert installed["result"] == {
                "installed": True,
                "materializer_objects": 187,
                "receipt_objects": 102,
            }
            parent = PgReportingReceiptStore(pool=pool, notifications=notifications)
            case = await durable_case(
                parent,
                count=3,
                finality="official",
                required="official",
                reconciliation_mode="consumer_receipt",
                consumer=(
                    "frozen-buyer"
                    if artifact in {"beta15", "records", "integration", "a", "b"}
                    else "https://buyer.example.test/frozen-feed"
                ),
                legacy_definition=artifact == "beta15",
            )
            root, python, _, settings = approved_feed_b21
            process_script = root / "feed_parent_materializer.py"
            shutil.copy2(Path(__file__).with_name("_materializer_process.py"), process_script)
            destination = tmp_path / f"destination-{notifications}"
            destination.mkdir(mode=0o700)
            h = DurableHarness(parent, None, pool)
            async with worker(
                h,
                case,
                destination,
                python=python,
                script=process_script,
                installed={**settings, "python": list(sys.version_info[:2])},
                notifications=notifications,
            ) as child:
                produced = await child.event("done")
                assert produced["state"] == "verified"
                feed_operation_3 = await asyncio.wait_for(child.process.wait(), 5)
                assert feed_operation_3 == 0
            outcome = (await case.outcomes())[0]
            second = await other_artifact_consumer(parent, case, outcome)
            evidence = outcome.verification
            receipt = ReportingRevisionReceiptRecord(
                case.scope,
                "frozen-feed-revision-0001",
                case.revision.reporting_revision_id,
                outcome.reporting_materialization_id,
                "accepted",
                evidence.verification_profile,
                evidence.row_count,
                evidence.control_totals,
                outcome.completed_at,
                observed_canonical_content_digest=evidence.canonical_content_digest,
            )
            adjustment = await adjustment_for(
                h,
                case,
                managed_control_total_deltas=(
                    ReportingControlTotalRecord(
                        "spend", "-1.50", "decimal", case.obligation.currency
                    ),
                ),
            )
            request = {
                "adcp_version": "3.2-rc.6",
                "account": {"account_id": case.config.account_id},
                "idempotency_key": "frozen-feed-mixed-batch",
                "receipts": [receipt_to_wire(receipt)],
                "adjustment_receipts": [adjustment],
            }
            caller = {"account_id": case.config.account_id, "consumer_id": case.binding.consumer_id}
            admitted = await b22_mounted(
                approved_feed_b22,
                pool,
                "receipt",
                notifications=notifications,
                caller=caller,
                request=request,
            )
            assert [r["result"] for r in admitted["result"]["results"]] == ["recorded", "recorded"]
            captures = await parent.read_receipt_boundaries(caller=case.scope.principal)
            kwargs = {
                "account": case.config.account_id,
                "consumer": case.binding.consumer_id,
                "obligation": case.obligation.reporting_obligation_id,
                "notifications": notifications,
                "receipt_count": 2,
            }
            before = await frozen_call(
                installed_feed_history, pool, "exercise", phase="before", **kwargs
            )
            saved = await immutable_parent_rows(pool)
            queue = await h.queue()
            async with pool.connection() as c:
                old_objects = await schema_objects(c)
            feed = PgReportingFeedStore(pool=pool, notifications=notifications)
            await feed.create_schema()
            async with pool.connection() as c:
                new_objects = await schema_objects(c)
            assert {k: new_objects[k] for k in old_objects} == old_objects
            added = new_objects.keys() - old_objects.keys()
            assert len(added) == 33 and all("reporting_feed_" in k for k in added)
            query = {
                "account": request["account"],
                "view": "periods",
                "pagination": {"max_results": 1},
            }
            first = await feed.read_reporting_feed(query, caller=case.scope.principal)
            expected = await walk(feed, query, case.scope.principal, first=first)
            frozen = await feed.read_reporting_feed_snapshot(
                first["ledger_snapshot_id"], caller=case.scope.principal
            )
            after = await frozen_call(
                installed_feed_history, pool, "exercise", phase="after", **kwargs
            )
            replayed = await b22_mounted(
                approved_feed_b22,
                pool,
                "receipt",
                notifications=notifications,
                caller=caller,
                request=request,
            )
            assert replayed["result"] == admitted["result"]
            # Actual B2.2 binary admits the same IDs/key for another canonical
            # consumer after migration, while the original walk is still open.
            second_result = await b22_mounted(
                approved_feed_b22,
                pool,
                "receipt",
                notifications=notifications,
                caller={"account_id": second.account_id, "consumer_id": second.consumer_id},
                request=request,
            )
            assert [r["result"] for r in second_result["result"]["results"]] == [
                "recorded",
                "recorded",
            ]
            assert second_result["result"] != admitted["result"]
            image = without_feed(await h.image())
            fresh_reader = PgReportingFeedStore(pool=pool, notifications=notifications)
            feed_operation_1 = await walk(fresh_reader, query, case.scope.principal, first=first)
            assert feed_operation_1 == expected
            assert (
                await fresh_reader.read_reporting_feed_snapshot(
                    frozen.snapshot_id, caller=case.scope.principal
                )
                == frozen
            )
            assert without_feed(await h.image()) == image
            assert await parent.read_receipt_boundaries(caller=case.scope.principal) == captures
            assert await immutable_parent_rows(pool) == saved
            assert await h.queue() == queue
            feed_operation_2 = await PgReportingMaterializerStore(
                pool=pool, notifications=notifications
            ).materializer_ready()
            assert feed_operation_2
            assert before["ordinary_core"] and after["ordinary_core"]
            assert (
                before["ordinary_materializer"]
                == after["ordinary_materializer"]
                == (artifact != "beta15")
            )
            assert before["notification_readiness"] == after["notification_readiness"]
            print(
                json.dumps(
                    {
                        "feed_rolling": artifact,
                        "notifications": notifications,
                        "parent_head": B22,
                        "parent_tree": B22_TREE,
                        "b21_producer_origins": produced["origins"],
                        "before": before,
                        "after": after,
                        "b22_mounted_origins": replayed["origins"],
                        "historical_wheel_sha256": installed_feed_history[3]["wheel_sha256"],
                        "b22_wheel_sha256": approved_feed_b22[3]["wheel_sha256"],
                        "feed_objects": len(added),
                        "page_count": len(expected[0]),
                        "quarantine_preserved": True,
                    }
                ),
                flush=True,
            )
