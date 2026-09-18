"""Run copied conformance fixtures against non-editable installed SDK bytes only."""

import contextlib
import hashlib
import importlib
import json
import os
import sys
import time
from importlib.resources import files
from pathlib import Path

import pytest


class Results:
    def __init__(self):
        self.passed = self.failed = self.skipped = self.errors = self.deselected = 0
        self.failures = []

    def pytest_runtest_logreport(self, report):
        if report.skipped:
            self.skipped += 1
        elif report.failed:
            if report.when == "call":
                self.failed += 1
                self.failures.append((report.nodeid, report.longreprtext))
            else:
                self.errors += 1
        elif report.when == "call":
            self.passed += 1

    def pytest_collectreport(self, report):
        if report.failed:
            self.errors += 1

    def pytest_deselected(self, items):
        self.deselected += len(items)


def main(settings):
    root = Path(settings["fixtures"])
    workspace = Path(settings["workspace"])
    assert sys.version_info[:2] == tuple(settings["python"])
    assert not any(Path(p).resolve().is_relative_to(workspace) for p in sys.path)
    # This directory contains copied test fixtures, never src/adcp or an adcp alias.
    assert not (root / "adcp").exists() and not (root / "src").exists()
    sys.path.insert(0, str(root))
    origins = {}
    for name, expected in settings["modules"].items():
        path = Path(importlib.import_module(name).__file__).resolve()
        assert "site-packages" in str(path) and not path.is_relative_to(workspace)
        assert hashlib.sha256(path.read_bytes()).hexdigest() == expected
        origins[name] = str(path)
    for name, expected in settings["assets"].items():
        raw = files("adcp.reporting").joinpath(name).read_bytes()
        assert hashlib.sha256(raw).hexdigest() == expected
    if settings["driver_absent"]:
        assert importlib.util.find_spec("psycopg") is None
        assert importlib.util.find_spec("psycopg_pool") is None
        os.environ.pop("ADCP_PG_TEST_URL", None)
    evidence = Path(settings["evidence"])
    evidence.mkdir(parents=True, exist_ok=True, mode=0o700)
    phases = [("green", None)]
    if settings["parent"]:
        phases = [
            (
                "negative",
                "startup_then_sequential or concurrent_cold or mounted_unexpected"
                " or original_pg_execute",
            ),
            ("preservation", "expected_closed or cancellation or actual_domain_rejections"),
        ]
    outputs = []
    for name, selection in phases:
        log = evidence / f"{settings['label']}-{name}.log"
        recorder = Results()
        command = [
            str(root / "tests/conformance/reporting/test_reporting_activity_schema_proof.py"),
            str(root / "tests/conformance/reporting/test_reporting_receipt_diagnostics.py"),
            "-v",
            "-s",
            "-ra",
            "-o",
            "asyncio_mode=auto",
            "-p",
            "no:cacheprovider",
            "--basetemp",
            str(root / f"temp-{name}"),
        ]
        if selection:
            command += ["-k", selection]
        started = time.monotonic()
        with (
            log.open("w") as stream,
            contextlib.redirect_stdout(stream),
            contextlib.redirect_stderr(stream),
        ):
            code = int(pytest.main(command, plugins=[recorder]))
        runtime = round(time.monotonic() - started, 3)
        valid = code == 0 and recorder.failed == recorder.errors == 0 and recorder.passed > 0
        if name == "negative":
            valid = (
                code == 1
                and recorder.failed == 30
                and recorder.errors == recorder.skipped == 0
                and all(
                    (
                        "assert 0 == 1" in reason
                        if "receipt_diagnostics" in node
                        else any(
                            f"assert ({scans}, {checkouts}) == (1, 1)" in reason
                            for scans, checkouts in ((9, 9), (27, 9), (12, 12), (36, 12))
                        )
                    )
                    for node, reason in recorder.failures
                )
            )
        result = {
            "phase": name,
            "command": command,
            "pytest_exit": code,
            "valid": valid,
            "passed": recorder.passed,
            "failed": recorder.failed,
            "errors": recorder.errors,
            "skipped": recorder.skipped,
            "deselected": recorder.deselected,
            "runtime_seconds": runtime,
            "log_path": str(log),
            "log_sha256": hashlib.sha256(log.read_bytes()).hexdigest(),
            "log_bytes": log.stat().st_size,
        }
        outputs.append(result)
        (evidence / f"{settings['label']}-{name}.json").write_text(
            json.dumps(result, indent=2) + "\n"
        )
    # Inspect all SDK modules loaded by pytest, not only the initial shortlist.
    for name, module in tuple(sys.modules.items()):
        if name == "adcp" or name.startswith("adcp."):
            path = getattr(module, "__file__", None)
            if path is not None:
                assert Path(path).resolve().is_relative_to(Path(sys.prefix))
    print(
        json.dumps(
            {
                "python": sys.version,
                "origins": origins,
                "assets": settings["assets"],
                "results": outputs,
            }
        )
    )


if __name__ == "__main__":
    main(json.load(sys.stdin))
