"""VCS and sdist installed Python 3.10, separate feed assets and strict adopter."""

import hashlib
import json
import os
import shutil
import sys
import tarfile
import zipfile
from pathlib import Path

import pytest

from .test_reporting_materializer_packaging import ROOT, b1_wheels, built_distribution, run_step

__all__ = ["b1_wheels", "built_distribution"]


def feed_modules():
    paths = list((ROOT / "src/adcp/reporting/feed").glob("*.py"))
    paths.extend(
        ROOT / "src/adcp" / name
        for name in (
            "reporting/receipts/handler.py",
            "reporting/receipts/transport.py",
            "server/mcp_tools.py",
            "server/a2a_server.py",
            "server/serve.py",
            "server/idempotency/store.py",
        )
    )
    return {
        "adcp."
        + str(p.relative_to(ROOT / "src/adcp"))
        .removesuffix(".py")
        .replace("/", ".")
        .removesuffix(".__init__"): hashlib.sha256(p.read_bytes())
        .hexdigest()
        for p in paths
    }


@pytest.fixture(scope="module")
def feed_wheels(b1_wheels, built_distribution):
    root, wheels, _ = b1_wheels
    _, _, source = built_distribution
    assets = {
        name: (ROOT / "src/adcp/reporting" / name).read_bytes()
        for name in (
            "ledger/reporting_feed.sql",
            "feed/required_schema.json",
            "ledger/reporting_materializer.sql",
            "materializer/required_schema.json",
            "ledger/reporting_receipt_ingestion.sql",
            "receipts/required_schema.json",
        )
    }
    with tarfile.open(source) as sdist:
        prefix = sdist.getnames()[0].split("/")[0]
        for name, raw in assets.items():
            assert sdist.extractfile(f"{prefix}/src/adcp/reporting/{name}").read() == raw
    for wheel in wheels.values():
        with zipfile.ZipFile(wheel) as archive:
            for name, raw in assets.items():
                assert archive.read("adcp/reporting/" + name) == raw
    return root, wheels, {name: hashlib.sha256(raw).hexdigest() for name, raw in assets.items()}


@pytest.mark.parametrize("kind", ["vcs", "sdist"])
def test_python310_feed_without_pg_exports_sql_and_strict_adopter(request, kind):
    interpreter = os.environ.get("ADCP_PYTHON310") or (
        sys.executable if sys.version_info[:2] == (3, 10) else None
    )
    if interpreter is None:
        pytest.skip("Python 3.10 matrix owns this cell; ADCP_PYTHON310 enables it locally")
    root, wheels, assets = request.getfixturevalue("feed_wheels")
    environment = root / f"feed-base-{kind}"
    run_step(
        [interpreter, "-m", "venv", str(environment)],
        label=f"feed-{kind}-base-environment",
        cwd=root,
    )
    python = environment / "bin/python"
    installer = (
        [shutil.which("uv"), "pip", "install", "--python", str(python)]
        if shutil.which("uv")
        else [str(python), "-m", "pip", "install"]
    )
    run_step(
        [*installer, str(wheels[kind]), "mypy==1.20.2"],
        label=f"feed-{kind}-base-install",
        cwd=root,
        timeout=180,
    )
    script = root / f"feed_base_{kind}.py"
    shutil.copy2(Path(__file__).with_name("_feed_installed_base.py"), script)
    settings = {"workspace": str(ROOT), "assets": assets, "modules": feed_modules()}
    result = json.loads(
        run_step(
            [str(python), "-I", str(script)],
            label=f"feed-{kind}-base-smoke",
            cwd=root,
            value=settings,
            timeout=120,
        )
    )
    assert result["driver_absent"] and result["python"] == "3.10"
    adopter = root / f"feed_adopter_{kind}.py"
    example = root / f"feed_example_{kind}.py"
    shutil.copy2(ROOT / "tests/type_checks/reporting_frozen_feed.py", adopter)
    shutil.copy2(ROOT / "examples/reporting_receipt_ingress.py", example)
    config = root / f"feed_mypy_{kind}.ini"
    config.write_text(
        "[mypy]\npython_version=3.10\nstrict=True\nplugins=adcp.types.mypy_plugin\nfollow_imports=silent\n"
    )
    run_step(
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
            str(example),
        ],
        label=f"feed-{kind}-base-adopter",
        cwd=root,
        timeout=120,
    )
    print(
        json.dumps(
            {
                "feed_base_install": kind,
                "wheel_sha256": hashlib.sha256(wheels[kind].read_bytes()).hexdigest(),
                **result,
            }
        ),
        flush=True,
    )
    from ._hardening_packaging import installed_hardening

    installed_hardening(root, python, wheels[kind], label=kind + "-base", driver_absent=True)
