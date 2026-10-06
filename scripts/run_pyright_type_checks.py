#!/usr/bin/env python3
"""Grade the adopter type-check fixtures under pyright as well as mypy (#1416).

``tests/type_checks/`` is what an adopter's own strict type-check sees of this
SDK. CI graded it with mypy only, and mypy ran with ``adcp.types.mypy_plugin``,
so an annotation only that plugin understands passed CI while every pyright
and Pylance user saw errors. This runs pyright over the same fixtures.

A fixture pyright cannot grade yet is listed in ``EXCLUDED`` with the reason;
the list may only shrink. Pass ``--all`` to run the excluded fixtures too and
see what is left to fix.

Several fixtures import the ``[pg]`` extra's ``psycopg``. mypy tolerates those
modules being absent through ``ignore_missing_imports``; pyright has no
per-module equivalent, and an unresolved import is an error, not an ``Any``.
So the gate requires the extra rather than skipping the fixture: a fixture
whose top-level imports this interpreter cannot find fails the run up front,
naming the module and the extra to install, and the CI step installs it.
"""

from __future__ import annotations

import ast
import importlib.util
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TYPE_CHECK_DIR = ROOT / "tests" / "type_checks"

#: Fixtures pyright rejects today for reasons unrelated to what they grade.
#: Each entry names the pyright-only diagnostic so the fix is findable.
EXCLUDED: dict[str, str] = {
    "composing_root_subclassing.py": (
        "reportIncompatibleVariableOverride: pyright treats a covariant override "
        "of a mutable field as incompatible; mypy admits it"
    ),
    "cross_class_override_with_schema_variant.py": (
        "SchemaVariant is a mypy-plugin marker with no pyright equivalent "
        "(see adcp/types/variants.py)"
    ),
    "extend_response_with_sequence.py": (
        "reportIncompatibleVariableOverride on the covariant Sequence override"
    ),
    "media_buy_actions.py": (
        "reportAttributeAccessIssue: pyright does not resolve DurationUnit through "
        "the lazy adcp.types facade"
    ),
    "reporting_buyer_submission_intents.py": (
        "reportArgumentType: ADCPClient against the ReportingReceiptSubmissionClient protocol"
    ),
    "reporting_durable_materializer.py": (
        "reportAssignmentType: store classes against the reporting store protocols "
        "(Coroutine vs CoroutineType return types)"
    ),
    "reporting_frozen_feed.py": (
        "reportAssignmentType: store classes against the reporting store protocols"
    ),
    "reporting_inline_storage.py": (
        "reportAssignmentType: store classes against the reporting store protocols"
    ),
    "reporting_receipt_ingress.py": (
        "reportAssignmentType: store classes against the reporting store protocols"
    ),
    "targeting_input_field_types.py": (
        "reportAssertTypeFailure: pyright reads GeoCountry where mypy reads str"
    ),
    "versioned_types.py": ("reportTypedDictNotRequiredAccess on NotRequired TypedDict keys"),
}


def fixtures(*, include_excluded: bool) -> list[Path]:
    return sorted(
        path
        for path in TYPE_CHECK_DIR.glob("*.py")
        if include_excluded or path.name not in EXCLUDED
    )


def missing_imports(path: Path) -> list[str]:
    """Top-level packages ``path`` imports that this interpreter cannot find.

    Checked before pyright runs so a missing optional dependency is reported as
    what it is — an incomplete environment — instead of as type errors in the
    fixture, and so a fixture is never silently dropped from the gate.
    """
    roots: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            roots.update(alias.name.partition(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
            roots.add(node.module.partition(".")[0])
    missing: list[str] = []
    for root in sorted(roots):
        try:
            found = importlib.util.find_spec(root) is not None
        except (ImportError, ValueError):
            found = False
        if not found:
            missing.append(root)
    return missing


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    include_excluded = "--all" in args

    stale = sorted(name for name in EXCLUDED if not (TYPE_CHECK_DIR / name).exists())
    if stale:
        print("run_pyright_type_checks: EXCLUDED names fixtures that no longer exist:")
        for name in stale:
            print(f"  {name}")
        return 1

    selected = fixtures(include_excluded=include_excluded)
    unavailable = {path.name: missing_imports(path) for path in selected}
    unavailable = {name: roots for name, roots in unavailable.items() if roots}
    if unavailable:
        print(
            "run_pyright_type_checks: these fixtures import modules this interpreter "
            "cannot find; install the optional extras (e.g. `uv sync --all-extras` or "
            "`pip install -e '.[dev,pg]'`) so pyright grades every fixture:"
        )
        for name, roots in sorted(unavailable.items()):
            print(f"  {name}: {', '.join(roots)}")
        return 1
    command = [
        sys.executable,
        "-m",
        "pyright",
        "--pythonpath",
        sys.executable,
        *(str(path) for path in selected),
    ]
    result = subprocess.run(command, cwd=ROOT)
    if result.returncode == 0:
        skipped = "" if include_excluded else f", {len(EXCLUDED)} excluded"
        print(f"✓ pyright passed on {len(selected)} adopter type-check fixtures{skipped}")
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
