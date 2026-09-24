"""Actual Python 3.10 distributions: mounted page one, SIGKILL, changed state."""

import asyncio
import hashlib
import json
import os
import shutil
import sys
from dataclasses import replace
from datetime import timedelta
from pathlib import Path

import pytest

from adcp.reporting.feed import PgReportingFeedStore
from adcp.reporting.ledger import ReportingMaterializationCheck
from adcp.reporting.ledger.status_projection import mismatch_key

from ._durable_materializer_support import DurableHarness
from ._feed_support import feed_request, mixed_case, second_consumer, walk, without_feed
from ._generation_support import isolated_reporting_pool, require_rolling_database
from ._reconciliation_support import Clock
from .test_reporting_feed_packaging import (
    ROOT,
    b1_wheels,
    built_distribution,
    feed_modules,
    feed_wheels,
    run_step,
)
from .test_reporting_feed_process import feed_process
from .test_reporting_notification_outbox import statement

__all__ = ["b1_wheels", "built_distribution", "feed_wheels"]


@pytest.fixture(scope="module", params=["vcs", "sdist"])
def installed_feed(request):
    require_rolling_database()
    root, wheels, assets = request.getfixturevalue("feed_wheels")
    interpreter = os.environ.get("ADCP_PYTHON310") or sys.executable
    environment = root / f"feed-pg-{request.param}"
    run_step(
        [interpreter, "-m", "venv", str(environment)],
        label=f"feed-{request.param}-pg-environment",
        cwd=root,
    )
    python = environment / "bin/python"
    installer = (
        [shutil.which("uv"), "pip", "install", "--python", str(python)]
        if shutil.which("uv")
        else [str(python), "-m", "pip", "install"]
    )
    run_step(
        [*installer, f"{wheels[request.param]}[pg]", "asgi-lifespan==2.1.0"],
        label=f"feed-{request.param}-pg-install",
        cwd=root,
        timeout=180,
    )
    version = json.loads(
        run_step(
            [str(python), "-I", "-c", "import json,sys;print(json.dumps(sys.version_info[:2]))"],
            label="feed-installed-version",
            cwd=root,
        )
    )
    if os.environ.get("ADCP_PYTHON310"):
        assert version == [3, 10]
    script, helper = root / "feed_process.py", root / "feed_transport.py"
    shutil.copy2(Path(__file__).with_name("_feed_process.py"), script)
    shutil.copy2(Path(__file__).with_name("_receipt_transport.py"), helper)
    installed = {
        "workspace": str(ROOT),
        "python": version,
        "modules": feed_modules(),
        "assets": assets,
        "distribution": request.param,
        "wheel_sha256": hashlib.sha256(wheels[request.param].read_bytes()).hexdigest(),
    }
    return root, python, script, helper, installed


@pytest.mark.parametrize("notifications", [False, True])
async def test_installed_python310_cold_continuation_freezes_mutable_history_and_receipt_replay(
    installed_feed, notifications
):
    root, python, script, helper, installed = installed_feed
    async with isolated_reporting_pool(autocommit=True) as pool:
        ready = json.loads(
            await asyncio.to_thread(
                run_step,
                [str(python), "-I", str(script)],
                label="feed-installed-sql",
                cwd=root,
                value={
                    "conninfo": pool.conninfo,
                    "kwargs": pool.kwargs,
                    "notifications": notifications,
                    "action": "install",
                    "installed": installed,
                },
                timeout=90,
            )
        )
        assert ready["result"] == {
            "installed": True,
            "materializer_objects": 187,
            "receipt_objects": 102,
            "feed_objects": 33,
        }
        h = DurableHarness(
            PgReportingFeedStore(pool=pool, notifications=notifications), Clock(), pool
        )
        s, receipt_request, receipt_response = await mixed_case(
            h, consumer_id="https://buyer.example.test/installed"
        )
        bad = replace(
            statement(s.obligation, s.binding.consumer_id),
            reporting_status_id="installed-consumer-before",
            consumer_status="unreadable",
            reporting_revision_id=s.revision.reporting_revision_id,
            failure_code="access_denied",
        )
        await h.store.record_consumer_status_with_lifecycle(bad)
        config = (await h.store.list_configurations(account_id=s.obligation.account_id))[0]
        await h.store.put_configuration(replace(config, deactivated_at=None))
        options = {"python": python, "script": script, "helper": helper, "installed": installed}
        async with feed_process(h, s, feed_request(s), pause="committed", **options) as child:
            first = (await child.event("committed"))["result"]
            await child.kill()
        original = await h.store.read_reporting_feed_snapshot(
            first["ledger_snapshot_id"], caller=s.binding.principal
        )
        expected = await walk(h.store, feed_request(s), s.binding.principal, first=first)
        issue = await h.store.get_issue(
            account_id=s.obligation.account_id, issue_key=mismatch_key(bad)
        )
        await h.store.set_issue_state(
            account_id=s.obligation.account_id,
            issue_key=issue.issue_key,
            state="waived",
            at=h.clock(),
        )
        await h.store.record_consumer_status_with_lifecycle(
            replace(
                bad,
                reporting_status_id="installed-consumer-after",
                consumer_status="received",
                supersedes_reporting_status_id=bad.reporting_status_id,
                failure_code=None,
                observed_revision_content_sha256=s.revision.revision_content_sha256,
            )
        )
        await h.store.put_configuration(
            replace(
                config,
                deactivated_at=h.clock(),
                status_retention_days=config.status_retention_days + 10,
            )
        )
        await h.store.set_revision_readable(
            account_id=s.obligation.account_id,
            reporting_revision_id=s.revision.reporting_revision_id,
            readable=False,
        )
        await h.store.record_materialization_check(
            ReportingMaterializationCheck(
                s.delivery.scope,
                s.outcome.reporting_materialization_id,
                "installed-corrupt",
                "corrupt",
                h.clock(),
            )
        )
        # Seed the foreign artifact before its source becomes unreadable.
        await h.store.set_revision_readable(
            account_id=s.obligation.account_id,
            reporting_revision_id=s.revision.reporting_revision_id,
            readable=True,
        )
        await second_consumer(h, s, "https://buyer.example.test/foreign-installed")
        await h.store.set_revision_readable(
            account_id=s.obligation.account_id,
            reporting_revision_id=s.revision.reporting_revision_id,
            readable=False,
        )
        h.clock.now += timedelta(days=500)
        await h.store.create_schema()
        before = without_feed(await h.image())
        continuation = feed_request(
            s, pagination={"cursor": first["pagination"]["cursor"], "max_results": 1}
        )
        for v1 in (False, True):
            async with feed_process(
                h, s, continuation, action="walk", transport="a2a", v1=v1, feedback=True, **options
            ) as child:
                done = await child.event("done")
                feed_operation_2 = await asyncio.wait_for(child.process.wait(), 5)
                assert feed_operation_2 == 0
            assert done["result"]["pages"] == expected[0][1:]
            assert done["result"]["binding"] == original.binding
            assert done["result"]["version"] == 1 and done["result"]["ownership_mode"] == "absent"
            assert done["origins"] == ready["origins"]
        assert (
            await h.store.read_reporting_feed_snapshot(
                first["ledger_snapshot_id"], caller=s.binding.principal
            )
            == original
        )
        feed_operation_1 = await h.store.ingest_receipt_batch(
            receipt_request, caller=s.binding.principal
        )
        assert feed_operation_1 == receipt_response
        assert without_feed(await h.image()) == before
        print(
            json.dumps(
                {
                    "feed_installed": installed,
                    "notifications": notifications,
                    "origins": done["origins"],
                    "pages": len(expected[0]),
                    "token_length": len(first["changes_checkpoint"]),
                }
            ),
            flush=True,
        )
