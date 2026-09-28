"""Fail a release build before upload if either distribution approaches PyPI's limit."""

from __future__ import annotations

import sys
import tarfile
import zipfile
from pathlib import Path

MAX_BYTES = 90_000_000
RETIRED_BUNDLES = {"2.5", "3.2.0-beta.6", "3.2.0-rc.3", "3.2.0-rc.6"}


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


def check_retired_schema_bundles(wheel: Path, source: Path) -> None:
    """Reject superseded schema directories in either release artifact."""
    with zipfile.ZipFile(wheel) as archive:
        wheel_members = archive.namelist()
    with tarfile.open(source) as archive:
        source_members = archive.getnames()
    for artifact, members in ((wheel, wheel_members), (source, source_members)):
        for name in members:
            path = Path(name)
            retired = RETIRED_BUNDLES.intersection((*path.parts, path.stem))
            if retired:
                raise ValueError(
                    f"{artifact.name} contains retired schema bundle {sorted(retired)[0]}"
                )


if __name__ == "__main__":
    try:
        wheel, source = check_distribution_size(
            Path(sys.argv[1]) if len(sys.argv) == 2 else Path("dist")
        )
        check_retired_schema_bundles(wheel, source)
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
