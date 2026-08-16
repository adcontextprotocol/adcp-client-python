"""Shared loader and completeness pin for the AdCP request-signing vectors.

The vectors under ``tests/conformance/vectors/request-signing/`` are vendored
verbatim from the AdCP spec repo. They are the graded conformance contract, so
the property that matters most about the vendored copy is that it is
*complete* -- a vector that never lands on disk is a rule nobody grades.

Globbing the directory and asserting the result is non-empty cannot express
that: any non-empty subset passes, so a missing vector is indistinguishable
from a complete set. That is not hypothetical. Before this module existed the
vendored copy was missing 12 of the spec's 40 vectors plus
``canonicalization.json`` entirely, and three further vectors had drifted from
their upstream contents -- all while the suite reported green.

``vector_manifest.json`` closes that hole by pinning the SHA-256 of every
vendored file. It fails on a missing file, an unexpected extra file, and on
silent content drift -- the last being what happened to the three stale
vectors, and what a filename-only manifest would have missed.

Re-pinning after a spec bump
----------------------------
Copy the new vector tree over ``tests/conformance/vectors/request-signing/``,
update ``src/adcp/ADCP_VERSION``, then regenerate the pin::

    python -m tests.conformance.signing.vectors --write

Review the resulting diff: every changed hash is a spec change the SDK must be
re-graded against, not a rubber stamp.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

VECTORS_DIR = Path(__file__).parent.parent / "vectors" / "request-signing"
MANIFEST_PATH = Path(__file__).parent / "vector_manifest.json"

_MANIFEST = json.loads(MANIFEST_PATH.read_text())

#: AdCP spec release these vectors were vendored from. Kept equal to the
#: SDK-wide pin in ``src/adcp/ADCP_VERSION`` so that bumping the targeted spec
#: version without re-vendoring the vectors fails loudly rather than leaving
#: the SDK graded against a stale contract.
VECTOR_SET_SPEC_VERSION: str = _MANIFEST["spec_version"]

#: SHA-256 of every vendored file, relative to :data:`VECTORS_DIR`.
VECTOR_MANIFEST: dict[str, str] = _MANIFEST["files"]


def manifest_ids(subdir: str) -> tuple[str, ...]:
    """Filenames the manifest expects under ``subdir`` ("positive"/"negative")."""
    prefix = f"{subdir}/"
    return tuple(sorted(name[len(prefix) :] for name in VECTOR_MANIFEST if name.startswith(prefix)))


def on_disk_files() -> dict[str, str]:
    """SHA-256 of every file actually present under :data:`VECTORS_DIR`."""
    return {
        path.relative_to(VECTORS_DIR).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in VECTORS_DIR.rglob("*")
        if path.is_file()
    }


def load_vector_set(subdir: str) -> list[tuple[str, Path]]:
    """Parametrization list for ``subdir``, gated on manifest completeness.

    Raises if the directory does not hold exactly the manifest's filenames, so
    a vector that goes missing takes the suite red at collection time instead
    of silently shrinking the graded set.
    """
    expected = set(manifest_ids(subdir))
    directory = VECTORS_DIR / subdir
    present = {path.name for path in directory.glob("*.json")}

    missing = sorted(expected - present)
    unexpected = sorted(present - expected)
    if missing or unexpected:
        raise AssertionError(
            f"{subdir} vector set does not match the manifest for AdCP "
            f"{VECTOR_SET_SPEC_VERSION}\n"
            f"  missing:    {missing or 'none'}\n"
            f"  unexpected: {unexpected or 'none'}\n"
            "Re-vendor from the spec repo, then regenerate the pin with "
            "`python -m tests.conformance.signing.vectors --write`."
        )

    return [(name, directory / name) for name in sorted(expected)]


def load_canonicalization_cases() -> list[tuple[str, dict[str, Any]]]:
    """Cases from ``canonicalization.json`` (pure URL canonicalization, no crypto)."""
    document = json.loads((VECTORS_DIR / "canonicalization.json").read_text())
    return [(case["name"], case) for case in document["cases"]]


def _write_manifest() -> None:
    # Shallow copy, then REPLACE "files" rather than mutating it in place --
    # ``_MANIFEST["files"]`` is what ``VECTOR_MANIFEST`` is bound to, and
    # mutating it here would rewrite the pin the tests grade against.
    document = dict(_MANIFEST)
    document["files"] = dict(sorted(on_disk_files().items()))
    MANIFEST_PATH.write_text(json.dumps(document, indent=2) + "\n")
    print(f"wrote {MANIFEST_PATH} with {len(document['files'])} entries")


if __name__ == "__main__":
    import sys

    if "--write" in sys.argv:
        _write_manifest()
    else:
        print(json.dumps(dict(sorted(on_disk_files().items())), indent=2))
