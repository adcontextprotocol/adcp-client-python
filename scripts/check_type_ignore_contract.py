#!/usr/bin/env python3
"""Fail if adopter type-check fixtures rely on type-ignore suppressions."""

from __future__ import annotations

import sys
import tokenize
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TYPE_CHECK_DIR = ROOT / "tests" / "type_checks"


def type_ignore_comments(path: Path) -> list[tuple[int, str]]:
    findings: list[tuple[int, str]] = []
    with path.open("rb") as handle:
        for token in tokenize.tokenize(handle.readline):
            if token.type != tokenize.COMMENT:
                continue
            comment = token.string.lstrip("#").strip()
            if comment.startswith("type: ignore"):
                findings.append((token.start[0], token.string.strip()))
    return findings


def main() -> int:
    failures: list[str] = []
    for path in sorted(TYPE_CHECK_DIR.rglob("*.py")):
        for line_no, comment in type_ignore_comments(path):
            rel_path = path.relative_to(ROOT)
            failures.append(f"{rel_path}:{line_no}: {comment}")

    if not failures:
        return 0

    print(
        "tests/type_checks/ fixtures must pass mypy --strict without type-ignore suppressions.",
        file=sys.stderr,
    )
    for failure in failures:
        print(failure, file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
