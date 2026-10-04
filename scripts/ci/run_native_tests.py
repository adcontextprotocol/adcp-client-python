"""Run canonical/full CI or a bounded interpreter-compatibility suite.

Only the unit phase may use workers. Conformance includes real distributions,
shared build caches, subprocesses, and databases, so it remains sequential.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SERIAL_PATTERNS = ("test_*_artifacts.py", "test_*_packaging.py")
COMPATIBILITY_TESTS = (
    "tests/test_adagents.py",
    "tests/test_adagents_matching.py",
    "tests/test_adagents_fetch_api.py",
    "tests/test_adagents_cache_validation.py",
    "tests/test_adagents_ads_redirects.py",
    "tests/test_client.py",
    "tests/test_client_server_version.py",
    "tests/test_public_api.py",
    "tests/test_lazy_types.py::test_import_adcp_does_not_build_schema_graph",
    "tests/test_lazy_types.py::test_import_adcp_types_does_not_build_schema_graph",
    "tests/test_lazy_types.py::test_schema_symbol_triggers_build",
    "tests/test_lazy_types.py::test_lazy_types_surface_matches_eager",
    "tests/test_type_guards.py",
    "tests/test_type_aliases.py",
    "tests/test_version_helpers.py",
    "tests/test_version_scoped_models.py",
    "tests/test_versioned_bases.py",
    "tests/test_validation_envelope.py",
    "tests/test_auth_credential_parity.py",
    "tests/test_mcp_reusable_lifespan.py",
)


def test_commands(mode: str, *, coverage: bool = False, workers: int = 0) -> list[list[str]]:
    if mode == "compatibility":
        if coverage:
            raise ValueError("coverage belongs to the canonical full suite")
        return [
            [
                *COMPATIBILITY_TESTS,
                # Static adopter checks run in the canonical lane; keep runtime
                # versioned-base checks on every interpreter.
                "--deselect=tests/test_versioned_bases.py::test_named_adopter_with_static_checkers",
                "-n",
                "0",
                "-v",
                "-ra",
                "--durations=20",
            ]
        ]
    if mode != "full" or workers not in (0, 2):
        raise ValueError("use full/compatibility mode and zero or two unit workers")
    unit = [
        "tests/",
        "--ignore=tests/conformance",
        "--ignore=tests/integration",
        *(f"--ignore-glob=tests/{pattern}" for pattern in SERIAL_PATTERNS),
        "-n",
        str(workers),
        "-v",
        "-ra",
        "--durations=20",
    ]
    if workers:
        unit.append("--dist=loadfile")
    conformance = [
        "tests/conformance/",
        "tests/integration/",
        *sorted(
            {
                str(path.relative_to(ROOT))
                for pattern in SERIAL_PATTERNS
                for path in (ROOT / "tests").glob(pattern)
            }
        ),
        "-n",
        "0",
        "-v",
        "-ra",
        "--durations=20",
    ]
    if coverage:
        # Enforce the repository's coverage floor only after the sequential
        # phase appends its measurements, not on the partial unit result.
        unit.extend(["--cov=src/adcp", "--cov-report=", "--cov-fail-under=0"])
        conformance.extend(["--cov=src/adcp", "--cov-append", "--cov-report=term-missing"])
    return [unit, conformance]


def run_commands(commands: list[list[str]]) -> int:
    for arguments in commands:
        command = [
            sys.executable,
            str(ROOT / "scripts/reporting_test_harness.py"),
            sys.executable,
            "-m",
            "pytest",
            *arguments,
        ]
        print(json.dumps({"native_test_command": command}), flush=True)
        result = subprocess.run(command, cwd=ROOT, check=False)
        if result.returncode:
            return result.returncode
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("full", "compatibility"), required=True)
    parser.add_argument("--coverage", action="store_true")
    parser.add_argument("--unit-workers", type=int, choices=(0, 2), default=0)
    args = parser.parse_args()
    return run_commands(test_commands(args.mode, coverage=args.coverage, workers=args.unit_workers))


if __name__ == "__main__":
    raise SystemExit(main())
