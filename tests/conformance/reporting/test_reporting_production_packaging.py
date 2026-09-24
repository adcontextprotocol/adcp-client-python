"""Python 3.10 VCS/sdist installed B2.4, with and without optional PostgreSQL."""

import asyncio
import os
import shutil

import pytest

from ._generation_support import require_rolling_database
from ._production_packaging import installed_production
from .test_reporting_materializer_packaging import b1_wheels, built_distribution
from .test_reporting_notification_packaging import run_step

__all__ = ["b1_wheels", "built_distribution"]


@pytest.mark.parametrize("kind", ["vcs", "sdist"])
@pytest.mark.parametrize("drivers", [False, True], ids=["base", "pg"])
async def test_floor_installed_production_contract(request, kind, drivers):
    interpreter = os.environ.get("ADCP_PYTHON310")
    if interpreter is None:
        pytest.skip("ADCP_PYTHON310 supplies the installed floor runtime")
    if drivers:
        require_rolling_database()
    root, wheels, _ = request.getfixturevalue("b1_wheels")
    _, _, source = request.getfixturevalue("built_distribution")
    label = kind + ("-pg" if drivers else "-base")
    environment = root / ("production-python310-" + label)
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
        [*installer, str(wheels[kind]) + ("[pg]" if drivers else "")],
        label=label + "-install",
        cwd=root,
        timeout=180,
    )
    await asyncio.to_thread(
        installed_production,
        root,
        python,
        wheels[kind],
        source,
        label=label,
        driver_absent=not drivers,
    )
