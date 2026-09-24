"""Non-editable VCS/sdist wheels, actual Python 3.10, strict adopter and no PG."""

import hashlib
import json
import os
import shutil
import sys
import tarfile
import zipfile
from pathlib import Path

import pytest

from . import test_reporting_notification_packaging as distribution

built_distribution = distribution.built_distribution
ROOT = distribution.ROOT
run_step = distribution.run_step
ASSETS = ROOT / "src/adcp/reporting/materializer/assets"


@pytest.fixture(scope="module")
def b1_wheels(built_distribution):
    path, sdist_wheel, source = built_distribution
    direct = path / "vcs-wheel"
    # This build starts in the actual VCS checkout, including its build hook.
    run_step(
        [sys.executable, "-m", "build", "--wheel", "--outdir", str(direct), str(ROOT)],
        label="b1-vcs-wheel",
        cwd=ROOT,
        timeout=180,
    )
    vcs_wheel = next(direct.glob("*.whl"))
    expected = {f.name: f.read_bytes() for f in ASSETS.glob("*.json")}
    with (
        zipfile.ZipFile(vcs_wheel) as vcs,
        zipfile.ZipFile(sdist_wheel) as wheel,
        tarfile.open(source) as tar,
    ):
        prefix = tar.getnames()[0].split("/")[0]
        for name, raw in expected.items():
            member = f"adcp/reporting/materializer/assets/{name}"
            assert vcs.read(member) == wheel.read(member) == raw
            assert tar.extractfile(f"{prefix}/src/{member}").read() == raw
        for relative in (
            "ledger/reporting_status_selector_version.sql",
            "outbox/required_status_selector_schema.json",
            "ledger/reporting_materializer.sql",
            "materializer/required_schema.json",
        ):
            assert (
                vcs.read(f"adcp/reporting/{relative}")
                == wheel.read(f"adcp/reporting/{relative}")
                == (ROOT / "src/adcp/reporting" / relative).read_bytes()
            )
    return (
        path,
        {"vcs": vcs_wheel, "sdist": sdist_wheel},
        {name: hashlib.sha256(raw).hexdigest() for name, raw in expected.items()},
    )


@pytest.mark.parametrize("kind", ["vcs", "sdist"])
def test_python310_installed_wheel_exports_verifier_reference_and_strict_adopter(b1_wheels, kind):
    path, wheels, hashes = b1_wheels
    interpreter = os.environ.get("ADCP_PYTHON310") or (
        sys.executable if sys.version_info[:2] == (3, 10) else None
    )
    if interpreter is None:
        pytest.skip(
            "Python 3.10 matrix job runs this gate; ADCP_PYTHON310 enables it on other hosts"
        )
    environment = path / f"b1-python310-{kind}"
    run_step(
        [interpreter, "-m", "venv", str(environment)],
        label=f"b1-{kind}-python310-environment",
        cwd=path,
    )
    python = environment / "bin/python"
    installer = (
        [shutil.which("uv"), "pip", "install", "--python", str(python)]
        if shutil.which("uv")
        else [str(python), "-m", "pip", "install"]
    )
    run_step(
        [*installer, str(wheels[kind]), "mypy==1.20.2"],
        label=f"b1-{kind}-noneditable-install",
        cwd=path,
        timeout=180,
    )
    smoke, example, adopter = (
        path / f"{name}-{kind}.py" for name in ("smoke", "example", "adopter")
    )
    shutil.copy2(Path(__file__).with_name("_materializer_installed.py"), smoke)
    shutil.copy2(ROOT / "examples/reporting_destination_writer.py", example)
    shutil.copy2(ROOT / "tests/type_checks/reporting_destination_writer.py", adopter)
    durable_example = path / f"durable_example_{kind}.py"
    durable_adopter = path / f"durable_adopter_{kind}.py"
    shutil.copy2(ROOT / "examples/reporting_durable_materializer.py", durable_example)
    shutil.copy2(ROOT / "tests/type_checks/reporting_durable_materializer.py", durable_adopter)
    result = json.loads(
        run_step(
            [str(python), "-I", str(smoke)],
            label=f"b1-{kind}-isolated-python310-smoke",
            cwd=path,
            value={"workspace": str(ROOT), "example": str(example), "assets": hashes},
            timeout=120,
        )
    )
    assert result == {
        "python": "3.10",
        "rows": [0, 501],
        "installed": True,
        "assets": hashes,
        "durable": True,
    }
    config = path / "mypy.ini"
    config.write_text(
        "[mypy]\npython_version = 3.10\nstrict = True\n"
        "plugins = adcp.types.mypy_plugin\nfollow_imports = silent\n"
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
            str(durable_adopter),
            str(durable_example),
        ],
        label=f"b1-{kind}-installed-adopter-types",
        cwd=path,
        timeout=120,
    )
