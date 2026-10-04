#!/usr/bin/env python3
"""Run ``datamodel-code-generator`` with a total input order.

The generator walks a directory input as ``sorted(path.rglob("*"), key=lambda
p: p.name)``: a stable sort on the *basename* alone. Every pair of inputs that
share a basename ties — 207 basenames in the pinned bundle are shared by two or
more directories (``media-buy/`` and ``creative/`` both have
``list-creative-formats-request.json``) — and a tie resolves in whatever order
``rglob`` yields, which is the filesystem's directory order: insertion order on
APFS, hash order on ext4. The anonymous variant classes the generator numbers
by traversal (``Result9`` versus ``Result13``) then differ between a developer
machine and CI, and a committed tree cannot be graded against a regeneration.

``pathlib.Path.rglob`` is patched here, in the generator's own process, to
yield its matches in a total order: full path, with the ``bundled/`` mirror of
the schema tree after everything else, which is the order the committed tree
was generated in. The generator's basename sort is stable, so a tie between
same-basename inputs now resolves by that order and no filesystem can perturb
it. Nothing else about the run changes:
``scripts/generate_types.py`` invokes this wrapper with the exact argument list
it used to hand to ``python -m datamodel_code_generator``.

``tests/test_code_generation.py::test_codegen_input_order_is_total`` holds
the property.
"""

from __future__ import annotations

import pathlib
import sys
from collections.abc import Iterator
from typing import Any

_original_rglob = pathlib.Path.rglob


def _input_order(path: pathlib.Path) -> tuple[bool, pathlib.Path]:
    """Full path, with the ``bundled/`` mirror after everything else.

    ``bundled/`` holds a second copy of schemas that also exist at the top
    level; the committed tree was generated with the top-level copy first, so
    the numbering adopters already see stays put. Within each half, the full
    path is the order.
    """
    return ("bundled" in path.parts, path)


def _sorted_rglob(
    self: pathlib.Path, pattern: str, *args: Any, **kwargs: Any
) -> Iterator[pathlib.Path]:
    return iter(sorted(_original_rglob(self, pattern, *args, **kwargs), key=_input_order))


def main(argv: list[str] | None = None) -> int:
    pathlib.Path.rglob = _sorted_rglob  # type: ignore[method-assign]
    from datamodel_code_generator.__main__ import main as codegen_main

    return int(codegen_main(sys.argv[1:] if argv is None else argv))


if __name__ == "__main__":
    sys.exit(main())
