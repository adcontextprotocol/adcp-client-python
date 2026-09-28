"""Fail a release build before upload if either distribution approaches PyPI's limit."""

from __future__ import annotations

import sys
from pathlib import Path

MAX_BYTES = 90_000_000


def check_distribution_size(directory: Path) -> tuple[Path, Path]:
    """Require one wheel and one sdist, each below the release budget."""
    artifacts = []
    for pattern in ("*.whl", "*.tar.gz"):
        found = sorted(directory.glob(pattern))
        if len(found) != 1:
            raise ValueError(f"expected one {pattern} in {directory}, found {len(found)}")
        artifacts.append(found[0])
    for artifact in artifacts:
        size = artifact.stat().st_size
        if size > MAX_BYTES:
            raise ValueError(f"{artifact.name} is {size} bytes; limit is {MAX_BYTES} bytes")
        print(f"{artifact.name}: {size} bytes (limit {MAX_BYTES})")
    return artifacts[0], artifacts[1]


if __name__ == "__main__":
    try:
        check_distribution_size(Path(sys.argv[1]) if len(sys.argv) == 2 else Path("dist"))
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
