#!/usr/bin/env python3
"""Vendor the unreleased ``sync_reporting_status`` schemas and generate preview models.

``sync_reporting_status`` merged to ``adcontextprotocol/adcp`` main but is not
in a cut release tag, so it is absent from this SDK's pinned schema bundle and
from ``adcp.types``.  Rather than hand-write the wire types -- which would rot
the moment rc.2 lands -- this script vendors the schemas at an exact upstream
commit and runs the same ``datamodel-code-generator`` the released types use.

Everything it produces is throwaway.  When the SDK repins to a bundle that
contains these schemas, delete ``src/adcp/reporting/_preview/`` entirely and
re-point :mod:`adcp.reporting.ledger.consumer_status` at ``adcp.types``.

Usage::

    python scripts/vendor_reporting_status_preview.py \\
        --source /path/to/adcp/static/schemas/source \\
        --commit 388e78e63e9f1824a4d430c0124ea4cbfc67c791
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
PREVIEW_ROOT = REPO_ROOT / "src" / "adcp" / "reporting" / "_preview"
SCHEMA_ROOT = PREVIEW_ROOT / "schemas"

#: The schemas this preview is actually about.  Their ref closure is vendored
#: alongside them so the bundle resolves without reaching the network.
ENTRY_POINTS = (
    "media-buy/sync-reporting-status-request.json",
    "media-buy/sync-reporting-status-response.json",
    "core/reporting-consumer-status.json",
)


def ref_closure(source: Path, entries: tuple[str, ...]) -> set[str]:
    """Every schema reachable from ``entries`` by ``$ref``."""
    closure = set(entries)
    queue = list(entries)
    while queue:
        current = json.loads((source / queue.pop()).read_text())
        for ref in _refs(current):
            relative = ref[len("/schemas/") :]
            if relative not in closure:
                closure.add(relative)
                queue.append(relative)
    return closure


def _refs(node: object) -> set[str]:
    found: set[str] = set()
    if isinstance(node, dict):
        ref = node.get("$ref")
        if isinstance(ref, str) and ref.startswith("/schemas/"):
            found.add(ref.split("#")[0])
        for value in node.values():
            found |= _refs(value)
    elif isinstance(node, list):
        for value in node:
            found |= _refs(value)
    return found


def rewrite_refs(node: object, depth: int) -> object:
    """Turn absolute ``/schemas/...`` refs into bundle-relative file paths.

    ``datamodel-code-generator`` resolves refs against the file on disk, so an
    absolute site path would send it to the network (or nowhere).
    """
    if isinstance(node, dict):
        rewritten: dict[str, object] = {}
        for key, value in node.items():
            if key == "$ref" and isinstance(value, str) and value.startswith("/schemas/"):
                target, _, fragment = value.partition("#")
                relative = ("../" * depth) + target[len("/schemas/") :]
                rewritten[key] = f"{relative}#{fragment}" if fragment else relative
            else:
                rewritten[key] = rewrite_refs(value, depth)
        return rewritten
    if isinstance(node, list):
        return [rewrite_refs(value, depth) for value in node]
    return node


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True, type=Path, help="adcp static/schemas/source")
    parser.add_argument("--commit", required=True, help="upstream commit the schemas came from")
    arguments = parser.parse_args()

    closure = sorted(ref_closure(arguments.source, ENTRY_POINTS))
    for relative in closure:
        destination = SCHEMA_ROOT / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        payload = json.loads((arguments.source / relative).read_text())
        depth = len(Path(relative).parts) - 1
        destination.write_text(
            json.dumps(rewrite_refs(payload, depth), indent=2, ensure_ascii=False) + "\n"
        )

    (SCHEMA_ROOT / "UPSTREAM_COMMIT").write_text(f"{arguments.commit}\n")

    generated = PREVIEW_ROOT / "consumer_status_models.py"
    command = [
        sys.executable,
        "-m",
        "datamodel_code_generator",
        "--input",
        str(SCHEMA_ROOT / "media-buy"),
        "--input-file-type",
        "jsonschema",
        "--output",
        str(PREVIEW_ROOT / "_generated_models"),
        "--output-model-type",
        "pydantic_v2.BaseModel",
        "--target-python-version",
        "3.10",
        "--use-annotated",
        "--use-standard-collections",
        "--use-union-operator",
        "--field-constraints",
        "--use-schema-description",
        "--use-field-description",
        "--snake-case-field",
        "--disable-timestamp",
    ]
    result = subprocess.run(command, cwd=REPO_ROOT, check=False)
    if result.returncode != 0:
        return result.returncode
    print(f"vendored {len(closure)} schemas at {arguments.commit}")
    print(f"generated models under {generated.parent}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
