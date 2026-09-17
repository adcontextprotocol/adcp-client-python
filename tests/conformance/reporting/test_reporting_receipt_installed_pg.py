"""Installed VCS/sdist Python 3.10 receipts through real transports after SIGKILL."""

import asyncio
import hashlib
import json
import shutil
from pathlib import Path

import pytest

from adcp.reporting.ledger.delivery import receipt_to_wire
from adcp.reporting.receipts import PgReportingReceiptStore

from ._durable_materializer_support import DurableHarness
from ._generation_support import isolated_reporting_pool
from ._receipt_support import adjustment_for, receipt_case, request_for
from ._reconciliation_support import Clock
from .test_reporting_materializer_installed_pg import installed_materializer
from .test_reporting_materializer_packaging import ROOT, b1_wheels, built_distribution, run_step
from .test_reporting_receipt_process import worker

__all__ = ["installed_materializer", "b1_wheels", "built_distribution"]


@pytest.fixture(scope="module")
def installed_receipts(installed_materializer):
    root, python, _, original = installed_materializer
    installer = (
        [shutil.which("uv"), "pip", "install", "--python", str(python)]
        if shutil.which("uv")
        else [str(python), "-m", "pip", "install"]
    )
    run_step(
        [*installer, "asgi-lifespan==2.1.0"],
        label="receipt-installed-transport-test-lifespan",
        cwd=root,
        timeout=60,
    )
    modules = dict(original["modules"])
    for name in (
        "reporting/receipts/pg.py",
        "reporting/receipts/handler.py",
        "reporting/receipts/wire.py",
        "reporting/receipts/transport.py",
        "server/mcp_tools.py",
        "server/a2a_server.py",
        "server/serve.py",
    ):
        modules["adcp." + name.removesuffix(".py").replace("/", ".")] = hashlib.sha256(
            (ROOT / "src/adcp" / name).read_bytes()
        ).hexdigest()
    scripts = {}
    for name in ("_receipt_process.py", "_receipt_installed.py", "_receipt_transport.py"):
        scripts[name] = root / name
        shutil.copy2(Path(__file__).with_name(name), scripts[name])
    return root, python, scripts, {**original, "modules": modules}


@pytest.mark.parametrize("notifications", [False, True])
async def test_installed_receipt_sql_crash_and_mounted_replay(installed_receipts, notifications):
    root, python, scripts, installed = installed_receipts
    async with isolated_reporting_pool(autocommit=True) as pool:
        settings = {
            "installed": installed,
            "conninfo": pool.conninfo,
            "kwargs": pool.kwargs,
            "notifications": notifications,
            "action": "install",
        }

        async def invoke(settings):
            result = json.loads(
                await asyncio.to_thread(
                    run_step,
                    [str(python), "-I", str(scripts["_receipt_installed.py"])],
                    label=f"receipt-installed-{settings['action']}",
                    cwd=root,
                    value=settings,
                    timeout=90,
                )
            )
            assert "failure" not in result, result
            return result

        ready = await invoke(settings)
        assert ready["result"] == {"installed": True}
        h = DurableHarness(
            PgReportingReceiptStore(pool=pool, notifications=notifications), Clock(), pool
        )
        s = await receipt_case(h)
        adjustment = await adjustment_for(h, s)
        request = request_for(s, adjustment_receipts=[adjustment])
        async with worker(
            h,
            s,
            request,
            pause="between_ordinals",
            installed=installed,
            python=python,
            script=scripts["_receipt_process.py"],
        ) as child:
            await child.event("between_ordinals")
            # The child owns the next ordinal's account lock. Kill it
            # before using the public locked reader; ordinal zero is
            # already durable and must survive that process death.
            await child.kill()
            original = await h.store.get_receipt(s.receipt.key)
            assert original is not None
        done = await invoke(
            {
                **settings,
                "action": "replay",
                "request": request,
                "helper": str(scripts["_receipt_transport.py"]),
                "caller": {
                    "account_id": s.obligation.account_id,
                    "consumer_id": s.binding.consumer_id,
                },
            }
        )
        assert done["result"]["results"][0]["receipt"] == receipt_to_wire(original)
        assert done["origins"] == ready["origins"]
        assert (
            await h.store.ingest_receipt_batch(request, caller=s.binding.principal)
            == done["result"]
        )
        assert len(await h.store.read_receipt_boundaries(caller=s.binding.principal)) == 2
        assert await h.queue() == ((), ())
        print(
            json.dumps(
                {
                    "installed_receipts": installed,
                    "notifications": notifications,
                    "origins": done["origins"],
                    "received_at": done["result"]["results"][0]["receipt"]["received_at"],
                }
            ),
            flush=True,
        )
