"""Floor-runtime conformance with complete installed module/schema provenance."""

import contextlib
import hashlib
import importlib
import importlib.metadata
import importlib.util
import json
import os
import subprocess
import sys
import time
from importlib.resources import files
from pathlib import Path

import pytest


def main(settings):
    root = Path(settings["fixtures"])
    workspace = Path(settings["workspace"])
    evidence = Path(settings["evidence"])
    evidence.mkdir(parents=True, exist_ok=True, mode=0o700)
    phases = []

    def enter_phase(name):
        # Closed phase names identify a failed preflight without publishing
        # arbitrary child stderr, provider detail or runtime values.
        phases.append(name)
        (evidence / (settings["label"] + "-phases.json")).write_text(
            json.dumps({"entered_phases": phases}) + "\n"
        )

    enter_phase("runtime")
    assert sys.version_info[:2] == tuple(settings["python"])
    assert not any(Path(p).resolve().is_relative_to(workspace) for p in sys.path)
    assert not (root / "adcp").exists() and not (root / "src").exists()
    sys.path.insert(0, str(root))
    from tests.conformance.reporting._hardening_installed import Results
    from tests.conformance.reporting._installed_progress import InstalledProgress

    progress = InstalledProgress(settings["progress"])

    enter_phase("installed_modules")
    origins = {}
    for name, expected in settings["modules"].items():
        path = Path(importlib.import_module(name).__file__).resolve()
        assert path.is_relative_to(Path(sys.prefix)) and not path.is_relative_to(workspace)
        assert hashlib.sha256(path.read_bytes()).hexdigest() == expected
        origins[name] = str(path)
    enter_phase("installed_assets")
    for name, expected in settings["assets"].items():
        assert (
            hashlib.sha256(files("adcp.reporting").joinpath(name).read_bytes()).hexdigest()
            == expected
        )
    from adcp.validation import schema_loader

    enter_phase("installed_current_schemas")
    assert set(settings["schemas"]) == {schema_loader._sdk_pinned_bundle_key()}
    for version, schemas in settings["schemas"].items():
        resolved = schema_loader._resolve_schema_root(version)
        assert resolved is not None
        schema_root = resolved.root
        assert schema_root.is_relative_to(Path(sys.prefix))
        for name, expected in schemas.items():
            assert hashlib.sha256((schema_root / name).read_bytes()).hexdigest() == expected
    enter_phase("historical_reference_schemas")
    reference = settings["historical_reference_schema"]
    reference_root = Path(reference["root"])
    assert not reference_root.is_relative_to(Path(sys.prefix))
    assert not reference_root.is_relative_to(workspace)
    assert reference["version"] not in settings["schemas"]
    historical_resolved = schema_loader._resolve_schema_root(reference["version"])
    assert historical_resolved is not None
    historical_root = historical_resolved.root
    assert historical_root.is_relative_to(Path(sys.prefix))
    assert not historical_root.is_relative_to(workspace)
    assert historical_root != reference_root

    def historical_manifest():
        return {
            str(p.relative_to(historical_root)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(historical_root.rglob("*.json"))
        }

    assert historical_manifest() == reference["files"]

    def reference_manifest():
        return {
            str(p.relative_to(reference_root)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(reference_root.rglob("*.json"))
        }

    assert reference_manifest() == reference["files"]
    enter_phase("optional_driver_boundary")
    if settings["driver_absent"]:
        assert importlib.util.find_spec("psycopg") is None
        assert importlib.util.find_spec("psycopg_pool") is None
        os.environ.pop("ADCP_PG_TEST_URL", None)
    else:
        assert os.environ.get("ADCP_PG_TEST_URL")
    reference_inputs = evidence / (settings["label"] + "-historical-schema-inputs.json")
    reference_bytes = (json.dumps(reference, indent=2, sort_keys=True) + "\n").encode()
    reference_inputs.write_bytes(reference_bytes)
    identity = {
        "python": sys.version,
        "source_basis": settings["source_basis"],
        "direct_url": json.loads(
            importlib.metadata.distribution("adcp").read_text("direct_url.json")
        ),
        "origins": origins,
        "distribution_version": importlib.metadata.version("adcp"),
        "wheel_sha256": settings["wheel_sha256"],
        "assets": settings["assets"],
        "schemas": settings["schemas"],
        "historical_reference_schema": {
            "version": reference["version"],
            "root": str(reference_root),
            "origin": "copied immutable test reference; independently compared with the packaged historical bundle",
            "files": len(reference["files"]),
            "inputs": str(reference_inputs),
            "inputs_sha256": hashlib.sha256(reference_bytes).hexdigest(),
        },
        "installed_historical_schema": {
            "version": reference["version"],
            "root": str(historical_root),
            "origin": "installed distribution",
            "files": len(reference["files"]),
        },
        "driver_absent": settings["driver_absent"],
    }
    # Preflight provenance survives an interrupted run without implying success.
    with (evidence / (settings["label"] + "-identity.json")).open("x") as stream:
        json.dump(identity, stream, indent=2)
        stream.write("\n")
    log = evidence / (settings["label"] + ".log")
    recorder = Results()
    command = [
        *(str(root / path) for path in settings["tests"]),
        "-v",
        "-s",
        "-ra",
        "-o",
        "asyncio_mode=auto",
        "-p",
        "no:cacheprovider",
        "--basetemp",
        str(root / "temp"),
        "--deselect=tests/test_reporting_capability_models.py::test_post_generation_repair_is_idempotent_for_both_actual_model_layouts",
    ]
    enter_phase("conformance")
    started = time.monotonic()
    progress.start("collection")
    with (
        log.open("x") as stream,
        contextlib.redirect_stdout(stream),
        contextlib.redirect_stderr(stream),
    ):
        code = int(pytest.main(command, plugins=[recorder, progress]))
    result = {
        "command": command,
        "pytest_exit": code,
        "passed": recorder.passed,
        "failed": recorder.failed,
        "errors": recorder.errors,
        "skipped": recorder.skipped,
        "deselected": recorder.deselected,
        "seconds": round(time.monotonic() - started, 3),
        "log": str(log),
        "bytes": log.stat().st_size,
        "sha256": hashlib.sha256(log.read_bytes()).hexdigest(),
        "valid": code == 0 and recorder.failed == recorder.errors == 0 and recorder.passed > 0,
    }
    if settings["driver_absent"]:
        assert recorder.skipped > 0
    else:
        result["valid"] &= recorder.skipped == 0
    result["valid"] &= recorder.deselected == 1
    config = root / "mypy.ini"
    config.write_text(
        "[mypy]\npython_version=3.10\nstrict=True\nplugins=adcp.types.mypy_plugin\nfollow_imports=silent\n"
    )
    typing_command = [
        sys.executable,
        "-I",
        "-m",
        "mypy",
        "--config-file",
        str(config),
        "--strict",
        "--no-incremental",
        str(root / "adopter.py"),
    ]
    progress.start("typing")
    enter_phase("strict_adopter")
    typed = subprocess.run(typing_command, cwd=root, capture_output=True, timeout=120)
    typing_log = evidence / (settings["label"] + "-adopter.log")
    typing_log.write_bytes(typed.stdout + typed.stderr)
    result["valid"] &= typed.returncode == 0
    progress.start("origins")
    enter_phase("final_origins_and_reference_preservation")
    for name, module in tuple(sys.modules.items()):
        if (name == "adcp" or name.startswith("adcp.")) and getattr(module, "__file__", None):
            assert Path(module.__file__).resolve().is_relative_to(Path(sys.prefix))
    assert reference_manifest() == reference["files"]
    assert historical_manifest() == reference["files"]
    progress.start("complete")
    progress.close()
    journal = progress.path.with_suffix(".jsonl")
    record = {
        **identity,
        "result": result,
        "progress": {
            "log": str(journal),
            "bytes": journal.stat().st_size,
            "sha256": hashlib.sha256(journal.read_bytes()).hexdigest(),
        },
        "adopter": {
            "command": typing_command,
            "exit": typed.returncode,
            "log": str(typing_log),
            "bytes": typing_log.stat().st_size,
            "sha256": hashlib.sha256(typing_log.read_bytes()).hexdigest(),
        },
    }
    (evidence / (settings["label"] + ".json")).write_text(json.dumps(record, indent=2) + "\n")
    enter_phase("record_complete")
    print(json.dumps(record), flush=True)


if __name__ == "__main__":
    main(json.load(sys.stdin))
