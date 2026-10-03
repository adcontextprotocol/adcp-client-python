#!/usr/bin/env python3
"""Regenerate tests/fixtures/public_api_snapshot.json.

The snapshot records, for every exported name, WHAT that name resolves to —
``module:QualName`` for a class or function, the module path for a submodule, a
structural key for a union. Run this after an intentional addition, removal or
repoint, and review the diff: a changed value is a name that now means a
different object to every adopter who imports it.

Recording resolutions rather than names is what lets
``tests/test_public_api.py::test_public_api_surface_matches_snapshot`` fail on a
repoint. ``_generated`` resolves a bare type name that several generated modules
define by sort order and stem preference, so a schema addition can take a name
an adopter already imports; with names alone that ships green.

Usage:
    python scripts/regenerate_public_api_snapshot.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from scripts.export_resolution import snapshot_entry, snapshot_modules  # noqa: E402

SNAPSHOT_PATH = Path(__file__).parent.parent / "tests" / "fixtures" / "public_api_snapshot.json"


def main() -> None:
    snapshot = {name: snapshot_entry(module) for name, module in snapshot_modules()}
    SNAPSHOT_PATH.write_text(json.dumps(snapshot, indent=2, sort_keys=True) + "\n")
    print(f"Wrote {SNAPSHOT_PATH}")
    for name, entry in snapshot.items():
        print(f"  {name}: {len(entry['resolves'])} names")
    print(f"  total: {sum(len(e['resolves']) for e in snapshot.values())}")


if __name__ == "__main__":
    main()
