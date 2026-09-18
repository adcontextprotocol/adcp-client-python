"""Actual approved B2.3 and current floor wheels at both changed boundaries."""

import asyncio
import hashlib
import json
import os
import shutil
import subprocess
import zipfile
from pathlib import Path

import pytest

from ._feed_support import feed_harness, feed_request, mixed_case, walk, without_feed
from ._hardening_packaging import ASSETS, B23, MODULES, installed_hardening
from .test_reporting_feed_installed_pg import (
    b1_wheels,
    built_distribution,
    feed_wheels,
    installed_feed,
)
from .test_reporting_feed_packaging import feed_modules
from .test_reporting_feed_process import feed_process
from .test_reporting_materializer_rolling import build_frozen
from .test_reporting_notification_packaging import ROOT, run_step

__all__ = ["b1_wheels", "built_distribution", "feed_wheels", "installed_feed"]


async def test_installed_python310_proof_and_receipt_diagnostics(installed_feed, feed_wheels):
    root, python, _, _, installed = installed_feed
    _, wheels, _ = feed_wheels
    wheel = wheels[installed["distribution"]]
    await asyncio.to_thread(
        installed_hardening, root, python, wheel, label=installed["distribution"] + "-pg"
    )


@pytest.fixture(scope="module")
def approved_b23(tmp_path_factory, request):
    # Preserve all nine original frozen inputs; this is an additional artifact.
    root, _, _, identity = build_frozen("b23-hardening-control", tmp_path_factory, request, sha=B23)
    interpreter = os.environ.get("ADCP_PYTHON310")
    if interpreter is None:
        pytest.skip("ADCP_PYTHON310 supplies the installed floor cell")
    environment = root / "python310"
    run_step(
        [interpreter, "-m", "venv", str(environment)], label="b23-python310-environment", cwd=root
    )
    python = environment / "bin/python"
    wheel = next((root / "dist").glob("*.whl"))
    installer = (
        [shutil.which("uv"), "pip", "install", "--python", str(python)]
        if shutil.which("uv")
        else [str(python), "-m", "pip", "install"]
    )
    run_step(
        [*installer, f"{wheel}[pg]", "asgi-lifespan==2.1.0"],
        label="b23-python310-wheel-install",
        cwd=root,
        timeout=180,
    )
    with zipfile.ZipFile(wheel) as archive:
        modules = {}
        for name in set(feed_modules()) | (set(MODULES) - {"adcp.reporting.receipts._diagnostics"}):
            path = name.replace(".", "/") + ".py"
            if path not in archive.namelist():
                path = name.replace(".", "/") + "/__init__.py"
            raw = archive.read(path)
            assert raw == subprocess.check_output(["git", "show", f"{B23}:src/{path}"], cwd=ROOT)
            modules[name] = hashlib.sha256(raw).hexdigest()
        assets = {}
        for name in ASSETS:
            raw = archive.read("adcp/reporting/" + name)
            assert raw == subprocess.check_output(
                ["git", "show", f"{B23}:src/adcp/reporting/{name}"], cwd=ROOT
            )
            assets[name] = hashlib.sha256(raw).hexdigest()
    script, helper = root / "feed_process.py", root / "receipt_transport.py"
    shutil.copy2(Path(__file__).with_name("_feed_process.py"), script)
    shutil.copy2(Path(__file__).with_name("_receipt_transport.py"), helper)
    installed = {
        **identity,
        "modules": modules,
        "assets": assets,
        "python": [3, 10],
        "tree": "16c55b24340aeea3524c781f19bfa64d1491ac34",
    }
    return root, python, script, helper, installed, wheel


def test_actual_approved_b23_installed_negative_and_preservation_controls(approved_b23):
    root, python, _, _, identity, wheel = approved_b23
    result = installed_hardening(root, python, wheel, label="b23-parent", parent=True)
    print(
        json.dumps({"approved_b23_installed_control": identity, "results": result["results"]}),
        flush=True,
    )


@pytest.mark.parametrize("notifications", [False, True])
async def test_actual_b23_to_child_installed_restart_preserves_pages_and_receipt_replay(
    approved_b23, installed_feed, notifications
):
    _, parent_python, parent_script, parent_helper, parent, _ = approved_b23
    root, python, script, helper, current = installed_feed
    old = {
        "python": parent_python,
        "script": parent_script,
        "helper": parent_helper,
        "installed": parent,
    }
    new = {"python": python, "script": script, "helper": helper, "installed": current}
    async with feed_harness("postgres", notifications=notifications) as h:
        s, receipt_request, receipt_response = await mixed_case(h)
        # The approved binary really mounts the ingress and returns its durable
        # replay before it writes page one; fixtures only supply populated data.
        async with feed_process(h, s, receipt_request, action="receipt", **old) as child:
            admitted = await child.event("done")
            assert await asyncio.wait_for(child.process.wait(), 5) == 0
        assert admitted["result"] == receipt_response
        async with feed_process(h, s, feed_request(s), pause="committed", **old) as child:
            first = (await child.event("committed"))["result"]
            await child.kill()
        original = await h.store.read_reporting_feed_snapshot(
            first["ledger_snapshot_id"], caller=s.binding.principal
        )
        expected = await walk(h.store, feed_request(s), s.binding.principal, first=first)
        await h.store.set_revision_readable(
            account_id=s.obligation.account_id,
            reporting_revision_id=s.revision.reporting_revision_id,
            readable=False,
        )
        # Normal stop/migrate/restart uses the current installed migration path.
        ready = json.loads(
            await asyncio.to_thread(
                run_step,
                [str(python), "-I", str(script)],
                label="b23-to-child-installed-restart",
                cwd=root,
                value={
                    "conninfo": h.pool.conninfo,
                    "kwargs": h.pool.kwargs,
                    "notifications": notifications,
                    "action": "install",
                    "installed": current,
                },
                timeout=90,
            )
        )
        assert ready["result"]["feed_objects"] == 33
        before = without_feed(await h.image())
        continuation = feed_request(
            s, pagination={"cursor": first["pagination"]["cursor"], "max_results": 1}
        )
        for v1 in (False, True):
            async with feed_process(
                h, s, continuation, action="walk", transport="a2a", v1=v1, **new
            ) as child:
                continued = await child.event("done")
                assert await asyncio.wait_for(child.process.wait(), 5) == 0
            assert continued["result"]["pages"] == expected[0][1:]
            assert continued["result"]["binding"] == original.binding
            assert continued["result"]["version"] == original.representation_version
            assert continued["result"]["ownership_mode"] == original.ownership_mode == "absent"
        async with feed_process(h, s, receipt_request, action="receipt", **new) as child:
            replayed = await child.event("done")
            assert await asyncio.wait_for(child.process.wait(), 5) == 0
        assert replayed["result"] == receipt_response
        assert without_feed(await h.image()) == before
        assert (
            await h.store.read_reporting_feed_snapshot(
                first["ledger_snapshot_id"], caller=s.binding.principal
            )
            == original
        )
        print(
            json.dumps(
                {
                    "b23_to_child_restart": current["distribution"],
                    "notifications": notifications,
                    "parent": parent,
                    "current": current,
                    "parent_origins": admitted["origins"],
                    "current_origins": replayed["origins"],
                    "page_count": len(expected[0]),
                    "checkpoint": first["changes_checkpoint"],
                }
            ),
            flush=True,
        )
