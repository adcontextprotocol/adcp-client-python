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
    assert sys.version_info[:2] == tuple(settings["python"])
    assert not any(Path(p).resolve().is_relative_to(workspace) for p in sys.path)
    assert not (root / "adcp").exists() and not (root / "src").exists()
    sys.path.insert(0, str(root))
    from tests.conformance.reporting._hardening_installed import Results
    from tests.conformance.reporting._installed_progress import InstalledProgress

    progress = InstalledProgress(settings["progress"])

    origins = {}
    for name, expected in settings["modules"].items():
        path = Path(importlib.import_module(name).__file__).resolve()
        assert path.is_relative_to(Path(sys.prefix)) and not path.is_relative_to(workspace)
        assert hashlib.sha256(path.read_bytes()).hexdigest() == expected
        origins[name] = str(path)
    for name, expected in settings["assets"].items():
        assert (
            hashlib.sha256(files("adcp.reporting").joinpath(name).read_bytes()).hexdigest()
            == expected
        )
    from adcp.validation import schema_loader

    schema_root = schema_loader._resolve_schema_root("3.2.0-rc.3").root
    assert schema_root.is_relative_to(Path(sys.prefix))
    for name, expected in settings["schemas"].items():
        assert hashlib.sha256((schema_root / name).read_bytes()).hexdigest() == expected
    current_root = schema_loader._resolve_schema_root(None).root
    assert current_root.is_relative_to(Path(sys.prefix))
    assert current_root.name == "3.2.0-rc.4"
    for name, expected in settings["current_schemas"].items():
        assert hashlib.sha256((current_root / name).read_bytes()).hexdigest() == expected
    if settings["driver_absent"]:
        assert importlib.util.find_spec("psycopg") is None
        assert importlib.util.find_spec("psycopg_pool") is None
        os.environ.pop("ADCP_PG_TEST_URL", None)
    else:
        assert os.environ.get("ADCP_PG_TEST_URL")
    evidence = Path(settings["evidence"])
    evidence.mkdir(parents=True, exist_ok=True, mode=0o700)
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
        "current_schemas": settings["current_schemas"],
        "driver_absent": settings["driver_absent"],
    }
    # A timed-out pytest run still retains the identities verified before it.
    # This is provenance, not a successful conformance result.
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
    typed = subprocess.run(typing_command, cwd=root, capture_output=True, timeout=120)
    typing_log = evidence / (settings["label"] + "-adopter.log")
    typing_log.write_bytes(typed.stdout + typed.stderr)
    result["valid"] &= typed.returncode == 0
    progress.start("origins")
    for name, module in tuple(sys.modules.items()):
        if (name == "adcp" or name.startswith("adcp.")) and getattr(module, "__file__", None):
            assert Path(module.__file__).resolve().is_relative_to(Path(sys.prefix))
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
    print(json.dumps(record), flush=True)


if __name__ == "__main__":
    main(json.load(sys.stdin))
