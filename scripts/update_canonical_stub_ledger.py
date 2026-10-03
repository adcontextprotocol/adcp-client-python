#!/usr/bin/env python3
"""Rewrite the canonical-stub field-gap ledger from the current gap.

``canonical_creative.pyi`` omits runtime fields that its models carry, and
``tests/test_protocol_envelope_inheritance.py`` holds the omissions to a ledger
that may only shrink. Declare a field in the stub, then run this to drop it from
the ledger. The ledger cannot grow: a field the stub stops declaring fails the
test, and this script refuses to record it.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))
sys.path.insert(0, str(REPO_ROOT))

from tests.canonical_stub_gap import LEDGER_FILE, stub_field_gap  # noqa: E402


def main() -> int:
    recorded: dict[str, list[str]] = json.loads(LEDGER_FILE.read_text(encoding="utf-8"))["gap"]
    measured = stub_field_gap()

    grown = {
        name: sorted(set(fields) - set(recorded.get(name, ())))
        for name, fields in measured.items()
        if set(fields) - set(recorded.get(name, ()))
    }
    if grown:
        print("✗ The stub stopped declaring fields it used to declare:", file=sys.stderr)
        for name, fields in grown.items():
            print(f"    {name}: {', '.join(fields)}", file=sys.stderr)
        print("  Declare them again rather than recording the loss.", file=sys.stderr)
        return 1

    before = sum(len(fields) for fields in recorded.values())
    after = sum(len(fields) for fields in measured.values())
    LEDGER_FILE.write_text(
        json.dumps({"_comment": _COMMENT, "gap": measured}, indent=2) + "\n",
        encoding="utf-8",
    )
    print(f"Wrote {LEDGER_FILE.name}: {after} undeclared field(s) across {len(measured)} class(es)")
    print(f"  {before - after} closed since the last update")
    return 0


_COMMENT = (
    "Runtime fields that src/adcp/types/canonical_creative.pyi does not declare, so a "
    "type checker rejects reading them and rejects passing them to the constructor. "
    "Shrink-only: declare the field in the stub, then run "
    "`python scripts/update_canonical_stub_ledger.py`. Graded by "
    "tests/test_protocol_envelope_inheritance.py::test_canonical_stub_declares_every_runtime_field."
)


if __name__ == "__main__":
    sys.exit(main())
