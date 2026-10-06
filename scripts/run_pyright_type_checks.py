#!/usr/bin/env python3
"""Grade the adopter type-check fixtures under pyright as well as mypy (#1416).

``tests/type_checks/`` is what an adopter's own strict type-check sees of this
SDK. CI graded it with mypy only, and mypy ran with ``adcp.types.mypy_plugin``,
so an annotation only that plugin understands passed CI while every pyright
and Pylance user saw errors. This runs pyright over the same fixtures.

A fixture pyright cannot grade yet is listed in ``EXCLUDED`` with the reason;
the list may only shrink. Pass ``--all`` to run the excluded fixtures too and
see what is left to fix.
"""

from __future__ import annotations

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
