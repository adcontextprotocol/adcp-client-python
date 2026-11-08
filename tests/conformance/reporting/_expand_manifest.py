"""Derive ``outbox/required_expand_schema.json`` from an expanded ledger's catalog.

An earlier release's ledger (adcp-reporting-storage 0.1.x/0.2.0b0), expanded
by this checkout, keeps what that release needs under its original names, so a
few required objects of this release's manifests differ there (the ``alternates``)
and the widened ones exist under new names (the ``objects``). The published
upgrade test seeds such a ledger with the actual wheels, expands it, and
asserts the packaged file equals what this derives from its catalog.

Regenerate after changing an expanded object: run that test with
``ADCP_WRITE_EXPAND_MANIFEST=1``.
"""

from __future__ import annotations

import json
from typing import Any

#: Name suffixes of the objects that exist only while a ledger is expanded.
EXPAND_SUFFIXES = (
    "_generation_key",
    "_offering_period_key",
    "_uncontracted",
    "_offering_key",
    "_offering_fkey",
)
#: Every function alternate needs the expanded configuration key.
FUNCTION_REQUIRES = ("constraint:reporting_configurations.reporting_configurations_generation_key",)


def _table(key: str) -> str | None:
    kind, _, name = key.partition(":")
    if kind == "function":
        return None
    return name.split(".", 1)[0]


def _expand_only(key: str) -> bool:
    kind, _, name = key.partition(":")
    return kind in ("constraint", "index") and name.rsplit(".", 1)[-1].endswith(EXPAND_SUFFIXES)


def derive(installed: dict[str, dict[str, Any]], manifests: dict[str, dict[str, Any]]) -> dict:
    """``{"alternates": ..., "objects": ...}`` for an expanded catalog and the manifests."""
    objects = {key: installed[key] for key in sorted(installed) if _expand_only(key)}
    alternates: dict[str, dict[str, Any]] = {}
    for key in sorted(manifests):
        actual = installed.get(key)
        if actual is None or actual == manifests[key]:
            continue
        table = _table(key)
        requires = (
            list(FUNCTION_REQUIRES)
            if table is None
            else [item for item in objects if _table(item) == table]
        )
        assert requires, f"{key} differs while expanded but nothing expanded replaces it"
        alternates[key] = {**actual, "requires": requires}
    return {"alternates": alternates, "objects": objects}


def render(manifest: dict) -> str:
    return json.dumps(manifest, sort_keys=True, indent=2) + "\n"
