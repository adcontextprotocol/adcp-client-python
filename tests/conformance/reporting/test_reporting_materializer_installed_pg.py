"""Non-editable VCS/sdist SQL installation and killed/resumed PostgreSQL workers."""

import asyncio
import hashlib
import json
import os
import shutil
import sys
from pathlib import Path

import pytest

from adcp.reporting.materializer import PgReportingMaterializerStore

from ._durable_materializer_support import DurableHarness, durable_case
from ._generation_support import isolated_reporting_pool, require_rolling_database
from .test_reporting_materializer_packaging import ROOT, b1_wheels, built_distribution, run_step
from .test_reporting_materializer_process import worker

# Expose the exact existing fixtures without rebuilding a current module as an
# alleged historical artifact. These are the new head's two distribution paths.
__all__ = ["b1_wheels", "built_distribution"]


@pytest.fixture(scope="module", params=["vcs", "sdist"])
def installed_materializer(request):
    require_rolling_database()
    b1_wheels = request.getfixturevalue("b1_wheels")
    path, wheels, _ = b1_wheels
    interpreter = os.environ.get("ADCP_PYTHON310") or sys.executable
    environment = path / f"materializer-pg-{request.param}"
    run_step(
        [interpreter, "-m", "venv", str(environment)],
        label=f"materializer-{request.param}-pg-environment",
        cwd=path,
    )
    python = environment / "bin/python"
    installer = (
        [shutil.which("uv"), "pip", "install", "--python", str(python)]
        if shutil.which("uv")
        else [str(python), "-m", "pip", "install"]
    )
    run_step(
        [*installer, f"{wheels[request.param]}[pg]"],
        label=f"materializer-{request.param}-pg-install",
        cwd=path,
        timeout=180,
    )
    script = path / f"materializer_pg_{request.param}.py"
    shutil.copy2(Path(__file__).with_name("_materializer_process.py"), script)
    version = json.loads(
        run_step(
            [str(python), "-I", "-c", "import json,sys;print(json.dumps(sys.version_info[:2]))"],
            label="materializer-installed-python-version",
            cwd=path,
        )
    )
    if os.environ.get("ADCP_PYTHON310"):
        assert version == [3, 10]
    modules = {}
    for name in ("materializer/pg.py", "materializer/service.py", "materializer/verification.py"):
        modules["adcp.reporting." + name.removesuffix(".py").replace("/", ".")] = hashlib.sha256(
            (ROOT / "src/adcp/reporting" / name).read_bytes()
        ).hexdigest()
    return path, python, script, {"workspace": str(ROOT), "python": version, "modules": modules}


@pytest.mark.parametrize("notifications", [False, True])
async def test_installed_sql_and_process_restart_preserve_original_effect(
    installed_materializer, notifications, tmp_path
):
    path, python, script, installed = installed_materializer
    async with isolated_reporting_pool(autocommit=True) as pool:
        result = json.loads(
            await asyncio.to_thread(
                run_step,
                [str(python), "-I", str(script)],
                label="materializer-installed-sql-readiness",
                cwd=path,
                value={
                    "conninfo": pool.conninfo,
                    "kwargs": pool.kwargs,
                    "notifications": notifications,
                    "action": "install",
                    "installed": installed,
                },
                timeout=60,
            )
        )
        assert result["point"] == "done" and result["state"] == "installed"
        h = DurableHarness(
            PgReportingMaterializerStore(pool=pool, notifications=notifications), None, pool
        )
        case = await durable_case(h.store, count=501)
        options = dict(
            python=python, script=script, installed=installed, notifications=notifications
        )
        async with worker(h, case, tmp_path, pause="after_write", **options) as child:
            await child.event("after_write")
            before = await h.works()
            await child.kill()
        assert not await case.outcomes()
        assert await h.queue() == ((), ())
        await h.expire()
        async with worker(h, case, tmp_path, **options) as child:
            done = await child.event("done")
            assert done["state"] == "verified" and done["origins"] == result["origins"]
            materializer_operation_1 = await asyncio.wait_for(child.process.wait(), 5)
            assert materializer_operation_1 == 0
        after = await h.works()
        assert len(after) == 1 and after[0][0] == before[0][0] and after[0][1] == "acked"
        assert len(tuple(tmp_path.glob("rwm_*"))) == 1
        assert len(await h.store.read_materializer_boundaries(caller=case.scope.principal)) == 1
        assert len((await h.queue())[0]) == int(notifications)
        print(json.dumps({"installed": installed, "origins": done["origins"]}), flush=True)
