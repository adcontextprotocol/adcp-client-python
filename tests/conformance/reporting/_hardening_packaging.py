"""Installed hardening probes reuse fixtures, never current SDK source modules."""

import importlib.util as _importlib_util


def _storage_split():
    """True when the adcp on sys.path predates the storage split (#1510).

    Its stores still live in adcp.reporting and the storage package must not
    be used. Evaluated at each import, after the script has set up sys.path.
    """
    return _importlib_util.find_spec("adcp.reporting.ledger.pg") is not None


import hashlib
import json
import os
import shutil
import subprocess
import zipfile
from pathlib import Path

from .test_reporting_notification_packaging import (
    ROOT,
    STORAGE_SRC,
    is_current_build,
    run_step,
    storage_distribution,
)

MODULES = (
    "adcp.reporting.outbox.support",
    "adcp_reporting_storage.outbox._schema",
    "adcp_reporting_storage.outbox.status_schema",
    "adcp.reporting.receipts.handler",
    "adcp_reporting_storage.receipts.pg",
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
B23 = "2d777ace7b4bf8be519ce0abd4fd0a25ed4f1da7"


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
    # A current build is two wheels: the stores, DDL and catalog manifests are in
    # the storage wheel. A historical wheel (the B23 parent) carries them itself.
    current = is_current_build(wheel)
    storage = zipfile.ZipFile(storage_distribution(wheel)) if current else None

    def located(name):
        """(archive, member path, expected bytes) for a module or asset name."""
        if name in ASSETS:
            if current:
                member, source = "adcp_reporting_storage/" + name, STORAGE_SRC / name
            else:
                member, source = "adcp/reporting/" + name, ROOT / "src/adcp/reporting" / name
        elif name.startswith("adcp_reporting_storage."):
            member = name.replace(".", "/") + ".py"
            source = STORAGE_SRC.parent / member
            if not current:
                member = member.replace("adcp_reporting_storage/", "adcp/reporting/", 1)
        else:
            member = name.replace(".", "/") + ".py"
            source = ROOT / "src" / member
        archive = (
            storage if current and member.startswith("adcp_reporting_storage/") else wheel_archive
        )

        def expected():
            # Resolved only for members the archive carries: a module newer than
            # the parent has no bytes at the parent commit.
            if parent:
                return subprocess.check_output(["git", "show", f"{B23}:src/{member}"], cwd=ROOT)
            return source.read_bytes()

        return archive, member, expected

    with zipfile.ZipFile(wheel) as wheel_archive:
        modules, assets = {}, {}
        for name in MODULES:
            archive, member, expected = located(name)
            if member not in archive.namelist():
                continue
            raw = archive.read(member)
            assert raw == expected(), member
            key = name if current else name.replace("adcp_reporting_storage.", "adcp.reporting.", 1)
            modules[key] = hashlib.sha256(raw).hexdigest()
        for name in ASSETS:
            archive, member, expected = located(name)
            raw = archive.read(member)
            assert raw == expected(), member
            assets[name] = hashlib.sha256(raw).hexdigest()
    if storage is not None:
        storage.close()
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
