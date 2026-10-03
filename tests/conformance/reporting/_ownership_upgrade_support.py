"""Maintenance-window controls around actual, pinned historical installations."""

import asyncio
import json
import secrets
import shutil
from contextlib import asynccontextmanager
from pathlib import Path

import pytest

from ._ownership_legacy_seed import main as source_upgrade
from .test_reporting_notification_packaging import ROOT, run_step

PRE_OWNERSHIP = "fa4f9450b3d2292d4909786518dae1991e1ab7e3"


@pytest.fixture(scope="module")
def ownership_parent(tmp_path_factory, request):
    # Import locally: rolling fixtures also use the controls below.
    from .test_reporting_materializer_rolling import build_frozen

    return prepare_seed(
        build_frozen("ownership-parent", tmp_path_factory, request, sha=PRE_OWNERSHIP)
    )


def prepare_seed(artifact):
    root, python, _, settings = artifact
    fixtures = root / "ownership-fixtures"
    if not fixtures.exists():
        fixtures.mkdir(mode=0o700)
        shutil.copytree(
            ROOT / "tests", fixtures / "tests", ignore=shutil.ignore_patterns("__pycache__")
        )
        installer = (
            [shutil.which("uv"), "pip", "install", "--python", str(python)]
            if shutil.which("uv")
            else [str(python), "-m", "pip", "install"]
        )
        run_step([*installer, "pytest==9.1.1"], label="historical-fixture-dependency", cwd=root)
    script = root / "ownership_seed.py"
    shutil.copy2(Path(__file__).with_name("_ownership_legacy_seed.py"), script)
    return root, python, script, {**settings, "fixtures": str(fixtures)}


async def seed_legacy(artifact, pool, *, kind, consumer, notifications, legacy_definition=False):
    root, python, script, settings = artifact
    return json.loads(
        await asyncio.to_thread(
            run_step,
            [str(python), "-I", str(script)],
            label="historical-owned-upgrade-seed",
            cwd=root,
            value={
                **settings,
                "conninfo": pool.conninfo,
                "kwargs": pool.kwargs,
                "action": "seed",
                "kind": kind,
                "consumer": consumer,
                "notifications": notifications,
                "legacy_definition": legacy_definition,
            },
            timeout=90,
        )
    )


async def maintenance_upgrade(pool, legacy, *, notifications, installed=None, keep_archive=False):
    settings = {
        "workspace": str(ROOT),
        "modules": {},
        "conninfo": pool.conninfo,
        "kwargs": pool.kwargs,
        "action": "upgrade",
        "notifications": notifications,
        "legacy": legacy,
        "archive": "adcp_reporting_quarantine_" + secrets.token_hex(6),
        "keep_archive": keep_archive,
    }
    if installed is None:
        result = await source_upgrade({**settings, "source_runtime": True})
    else:
        root, python, identity = installed
        script = root / "ownership_upgrade.py"
        shutil.copy2(Path(__file__).with_name("_ownership_legacy_seed.py"), script)
        result = json.loads(
            await asyncio.to_thread(
                run_step,
                [str(python), "-I", str(script)],
                label="installed-maintenance-upgrade",
                cwd=root,
                value={**settings, "modules": identity["modules"], "python": [3, 10]},
                timeout=120,
            )
        )
    assert result["archive_unchanged"] and result["legacy_cursor_restarted"]
    assert result["retained_quarantined"] and result["inherited_rows"]
    return result


@asynccontextmanager
async def pending_legacy(artifact, pool, *, consumer, notifications):
    from .test_reporting_materializer_process import Child

    _, python, script, settings = artifact
    process = await asyncio.create_subprocess_exec(
        str(python),
        "-I",
        str(script),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    child = Child(process)
    try:
        await child.send(
            {
                **settings,
                "conninfo": pool.conninfo,
                "kwargs": pool.kwargs,
                "action": "seed",
                "kind": "feed",
                "consumer": consumer,
                "notifications": notifications,
                "legacy_definition": False,
                "pending": True,
                "pause": True,
            }
        )
        yield child, (await child.event("pending"))["legacy"]
    finally:
        await child.kill()
        diagnostic = await process.stderr.read()
        assert process.returncode == -9, (process.returncode, len(diagnostic))
