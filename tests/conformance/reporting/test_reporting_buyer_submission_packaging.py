"""Actual Python 3.10 VCS/sdist wheels, no-driver imports, typing and PG restart."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shutil
import tarfile
import zipfile
from pathlib import Path

import pytest

from adcp.reporting.ledger.delivery import receipt_to_wire
from adcp.reporting.submissions import PgReportingSubmissionIntentStore, ReportingSubmissionScope

from ._generation_support import require_rolling_database
from ._receipt_support import adjustment_for, batch_state, receipt_case, receipt_harness
from .test_reporting_buyer_submission_process import buyer_worker
from .test_reporting_materializer_packaging import b1_wheels, built_distribution
from .test_reporting_notification_packaging import ROOT, run_step

__all__ = ["b1_wheels", "built_distribution"]


@pytest.mark.parametrize("kind", ["vcs", "sdist"])
@pytest.mark.parametrize("drivers", [False, True], ids=["base", "pg"])
async def test_python310_installed_buyer_submission_contract(request, kind, drivers):
    interpreter = os.environ.get("ADCP_PYTHON310")
    if interpreter is None:
        pytest.skip("ADCP_PYTHON310 supplies the actual installed floor runtime")
    if drivers:
        require_rolling_database()
    root, wheels, _ = request.getfixturevalue("b1_wheels")
    _, _, source = request.getfixturevalue("built_distribution")
    label = "buyer-" + kind + ("-pg" if drivers else "-base")
    environment = root / label
    await asyncio.to_thread(
        run_step,
        [interpreter, "-m", "venv", str(environment)],
        label=label + "-environment",
        cwd=root,
    )
    python = environment / "bin/python"
    installer = (
        [shutil.which("uv"), "pip", "install", "--python", str(python)]
        if shutil.which("uv")
        else [str(python), "-m", "pip", "install"]
    )
    await asyncio.to_thread(
        run_step,
        [*installer, str(wheels[kind]) + ("[pg]" if drivers else ""), "mypy==1.20.2"],
        label=label + "-install",
        cwd=root,
        timeout=180,
    )
    modules = {
        "adcp.reporting.submissions"
        + ("." + path.stem if path.stem != "__init__" else ""): hashlib.sha256(
            path.read_bytes()
        ).hexdigest()
        for path in (ROOT / "src/adcp/reporting/submissions").glob("*.py")
    }
    sql_path = ROOT / "src/adcp/reporting/ledger/reporting_buyer_submissions.sql"
    with zipfile.ZipFile(wheels[kind]) as wheel, tarfile.open(source) as sdist:
        prefix = sdist.getnames()[0].split("/")[0]
        member = "adcp/reporting/ledger/reporting_buyer_submissions.sql"
        assert (
            wheel.read(member)
            == sdist.extractfile(f"{prefix}/src/{member}").read()
            == sql_path.read_bytes()
        )
    settings = {
        "workspace": str(ROOT),
        "modules": modules,
        "sql_sha256": hashlib.sha256(sql_path.read_bytes()).hexdigest(),
        "drivers": drivers,
    }
    smoke = root / f"{label}_smoke.py"
    worker_script = root / f"{label}_worker.py"
    adopter = root / f"{label.replace('-', '_')}_adopter.py"
    shutil.copy2(Path(__file__).with_name("_buyer_submission_installed.py"), smoke)
    shutil.copy2(Path(__file__).with_name("_buyer_submission_process.py"), worker_script)
    shutil.copy2(ROOT / "tests/type_checks/reporting_buyer_submission_intents.py", adopter)
    result = json.loads(
        await asyncio.to_thread(
            run_step,
            [str(python), "-I", str(smoke)],
            label=label + "-isolated-contract",
            cwd=root,
            value=settings,
            timeout=120,
        )
    )
    assert result["python"] == "3.10" and result["outcomes"] == 201
    assert result["submitted"] == 200 and result["drivers"] == drivers
    config = root / f"{label}-mypy.ini"
    config.write_text(
        "[mypy]\npython_version = 3.10\nstrict = True\n"
        "plugins = adcp.types.mypy_plugin\nfollow_imports = silent\n"
    )
    await asyncio.to_thread(
        run_step,
        [
            str(python),
            "-I",
            "-m",
            "mypy",
            "--config-file",
            str(config),
            "--strict",
            "--no-incremental",
            str(adopter),
        ],
        label=label + "-strict-adopter",
        cwd=root,
        timeout=120,
    )
    if drivers:
        for point in ("seller_committed", "confirmation_committed"):
            async with receipt_harness("postgres") as h:
                s = await receipt_case(h, consumer_id="https://buyer.example.test/agent")
                adjustment = await adjustment_for(h, s)
                scope = ReportingSubmissionScope(
                    "seller", s.obligation.account_id, s.binding.consumer_id
                )
                await PgReportingSubmissionIntentStore(pool=h.pool).create_schema()
                inputs = [adjustment, receipt_to_wire(s.receipt)]
                async with buyer_worker(
                    h,
                    scope,
                    inputs,
                    pause=point,
                    python=python,
                    script=worker_script,
                    installed=settings,
                ) as child:
                    await child.event(point)
                    await child.kill()
                async with buyer_worker(
                    h,
                    scope,
                    inputs,
                    resume=True,
                    python=python,
                    script=worker_script,
                    installed=settings,
                ) as restarted:
                    outcome = await restarted.event("done")
                    assert await asyncio.wait_for(restarted.process.wait(), 10) == 0
                assert outcome["outcomes"] == ["recorded", "recorded"]
                assert outcome["calls"] == int(point == "seller_committed")
                assert set(outcome["origins"]) == set(modules)
                assert await batch_state(h) == ((2, True),)
