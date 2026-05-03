"""Ensure env-var names in public docstrings are backed by actual
``os.environ`` reads in the source tree.

Motivated by the drift in ``client.py:381-388``: the docstring claimed
``PYTHON_ENV`` / ``ENV`` / ``ENVIRONMENT`` were honored; only ``ADCP_ENV``
was. This test prevents that class of documentation drift from re-entering
silently.

Detection rules (applied per docstring line):

- **Pattern A** ``VARNAME=value`` — RST backtick-wrapped env-var assignment
  syntax, e.g. `` ``ADCP_VALIDATION_MODE=strict|warn|off`` ``.
- **Pattern B** SCREAMING_CASE_WITH_UNDERSCORE in a line that explicitly
  names an env-var context (``environ``, ``env var``, ``environment
  variable``, ``overrides``, ``flips``).

Exclusion: lines containing ``ignore``, ``deliberately``, or
``not honored`` are skipped — the var is explicitly documented as inactive.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

SRC_ROOT = Path(__file__).parent.parent / "src" / "adcp"

# SCREAMING_CASE with at least one underscore (e.g. ADCP_ENV, ADCP_HOST).
# Excludes short acronyms like A2A, HTTP, RFC that never contain an underscore.
_SCREAMING_WITH_UNDERSCORE = re.compile(r"\b([A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+)\b")

# Pattern A: ``VARNAME=anything`` inside RST double-backtick literals.
_ASSIGNMENT_PATTERN = re.compile(r"``([A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+=)")

# Context keywords that mark a line as discussing an env var (Pattern B).
_ENV_CONTEXT_RE = re.compile(
    r"\benviron\b|\benv var\b|\benvironment variable\b|\boverride[sd]?\b|\bflips?\b",
    re.IGNORECASE,
)

# Words that indicate a var is documented as deliberately not honored.
_IGNORE_CONTEXT_RE = re.compile(
    r"\bignored?\b|\bdeliberately\b|\bnot honored\b",
    re.IGNORECASE,
)

# All the ways the source code reads env vars:
#   os.environ.get("VAR"), os.getenv("VAR"), os.environ["VAR"]
_ENVIRON_READ_RE = re.compile(
    r"""(?:os\.environ\.get|os\.getenv)\(\s*['"]([A-Z][A-Z0-9_]+)['"]\s*"""
    r"""|os\.environ\[\s*['"]([A-Z][A-Z0-9_]+)['"]\s*\]"""
)


def _env_vars_in_use() -> frozenset[str]:
    """Return all env var names read by any file under src/adcp/."""
    found: set[str] = set()
    for path in SRC_ROOT.rglob("*.py"):
        src = path.read_text(encoding="utf-8")
        for m in _ENVIRON_READ_RE.finditer(src):
            var = m.group(1) or m.group(2)
            if var:
                found.add(var)
    return frozenset(found)


def _collect_docstrings(tree: ast.Module) -> list[tuple[str, int]]:
    """Return (docstring, lineno) for all public nodes, including dunders."""
    results: list[tuple[str, int]] = []
    for node in ast.walk(tree):
        name: str = getattr(node, "name", "") or ""
        # Include dunder methods (__init__, __str__, …) — their docstrings are
        # user-facing. Skip single-leading-underscore private names only.
        if name.startswith("_") and not (name.startswith("__") and name.endswith("__")):
            continue
        if not isinstance(
            node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
        ):
            continue
        doc = ast.get_docstring(node)
        if doc:
            lineno = getattr(node, "lineno", 0)
            results.append((doc, lineno))
    return results


def _env_vars_from_line(line: str) -> set[str]:
    """Extract env var names that are claimed active in this docstring line.

    Returns an empty set when the line explicitly marks vars as ignored.
    """
    if _IGNORE_CONTEXT_RE.search(line):
        return set()

    found: set[str] = set()

    # Pattern A: ``VARNAME=...`` assignment syntax.
    for m in _ASSIGNMENT_PATTERN.finditer(line):
        found.add(m.group(1).rstrip("="))

    # Pattern B: SCREAMING_CASE in an env-var context sentence.
    if _ENV_CONTEXT_RE.search(line):
        for m in _SCREAMING_WITH_UNDERSCORE.finditer(line):
            found.add(m.group(1))

    return found


def test_docstring_env_var_consistency() -> None:
    """Every env-var name documented as active in a public docstring must
    have a matching ``os.environ`` read somewhere in ``src/adcp/``.

    A violation means either:
    - the implementation was removed but the docs were not updated, or
    - the docs claim an override that was never implemented.

    Fix: add the missing ``os.environ.get(...)`` call, or add the phrase
    'deliberately ignored' to the docstring line.
    """
    in_use = _env_vars_in_use()
    violations: list[str] = []

    for filepath in sorted(SRC_ROOT.rglob("*.py")):
        try:
            source = filepath.read_text(encoding="utf-8")
            tree = ast.parse(source, filename=str(filepath))
        except SyntaxError:
            continue

        rel = str(filepath.relative_to(SRC_ROOT.parent.parent))
        for doc, lineno in _collect_docstrings(tree):
            for line in doc.splitlines():
                for var in _env_vars_from_line(line):
                    if var not in in_use:
                        violations.append(
                            f"{rel}:{lineno}: docstring references {var!r} "
                            f"but no os.environ read of {var!r} found in src/adcp/; "
                            f"line: {line.strip()!r}"
                        )

    assert not violations, (
        "Docstring/code env-var drift detected.\n"
        "Either add the missing os.environ read, or mark the var as "
        "'deliberately ignored' in the docstring.\n\n" + "\n".join(violations)
    )
