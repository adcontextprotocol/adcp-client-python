"""Actual approved B2.3 and hardening binaries across installed B2.4 activation."""

import asyncio
import hashlib
import json
import os
import shutil
import subprocess
import zipfile
from contextlib import asynccontextmanager
from pathlib import Path

import pytest

from ._production_packaging import copied_fixtures, inspect_distribution, source_basis
from .test_reporting_feed_hardening_installed import approved_b23
from .test_reporting_feed_installed_pg import (
    b1_wheels,
    built_distribution,
    feed_wheels,
    installed_feed,
)
from .test_reporting_feed_process import feed_process
from .test_reporting_materializer_process import Child
from .test_reporting_materializer_rolling import build_frozen
from .test_reporting_notification_packaging import ROOT, run_step

__all__ = ["approved_b23", "b1_wheels", "built_distribution", "feed_wheels", "installed_feed"]
HARDENING = "e16eb8cf3074cabd45aab42840950f05ad6d2b43"


@pytest.fixture(scope="module", params=["b23", "hardening"])
def production_parent(request, tmp_path_factory):
    if request.param == "b23":
        return request.getfixturevalue("approved_b23")
    root, _, _, identity = build_frozen("hardening-b24", tmp_path_factory, request, sha=HARDENING)
    interpreter = os.environ.get("ADCP_PYTHON310")
    if interpreter is None:
        pytest.skip("ADCP_PYTHON310 supplies the installed floor artifact")
    environment = root / "python310"
    run_step(
        [interpreter, "-m", "venv", str(environment)], label="hardening-floor-environment", cwd=root
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
        label="hardening-floor-install",
        cwd=root,
        timeout=180,
    )
    from .test_reporting_feed_packaging import feed_modules

    with zipfile.ZipFile(wheel) as archive:
        modules = {}
        for name in {*feed_modules(), "adcp.reporting.outbox.status_pg"}:
            member = name.replace(".", "/") + ".py"
            if member not in archive.namelist():
                member = name.replace(".", "/") + "/__init__.py"
            raw = archive.read(member)
            assert raw == subprocess.check_output(
                ["git", "show", f"{HARDENING}:src/{member}"], cwd=ROOT
            )
            modules[name] = hashlib.sha256(raw).hexdigest()
    script, helper = root / "feed_process.py", root / "receipt_transport.py"
    shutil.copy2(Path(__file__).with_name("_feed_process.py"), script)
    shutil.copy2(Path(__file__).with_name("_receipt_transport.py"), helper)
    return (
        root,
        python,
        script,
        helper,
        {
            **identity,
            "modules": modules,
            "python": [3, 10],
            "tree": "c043d1e14c5071859f566e07cc9980058fa6ee07",
        },
        wheel,
    )


@pytest.fixture(scope="module")
def production_install(installed_feed, feed_wheels, built_distribution, production_parent):
    root, python, _, _, current = installed_feed
    parent_label = production_parent[4]["sha"][:12]
    label = parent_label + "-" + current["distribution"]
    _, wheels, _ = feed_wheels
    _, _, source = built_distribution
    modules, assets = inspect_distribution(wheels[current["distribution"]], source)
    fixture_root = copied_fixtures(root, label + "-restart")
    script = fixture_root / "production_process.py"
    shutil.copy2(Path(__file__).with_name("_production_installed_process.py"), script)
    installer = (
        [shutil.which("uv"), "pip", "install", "--python", str(python)]
        if shutil.which("uv")
        else [str(python), "-m", "pip", "install"]
    )
    run_step(
        [*installer, "pytest==9.1.1", "pytest-asyncio==1.4.0", "respx==0.23.1"],
        label="production-process-fixtures",
        cwd=root,
        timeout=180,
    )
    return (
        python,
        script,
        {
            **current,
            "fixtures": str(fixture_root),
            "modules": modules,
            "assets": assets,
            "source_basis": source_basis(
                wheels[current["distribution"]],
                source,
                evidence=Path(os.environ.get("ADCP_PRODUCTION_EVIDENCE", root / "evidence")),
                label=label + "-rolling",
            ),
        },
    )


@asynccontextmanager
async def installed_child(python, script, settings, path):
    log = path / settings.get(
        "diagnostic_name", "activation.log" if settings["pause"] else "continuation.log"
    )
    try:
        with log.open("xb") as diagnostic:
            process = await asyncio.create_subprocess_exec(
                str(python),
                "-I",
                str(script),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=diagnostic,
            )

            class ActivationChild(Child):
                async def event(self, point):
                    line = await asyncio.wait_for(self.process.stdout.readline(), 120)
                    assert (
                        line
                    ), f"installed process exited before {point}; diagnostic retained at {log}"
                    result = json.loads(line)
                    assert result["point"] == point
                    return result

            child = ActivationChild(process)
            try:
                await child.send(settings)
                yield child
            finally:
                await child.kill()
    finally:
        raw = log.read_bytes()
        retained = None
        if os.environ.get("ADCP_PRODUCTION_EVIDENCE"):
            evidence = Path(os.environ["ADCP_PRODUCTION_EVIDENCE"])
            evidence.mkdir(parents=True, exist_ok=True, mode=0o700)
            retained = evidence / (settings["evidence_key"] + "-" + log.name)
            with retained.open("xb") as stream:
                stream.write(raw)
        print(
            json.dumps(
                {
                    "installed_activation_process_log": str(log),
                    "retained_log": str(retained) if retained else None,
                    "bytes": len(raw),
                    "sha256": hashlib.sha256(raw).hexdigest(),
                }
            ),
            flush=True,
        )


@pytest.mark.parametrize("notifications", [False, True])
async def test_actual_parent_page_one_to_installed_activation_sigkill_and_complete_walk(
    production_parent, production_install, notifications, tmp_path
):
    from types import SimpleNamespace

    from ._generation_support import isolated_reporting_pool
    from ._ownership_upgrade_support import maintenance_upgrade, pending_legacy, prepare_seed

    old_root, old_python, old_script, old_helper, old, _ = production_parent
    python, _, current = production_install
    parent = prepare_seed((old_root, old_python, old_script, old))
    async with isolated_reporting_pool(autocommit=True) as pool:
        async with pending_legacy(
            parent,
            pool,
            consumer="https://buyer.example.test/production-legacy-owner",
            notifications=notifications,
        ) as (child, legacy):
            await child.kill()
            assert child.process.returncode == -9
        pending = legacy["pending"]
        assert pending["external_id"] and pending["attempt"]["reporting_materialization_id"]
        h = SimpleNamespace(pool=pool, store=SimpleNamespace(_notifications_enabled=notifications))
        case = SimpleNamespace(
            obligation=SimpleNamespace(account_id=legacy["account"]),
            binding=SimpleNamespace(consumer_id=legacy["consumer"]),
        )
        request = {
            "adcp_version": "3.2-rc.6",
            "account": {"account_id": legacy["account"]},
            "view": "periods",
            "pagination": {"max_results": 1},
        }
        async with feed_process(
            h,
            case,
            request,
            pause="committed",
            python=old_python,
            script=old_script,
            helper=old_helper,
            installed=old,
        ) as child:
            first = (await child.event("committed"))["result"]
            await child.kill()
            assert child.process.returncode == -9
        legacy["first"] = first
        root = Path(current["fixtures"])
        upgrade = await maintenance_upgrade(
            pool,
            legacy,
            notifications=notifications,
            installed=(root, python, current),
            keep_archive=True,
        )
        from psycopg import sql

        from ._ownership_legacy_seed import image

        try:
            # Restart the installed maintenance validation in a new process;
            # archived pending identity and every row survive without delivery.
            check = root / "archive_restart.py"
            shutil.copy2(Path(__file__).with_name("_ownership_archive_restart.py"), check)
            replay = json.loads(
                await asyncio.to_thread(
                    run_step,
                    [str(python), "-I", str(check)],
                    label="installed-quarantine-restart",
                    cwd=root,
                    value={
                        "conninfo": pool.conninfo,
                        "kwargs": pool.kwargs,
                        "archive": upgrade["archive"],
                        "legacy": legacy,
                        "notifications": notifications,
                        "archive_image_sha256": upgrade["archive_image_sha256"],
                    },
                    timeout=90,
                )
            )
            assert replay["pending_external_id"] == pending["external_id"]
            assert (
                replay["pending_materialization_id"]
                == pending["attempt"]["reporting_materialization_id"]
            )
            assert replay["active_pending"] == 0 and replay["archive_unchanged"]
            print(
                json.dumps(
                    {
                        "production_ownership_maintenance": old["sha"],
                        "distribution": current["distribution"],
                        "notifications": notifications,
                        "upgrade": upgrade,
                        "restart": replay,
                    }
                ),
                flush=True,
            )
        finally:
            async with pool.connection() as connection:
                assert await image(connection, upgrade["archive"])
                await connection.execute(
                    sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(upgrade["archive"]))
                )
