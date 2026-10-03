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
        "tree": "2f71a273c0218e7ffc490fb4df243d02711cff7b",
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
async def test_actual_b23_to_child_installed_restart_preserves_history_and_refuses_old_pin(
    approved_b23, installed_feed, notifications
):
    from types import SimpleNamespace

    from ._generation_support import isolated_reporting_pool
    from ._ownership_upgrade_support import maintenance_upgrade, prepare_seed, seed_legacy

    parent_root, parent_python, parent_script, parent_helper, parent, _ = approved_b23
    root, python, script, helper, current = installed_feed
    old = {
        "python": parent_python,
        "script": parent_script,
        "helper": parent_helper,
        "installed": parent,
    }
    new = {"python": python, "script": script, "helper": helper, "installed": current}
    parent_seed = prepare_seed((parent_root, parent_python, parent_script, parent))
    async with isolated_reporting_pool(autocommit=True) as pool:
        legacy = await seed_legacy(
            parent_seed,
            pool,
            kind="feed",
            consumer="https://buyer.example.test/b23-owner",
            notifications=notifications,
        )
        h = SimpleNamespace(pool=pool, store=SimpleNamespace(_notifications_enabled=notifications))
        s = SimpleNamespace(
            obligation=SimpleNamespace(account_id=legacy["account"]),
            binding=SimpleNamespace(consumer_id=legacy["consumer"]),
        )
        async with feed_process(h, s, legacy["request"], action="receipt", **old) as child:
            admitted = await child.event("done")
            assert await asyncio.wait_for(child.process.wait(), 5) == 0
        assert admitted["result"] == legacy["response"]
        query = {
            "adcp_version": "3.2-rc.6",
            "account": {"account_id": legacy["account"]},
            "view": "periods",
            "pagination": {"max_results": 1},
        }
        async with feed_process(h, s, query, pause="committed", **old) as child:
            first = (await child.event("committed"))["result"]
            await child.kill()
        assert first["pagination"]["has_more"]
        legacy["first"] = first
        upgrade = await maintenance_upgrade(
            pool, legacy, notifications=notifications, installed=(root, python, current)
        )
        # rc.6 bytes stay archived. The current mounted API refuses the old pin
        # before attempting any replay; ownership backfill grants no delivery.
        async with feed_process(h, s, legacy["request"], action="receipt_refused", **new) as child:
            replayed = await child.event("done")
            assert await asyncio.wait_for(child.process.wait(), 5) == 0
        assert replayed["result"] == {"error_code": "VERSION_UNSUPPORTED"}
        print(
            json.dumps(
                {
                    "b23_to_child_maintenance": current["distribution"],
                    "notifications": notifications,
                    "parent": parent,
                    "current": current,
                    "upgrade": upgrade,
                    "parent_origins": admitted["origins"],
                    "current_origins": replayed["origins"],
                }
            ),
            flush=True,
        )
