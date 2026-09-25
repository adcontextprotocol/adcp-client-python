"""Eight actual historical installations on the approved B2.1 and B2.2 schemas."""

import asyncio
import json
import shutil
import sys
from datetime import datetime, timedelta, timezone
from importlib.resources import files
from pathlib import Path

import pytest

from adcp.reporting.ledger import (
    ReportingAdjustmentRecord,
    ReportingControlTotalRecord,
    ReportingRevisionReceiptRecord,
)
from adcp.reporting.ledger.delivery import adjustment_to_wire, receipt_to_wire
from adcp.reporting.materializer import PgReportingMaterializerStore
from adcp.reporting.outbox._schema import schema_objects
from adcp.reporting.receipts import PgReportingReceiptStore

from ._durable_materializer_support import DurableHarness, durable_case
from ._generation_support import END, isolated_reporting_pool
from ._provisional_catalog import PROVISIONAL_OBJECTS
from .test_reporting_materializer_process import worker
from .test_reporting_materializer_rolling import ARTIFACTS, build_frozen, frozen_call

B21 = "3fd62121c96a074e3ea458c30c5224d6a586f169"


def receipt_probe(artifact):
    root, python, _, settings = artifact
    script = root / "receipt_frozen.py"
    shutil.copy2(Path(__file__).with_name("_receipt_frozen.py"), script)
    return root, python, script, settings


@pytest.fixture(scope="module")
def installed_b21(tmp_path_factory, request):
    return receipt_probe(build_frozen("b21", tmp_path_factory, request, sha=B21))


@pytest.fixture(scope="module", params=(*ARTIFACTS, "b21"))
def installed_receipt_history(request, tmp_path_factory, installed_b21):
    if request.param == "b21":
        return installed_b21
    return receipt_probe(build_frozen(request.param, tmp_path_factory, request))


async def immutable_parent_rows(pool):
    async with pool.connection() as connection:
        return [
            await (await connection.execute(f"SELECT * FROM {table} ORDER BY 1,2,3")).fetchall()
            for table in (
                "reporting_materializer_work",
                "reporting_materializer_status_boundaries",
                "reporting_materializer_status_heads",
                "reporting_materializer_notification_events",
                "reporting_materializer_notification_expansions",
            )
        ]


async def test_actual_old_readers_and_writers_before_and_after_receipt_migration(
    installed_receipt_history, installed_b21, tmp_path
):
    artifact = installed_receipt_history[3]["artifact"]
    # All eight default-off ordinary writers are required. A's inherited
    # notification-readiness closure after C is compared separately on both
    # sides. The four compatible notification writers also exercise enabled
    # writes; no applicable case is hidden behind a parameter skip.
    for notifications in [False, True] if artifact in {"b", "c", "b1", "b21"} else [False]:
        async with isolated_reporting_pool(autocommit=True) as pool:
            installer = await frozen_call(
                installed_b21, pool, "install", notifications=notifications
            )
            assert installer["manifest_objects"] == 187
            parent = PgReportingMaterializerStore(pool=pool, notifications=notifications)
            case = await durable_case(
                parent,
                count=3,
                finality="official",
                required="official",
                reconciliation_mode="consumer_receipt",
                consumer=(
                    "frozen-buyer"
                    if artifact in {"beta15", "records", "integration", "a", "b"}
                    else "https://buyer.example.test/historical"
                ),
                legacy_definition=artifact == "beta15",
            )
            # This is the actual approved B2.1 installation executing reserve /
            # write / verification / finish / captured boundary / quarantine.
            root, python, _, settings = installed_b21
            process_script = root / "parent_process.py"
            shutil.copy2(Path(__file__).with_name("_materializer_process.py"), process_script)
            destination = tmp_path / f"destination-{notifications}"
            destination.mkdir(mode=0o700)
            installed = {**settings, "python": list(sys.version_info[:2])}
            h = DurableHarness(parent, None, pool)
            async with worker(
                h,
                case,
                destination,
                python=python,
                script=process_script,
                installed=installed,
                notifications=notifications,
            ) as child:
                produced = await child.event("done")
                assert produced["state"] == "verified", produced
                receipt_operation_2 = await asyncio.wait_for(child.process.wait(), 5)
                assert receipt_operation_2 == 0
            outcome = (await case.outcomes())[0]
            assert outcome.verification is not None
            kwargs = {
                "account": case.scope.principal.account_id,
                "consumer": case.scope.principal.consumer_id,
                "obligation": case.obligation.reporting_obligation_id,
                "notifications": notifications,
            }
            before = await frozen_call(
                installed_receipt_history,
                pool,
                "exercise",
                phase="before",
                receipt_count=0,
                **kwargs,
            )
            saved = await immutable_parent_rows(pool)
            queue = await h.queue()
            assert queue[1] == (("quarantined",) if notifications else ())
            async with pool.connection() as c:
                old_objects = await schema_objects(c)
            store = PgReportingReceiptStore(pool=pool, notifications=notifications)
            await store.create_schema()
            assert await store.receipt_ingestion_ready()
            async with pool.connection() as c:
                new_objects = await schema_objects(c)
            assert {key: new_objects[key] for key in old_objects} == old_objects
            receipt_objects = json.loads(
                files("adcp.reporting.receipts").joinpath("required_schema.json").read_text()
            )
            assert len(receipt_objects) == 102
            assert new_objects == {**old_objects, **receipt_objects, **PROVISIONAL_OBJECTS}
            assert await immutable_parent_rows(pool) == saved
            adjustment = ReportingAdjustmentRecord(
                "frozen-adjustment",
                case.config.account_id,
                case.revision.reporting_revision_id,
                "source_correction",
                END,
                END + timedelta(days=30),
                (("spend", "-1.50"),),
                END + timedelta(seconds=5),
                END + timedelta(seconds=6),
                managed_control_total_deltas=(
                    ReportingControlTotalRecord("spend", "-1.50", "decimal", "USD"),
                ),
            )
            await store.commit_adjustment(adjustment)
            verification = outcome.verification
            observed = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
            receipt = ReportingRevisionReceiptRecord(
                case.scope,
                "historical-revision-0001",
                case.revision.reporting_revision_id,
                outcome.reporting_materialization_id,
                "accepted",
                verification.verification_profile,
                verification.row_count,
                verification.control_totals,
                datetime.fromisoformat(observed.replace("Z", "+00:00")),
                observed_canonical_content_digest=verification.canonical_content_digest,
            )
            request = {
                "adcp_version": "3.2-rc.6",
                "account": {"account_id": case.config.account_id},
                "idempotency_key": "historical-mixed-receipts",
                "receipts": [receipt_to_wire(receipt)],
                "adjustment_receipts": [
                    {
                        "reporting_receipt_id": "historical-adjustment-0001",
                        "reporting_adjustment_id": adjustment.reporting_adjustment_id,
                        "adjusts_reporting_revision_id": case.revision.reporting_revision_id,
                        "observed_at": observed,
                        "status": "accepted",
                        "observed_adjustment_sha256": adjustment_to_wire(adjustment)[
                            "canonical_adjustment_sha256"
                        ],
                    }
                ],
            }
            response = await store.ingest_receipt_batch(request, caller=case.scope.principal)
            assert [r["result"] for r in response["results"]] == ["recorded", "recorded"], response
            captured = await store.read_receipt_boundaries(caller=case.scope.principal)
            after = await frozen_call(
                installed_receipt_history,
                pool,
                "exercise",
                phase="after",
                receipt_count=2,
                **kwargs,
            )
            assert before["ordinary_core"] and after["ordinary_core"]
            assert before["notification_readiness"] == after["notification_readiness"]
            assert (
                before["ordinary_materializer"]
                == after["ordinary_materializer"]
                == (artifact != "beta15")
            )
            receipt_operation_1 = await store.ingest_receipt_batch(
                request, caller=case.scope.principal
            )
            assert receipt_operation_1 == response
            assert await store.read_receipt_boundaries(caller=case.scope.principal) == captured
            assert len(captured) == 2 and captured[0].account_sequence > 1
            assert await h.queue() == queue
            assert await immutable_parent_rows(pool) == saved
            assert await parent.materializer_ready()
            print(
                json.dumps(
                    {
                        "receipt_rolling": artifact,
                        "notifications": notifications,
                        "parent": B21,
                        "parent_process_origins": produced["origins"],
                        "before": before,
                        "after": after,
                        "wheel_sha256": installed_receipt_history[3]["wheel_sha256"],
                        "additive_objects": len(new_objects.keys() - old_objects.keys()),
                        "parent_manifest": 187,
                        "receipt_count": 2,
                        "capture_count": len(captured),
                        "quarantine_preserved": True,
                    }
                ),
                flush=True,
            )
