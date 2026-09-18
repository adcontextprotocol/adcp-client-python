"""Installed hardening probes reuse fixtures, never current SDK source modules."""

import hashlib
import json
import os
import shutil
import subprocess
import zipfile
from pathlib import Path

from .test_reporting_notification_packaging import ROOT, run_step

MODULES = (
    "adcp.reporting.outbox.support",
    "adcp.reporting.outbox._schema",
    "adcp.reporting.outbox.status_schema",
    "adcp.reporting.receipts.handler",
    "adcp.reporting.receipts.pg",
    "adcp.reporting.receipts._diagnostics",
)
ASSETS = (
    "outbox/required_schema.json",
    "outbox/required_status_schema.json",
    "ledger/reporting_feed.sql",
    "feed/required_schema.json",
    "ledger/reporting_materializer.sql",
    "materializer/required_schema.json",
    "ledger/reporting_receipt_ingestion.sql",
    "receipts/required_schema.json",
)
B23 = "50e35f0ae3540f19b40e8fc460f5870dfe018bf9"


def installed_hardening(root, python, wheel, *, label, parent=False, driver_absent=False):
    fixture_root = root / f"hardening-{label}"
    fixture_root.mkdir(mode=0o700)
    shutil.copytree(
        ROOT / "tests", fixture_root / "tests", ignore=shutil.ignore_patterns("__pycache__")
    )
    script = fixture_root / "run_installed.py"
    shutil.copy2(Path(__file__).with_name("_hardening_installed.py"), script)
    installer = (
        [shutil.which("uv"), "pip", "install", "--python", str(python)]
        if shutil.which("uv")
        else [str(python), "-m", "pip", "install"]
    )
    run_step(
        [
            *installer,
            "pytest==9.1.1",
            "pytest-asyncio==1.4.0",
            "respx==0.23.1",
            "asgi-lifespan==2.1.0",
        ],
        label=f"{label}-conformance-dependencies",
        cwd=root,
        timeout=180,
    )
    with zipfile.ZipFile(wheel) as archive:
        modules = {
            name: hashlib.sha256(archive.read(name.replace(".", "/") + ".py")).hexdigest()
            for name in MODULES
            if name.replace(".", "/") + ".py" in archive.namelist()
        }
        assets = {
            name: hashlib.sha256(archive.read("adcp/reporting/" + name)).hexdigest()
            for name in ASSETS
        }
        for path in [
            *(name.replace(".", "/") + ".py" for name in modules),
            *("adcp/reporting/" + name for name in assets),
        ]:
            expected = (
                subprocess.check_output(["git", "show", f"{B23}:src/{path}"], cwd=ROOT)
                if parent
                else (ROOT / "src" / path).read_bytes()
            )
            assert archive.read(path) == expected
    settings = {
        "workspace": str(ROOT),
        "fixtures": str(fixture_root),
        "label": label,
        "modules": modules,
        "assets": assets,
        "parent": parent,
        "driver_absent": driver_absent,
        "python": [3, 10],
        "source": (
            B23
            if parent
            else subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
        ),
        "evidence": os.environ.get("ADCP_HARDENING_EVIDENCE", str(root / "hardening-evidence")),
    }
    result = json.loads(
        run_step(
            [str(python), "-I", str(script)],
            label=f"{label}-installed-hardening",
            cwd=fixture_root,
            value=settings,
            timeout=420,
        )
    )
    print(
        json.dumps(
            {
                "installed_hardening": label,
                "wheel_sha256": hashlib.sha256(wheel.read_bytes()).hexdigest(),
                **result,
            }
        ),
        flush=True,
    )
    assert all(item["valid"] for item in result["results"]), result["results"]
    return result
