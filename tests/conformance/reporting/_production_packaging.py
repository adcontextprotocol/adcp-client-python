"""Build and inspect actual production distributions; fixtures never alias SDK source."""

import hashlib
import json
import os
import shutil
import subprocess
import tarfile
import zipfile
from pathlib import Path

from .test_reporting_notification_packaging import ROOT, run_step

ASSETS = (
    "ledger/reporting_materializer.sql",
    "materializer/required_schema.json",
    "ledger/reporting_receipt_ingestion.sql",
    "receipts/required_schema.json",
    "ledger/reporting_feed.sql",
    "feed/required_schema.json",
    "ledger/reporting_status_notifications.sql",
    "outbox/required_status_schema.json",
    "ledger/reporting_status_selector_version.sql",
    "outbox/required_status_selector_schema.json",
    "ledger/reporting_projection.sql",
    "ledger/reporting_projection_notifications.sql",
    "ledger/reporting_projection_feed.sql",
    "projection/required_schema.json",
    "ledger/reporting_production.sql",
    "production/required_schema.json",
)
SCHEMAS = (
    "core/reporting-delivery-config-state.json",
    "media-buy/get-reporting-status-response.json",
    "bundled/media-buy/get-reporting-status-response.json",
    "mcp/2026-07-28/profiles/production/media-buy/get-reporting-status-response.json",
    "protocol/get-adcp-capabilities-response.json",
    "bundled/protocol/get-adcp-capabilities-response.json",
)


def production_modules():
    paths = [
        p
        for part in ("reporting/production", "reporting/projection")
        for p in (ROOT / "src/adcp" / part).glob("*.py")
    ]
    paths += [
        ROOT / "src/adcp" / name
        for name in (
            "reporting/ledger/status.py",
            "reporting/ledger/status_snapshot.py",
            "reporting/ledger/status_projection.py",
            "reporting/ledger/reconciliation_projection.py",
            "reporting/ledger/schedule.py",
            "reporting/ledger/producer.py",
            "reporting/ledger/producer_progress.py",
            "reporting/materializer/memory.py",
            "reporting/materializer/pg.py",
            "reporting/materializer/publication.py",
            "reporting/materializer/service.py",
            "reporting/materializer/verification.py",
            "reporting/feed/request.py",
            "reporting/feed/errors.py",
            "reporting/ledger/status_server.py",
            "reporting/feed/snapshot.py",
            "reporting/feed/projection.py",
            "reporting/feed/memory.py",
            "reporting/feed/pg.py",
            "reporting/ownership.py",
            "reporting/_timestamp.py",
            "reporting/_reconcile.py",
            "reporting/receipts/handler.py",
            "reporting/outbox/memory.py",
            "reporting/outbox/_activity_pg.py",
            "reporting/outbox/worker.py",
            "validation/schema_loader.py",
            "types/base.py",
            "types/generated_poc/core/reporting_delivery_capabilities.py",
            "types/generated_poc/bundled/protocol/get_adcp_capabilities_response.py",
        )
    ]
    return {
        "adcp."
        + str(p.relative_to(ROOT / "src/adcp"))
        .removesuffix(".py")
        .replace("/", ".")
        .removesuffix(".__init__"): hashlib.sha256(p.read_bytes())
        .hexdigest()
        for p in paths
    }


def inspect_distribution(wheel, source):
    modules = production_modules()
    assets = {name: (ROOT / "src/adcp/reporting" / name).read_bytes() for name in ASSETS}
    with zipfile.ZipFile(wheel) as archive, tarfile.open(source) as sdist:
        prefix = sdist.getnames()[0].split("/")[0]
        for name, digest in modules.items():
            member = name.replace(".", "/") + ".py"
            if member not in archive.namelist():
                member = name.replace(".", "/") + "/__init__.py"
            raw = archive.read(member)
            assert hashlib.sha256(raw).hexdigest() == digest
            assert sdist.extractfile(f"{prefix}/src/{member}").read() == raw
        for name, raw in assets.items():
            assert archive.read("adcp/reporting/" + name) == raw
            assert sdist.extractfile(f"{prefix}/src/adcp/reporting/{name}").read() == raw
    return modules, {name: hashlib.sha256(raw).hexdigest() for name, raw in assets.items()}


def copied_fixtures(root, label):
    destination = root / ("production-" + label)
    destination.mkdir(mode=0o700)
    shutil.copytree(
        ROOT / "tests", destination / "tests", ignore=shutil.ignore_patterns("__pycache__")
    )
    shutil.copy2(ROOT / "examples/reporting_production.py", destination / "adopter.py")
    assert not (destination / "adcp").exists() and not (destination / "src").exists()
    return destination


def source_basis(wheel, source, *, evidence, label):
    """Record actual build inputs; a dirty checkout's HEAD is lineage only."""

    def git(*args):
        return subprocess.check_output(["git", *args], cwd=ROOT)

    head, tree = (git("rev-parse", value).decode().strip() for value in ("HEAD", "HEAD^{tree}"))
    dirty = bool(git("status", "--porcelain=v1", "-uall"))
    members = []
    with tarfile.open(source) as archive:
        prefix = archive.getnames()[0].split("/")[0] + "/"
        for member in archive.getmembers():
            if not member.isfile():
                continue
            relative = Path(member.name.removeprefix(prefix))
            if any(p.endswith(".egg-info") for p in relative.parts):
                continue
            path = ROOT / relative
            if not path.is_file():
                continue  # Generated sdist metadata is identified by its archive digest.
            raw = archive.extractfile(member).read()
            assert path.read_bytes() == raw, relative
            members.append(
                {
                    "path": str(relative),
                    "bytes": len(raw),
                    "sha256": hashlib.sha256(raw).hexdigest(),
                }
            )
    raw_manifest = (json.dumps(sorted(members, key=lambda r: r["path"]), indent=2) + "\n").encode()
    evidence.mkdir(parents=True, exist_ok=True, mode=0o700)
    manifest = evidence / (label + "-build-inputs.json")
    with manifest.open("xb") as stream:
        stream.write(raw_manifest)
    basis = {
        "kind": "development-export" if dirty else "git-commit",
        "head": head,
        "tree": tree,
        "clean": not dirty,
        "head_role": "parent lineage only" if dirty else "actual built commit",
        "build_input_manifest": str(manifest),
        "build_input_count": len(members),
        "build_input_manifest_sha256": hashlib.sha256(raw_manifest).hexdigest(),
        "sdist_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
        "wheel_sha256": hashlib.sha256(wheel.read_bytes()).hexdigest(),
    }
    if dirty:
        patch = git("diff", "--binary", "HEAD")
        patch_file = evidence / (label + "-development.patch")
        with patch_file.open("xb") as stream:
            stream.write(patch)
        basis["patch_sha256"] = hashlib.sha256(patch).hexdigest()
        basis["patch"] = str(patch_file)
        new_files = {
            p.decode(): hashlib.sha256((ROOT / p.decode()).read_bytes()).hexdigest()
            for p in git("ls-files", "--others", "--exclude-standard", "-z").split(b"\0")
            if p and (ROOT / p.decode()).is_file()
        }
        new_manifest = evidence / (label + "-development-new-files.json")
        raw_new = (json.dumps(new_files, indent=2, sort_keys=True) + "\n").encode()
        with new_manifest.open("xb") as stream:
            stream.write(raw_new)
        basis["new_files_manifest"] = str(new_manifest)
        basis["new_files_manifest_sha256"] = hashlib.sha256(raw_new).hexdigest()
    if os.environ.get("ADCP_PRODUCTION_EVIDENCE"):
        retained = evidence / (label + "-artifacts")
        retained.mkdir(mode=0o700)
        for path in (wheel, source):
            shutil.copy2(path, retained / path.name)
        basis["retained_artifacts"] = str(retained)
    return basis


def historical_schema_fixture(root):
    """Copy immutable rc.3 reference inputs, never claim them as wheel contents.

    The installed suite retains historical rejection and correction controls.
    rc.3 is no longer a shipped bundle. The existing source-layout fallback
    can read these explicit test inputs without changing installed SDK code,
    its current bundle, or the public protocol-version allowlist.
    """
    version = "3.2.0-rc.3"
    source = ROOT / "schemas/cache" / version
    destination = root / "schemas/cache" / version
    assert not root.resolve().is_relative_to(ROOT.resolve())
    expected = {
        str(p.relative_to(source)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(source.rglob("*.json"))
    }
    assert expected
    if not destination.exists():
        shutil.copytree(source, destination)
    actual = {
        str(p.relative_to(destination)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(destination.rglob("*.json"))
    }
    assert actual == expected
    return {"version": version, "root": str(destination), "files": expected}


def installed_production(root, python, wheel, source, *, label, driver_absent):
    fixture_root = copied_fixtures(root, label)
    script = fixture_root / "run_installed.py"
    shutil.copy2(Path(__file__).with_name("_production_installed.py"), script)
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
            "mypy==1.20.2",
        ],
        label=label + "-production-conformance-dependencies",
        cwd=root,
        timeout=180,
    )
    modules, assets = inspect_distribution(wheel, source)
    tests = sorted(
        {
            *ROOT.glob("tests/conformance/reporting/test_reporting_production*.py"),
            *ROOT.glob("tests/conformance/reporting/test_reporting_projection*.py"),
        }
    )
    tests = [
        p
        for p in tests
        if p.name
        not in {"test_reporting_production_packaging.py", "test_reporting_production_rolling.py"}
    ]
    tests += [
        ROOT / name
        for name in (
            "tests/conformance/reporting/test_reporting_tier_projection.py",
            "tests/conformance/reporting/test_reporting_schedule_schema.py",
            "tests/test_reporting_revision_ownership.py",
            "tests/test_reporting_capability_models.py",
            "tests/test_reporting_production_public.py",
            "tests/test_schema_datetime_formats.py",
        )
    ]
    evidence = Path(os.environ.get("ADCP_PRODUCTION_EVIDENCE", str(root / "production-evidence")))
    settings = {
        "workspace": str(ROOT),
        "fixtures": str(fixture_root),
        "label": label,
        "modules": modules,
        "assets": assets,
        "schemas": {
            version: {
                name: hashlib.sha256(
                    (ROOT / "schemas/cache" / version / name).read_bytes()
                ).hexdigest()
                for name in SCHEMAS
            }
            for version in ("3.2.0-rc.6",)
        },
        "historical_reference_schema": historical_schema_fixture(root),
        "tests": [str(p.relative_to(ROOT)) for p in tests],
        "driver_absent": driver_absent,
        "python": [3, 10],
        "source_basis": source_basis(wheel, source, evidence=evidence, label=label),
        "wheel_sha256": hashlib.sha256(wheel.read_bytes()).hexdigest(),
        "evidence": str(evidence),
    }
    result = json.loads(
        run_step(
            [str(python), "-I", str(script)],
            label=label + "-installed-production",
            cwd=fixture_root,
            value=settings,
            timeout=1800,
        )
    )
    print(json.dumps({"installed_production": label, **result}), flush=True)
    assert result["result"]["valid"], result["result"]
    return result
