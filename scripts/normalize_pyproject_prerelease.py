#!/usr/bin/env python3
"""Normalize release-please prerelease versions in pyproject.toml."""

from __future__ import annotations

import argparse
import re
from pathlib import Path

_SEMVER_PRERELEASE_RE = re.compile(
    r"^(?P<release>\d+\.\d+\.\d+)-(?P<label>alpha|beta|rc)\.(?P<number>0|[1-9]\d*)$"
)
_PYPROJECT_SECTION_RE = re.compile(r"^\s*\[(?P<section>[^\]]+)\]\s*$")
_PYPROJECT_VERSION_LINE_RE = re.compile(
    r'^(?P<prefix>\s*version\s*=\s*")'
    r"(?P<version>\d+\.\d+\.\d+-(?:alpha|beta|rc)\.(?:0|[1-9]\d*))"
    r'(?P<suffix>"\s*)$'
)
_PEP440_LABELS = {
    "alpha": "a",
    "beta": "b",
    "rc": "rc",
}


def pep440_prerelease(version: str) -> str:
    """Convert a SemVer prerelease version to PEP 440 when needed."""
    match = _SEMVER_PRERELEASE_RE.fullmatch(version)
    if not match:
        return version

    label = _PEP440_LABELS[match.group("label")]
    return f"{match.group('release')}{label}{match.group('number')}"


def normalize_pyproject_text(text: str) -> str:
    """Normalize a pyproject.toml project version line if it is a prerelease."""

    lines = text.splitlines(keepends=True)
    current_section: str | None = None
    for index, line in enumerate(lines):
        content = line.removesuffix("\n")
        newline = "\n" if line.endswith("\n") else ""
        section_match = _PYPROJECT_SECTION_RE.match(content)
        if section_match:
            current_section = section_match.group("section").strip()
            continue

        if current_section != "project":
            continue

        version_match = _PYPROJECT_VERSION_LINE_RE.match(content)
        if not version_match:
            continue

        lines[index] = (
            f"{version_match.group('prefix')}"
            f"{pep440_prerelease(version_match.group('version'))}"
            f"{version_match.group('suffix')}"
            f"{newline}"
        )

    return "".join(lines)


def normalize_pyproject(path: Path) -> bool:
    """Normalize pyproject.toml in place.

    Returns True when the file changed.
    """
    text = path.read_text(encoding="utf-8")
    normalized = normalize_pyproject_text(text)
    if normalized == text:
        return False

    path.write_text(normalized, encoding="utf-8")
    return True


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Normalize release-please SemVer prereleases to PEP 440 in pyproject.toml."
    )
    parser.add_argument(
        "path",
        nargs="?",
        default="pyproject.toml",
        type=Path,
        help="Path to pyproject.toml.",
    )
    args = parser.parse_args()

    changed = normalize_pyproject(args.path)
    if changed:
        print(f"Normalized prerelease version in {args.path}")
    else:
        print(f"No prerelease normalization needed for {args.path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
