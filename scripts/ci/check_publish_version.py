"""Match a release tag to the built distribution's package and version."""

from __future__ import annotations

import argparse
import re
from pathlib import Path

from scripts.ci.check_release_artifacts import check_artifacts
from scripts.normalize_pyproject_prerelease import pep440_prerelease

VERSION = re.compile(r"\d+\.\d+\.\d+(?:-(?:alpha|beta|rc)\.(?:0|[1-9]\d*))?")
TAG_PREFIXES = {"adcp": "v", "adcp-reporting-storage": "adcp-reporting-storage-v"}


def check_publish_version(directory: Path, package: str, tag: str) -> None:
    prefix = TAG_PREFIXES.get(package)
    if prefix is None or not tag.startswith(prefix):
        raise ValueError("release tag does not match the selected package")
    version = tag.removeprefix(prefix)
    if VERSION.fullmatch(version) is None:
        raise ValueError("release tag has an invalid version")
    check_artifacts(directory, pep440_prerelease(version), package)
    print(f"{package} distributions match {tag}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    parser.add_argument("package")
    parser.add_argument("tag")
    args = parser.parse_args()
    check_publish_version(args.directory, args.package, args.tag)


if __name__ == "__main__":
    main()
