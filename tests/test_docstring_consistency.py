"""Ensure env-var names in public docstrings are backed by actual
``os.environ`` reads in the source tree.

Motivated by the drift in ``client.py:381-388``: the docstring claimed
``PYTHON_ENV`` / ``ENV`` / ``ENVIRONMENT`` were honored; only ``ADCP_ENV``
was. This test prevents that class of documentation drift from re-entering
silently.

Detection rules (applied per docstring line, with a ±1-line window for
Pattern B context keywords):

- **Pattern A** ``VARNAME=value`` — RST double-backtick env-var assignment
  syntax, e.g. `` ``ADCP_VALIDATION_MODE=strict|warn|off`` ``.
- **Pattern B** SCREAMING_CASE_WITH_UNDERSCORE inside a double-backtick span
  on a line whose ±1 neighborhood contains an env-var context keyword
  (``environ``, ``env var``, ``environment variable``, ``flips``). Restricted
  to backtick-wrapped tokens to prevent false positives from protocol
  constants or OOP-override prose (e.g. "this method overrides HTTP_TIMEOUT").
  "overrides" is intentionally excluded as a standalone keyword for the same
  reason; legitimate override docs use ``VARNAME=value`` or "VARNAME env var".

Exclusion: lines containing ``deliberately`` or ``not honored`` are skipped —
the var is explicitly documented as inactive on that line. Bare "ignored" is
not an exclusion keyword to avoid suppressing real checks when a docstring
says "X is ignored when Y is set explicitly."

Known limitations:
- Single-token env vars without underscores (e.g. ``PORT``) are invisible to
  both patterns; the regex requires at least one underscore to distinguish env
  vars from acronyms and other short tokens.
- ``_env_vars_in_use()`` scans raw source text, so ``os.environ`` calls that
  appear inside docstring examples are included in the "in use" set. A future
  docstring embedding a fictional ``os.environ["FAKE_VAR"]`` call would
  suppress detection of that name. This is an accepted trade-off for
  simplicity.
- ``ast.walk`` collects public methods defined inside private classes. This is
  a known over-approximation; fixing it requires tracking parent scope.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

SRC_ROOT = Path(__file__).parent.parent / "src" / "adcp"

# Pattern A: ``VARNAME=anything`` inside RST double-backtick literals.
# Captures the var name without the trailing ``=``.
_ASSIGNMENT_PATTERN = re.compile(r"``([A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+)=")

# Pattern B — step 1: extract double-backtick span contents.
_BACKTICK_SPAN = re.compile(r"``([^`]+)``")

# Pattern B — step 2: find SCREAMING_CASE_WITH_UNDERSCORE inside a span.
# Two-step avoids greedy-match bugs where a single regex would capture a
# suffix like ``P_ENV`` from ``ADCP_ENV``.
_SCREAMING_WITH_UNDERSCORE = re.compile(r"\b([A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+)\b")

# Context keywords for Pattern B. Present in the line's ±1 neighborhood, they
# indicate the backtick-wrapped token is an env var. "overrides" is excluded
# (see module docstring) to prevent OOP-override false positives.
_ENV_CONTEXT_RE = re.compile(
    r"\benviron\b|\benv var\b|\benvironment variable\b|\bflips?\b",
    re.IGNORECASE,
)

# Words that, on the SAME line as a token, explicitly mark it as inactive.
# Bare "ignored" is excluded: "X is ignored when Y is set" would suppress
# legitimate checks if we matched it.
_IGNORE_CONTEXT_RE = re.compile(
    r"\bdeliberately\b|\bnot honored\b",
    re.IGNORECASE,
)

# All the ways the source code reads env vars:
#   os.environ.get("VAR"), os.getenv("VAR"), os.environ["VAR"]
_ENVIRON_READ_RE = re.compile(
    r"""(?:os\.environ\.get|os\.getenv)\(\s*['"]([A-Z][A-Z0-9_]+)['"]\s*"""
    r"""|os\.environ\[\s*['"]([A-Z][A-Z0-9_]+)['"]\s*\]"""
)


def _env_vars_in_use() -> frozenset[str]:
    """Return all env var names read by any file under src/adcp/.

    Scans raw source text; see module docstring for the known limitation about
    os.environ calls embedded in docstring examples.
    """
    found: set[str] = set()
    for path in SRC_ROOT.rglob("*.py"):
        src = path.read_text(encoding="utf-8")
        for m in _ENVIRON_READ_RE.finditer(src):
            var = m.group(1) or m.group(2)
            if var:
                found.add(var)
    return frozenset(found)


def _collect_docstrings(tree: ast.Module) -> list[tuple[str, int]]:
    """Return (docstring, lineno) for all public nodes, including dunders.

    See module docstring for the known limitation about public methods inside
    private classes.
    """
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
            # ast.Module has no lineno; use 1 since module docstrings are at
            # the top of the file.
            lineno = getattr(node, "lineno", 1)
            results.append((doc, lineno))
    return results


def _env_vars_from_docstring(doc: str) -> set[str]:
    """Extract env var names documented as active in this docstring.

    Uses a ±1-line window for Pattern B so that a context keyword on the line
    immediately after the env-var reference is still detected (e.g.
    ``ADCP_ENV`` on one line, ``flips …`` on the next).
    """
    lines = doc.splitlines()
    found: set[str] = set()

    for i, line in enumerate(lines):
        # Pattern A: ``VARNAME=...`` assignment syntax — line-local check.
        if not _IGNORE_CONTEXT_RE.search(line):
            for m in _ASSIGNMENT_PATTERN.finditer(line):
                found.add(m.group(1))

        # Pattern B: backtick-wrapped SCREAMING_CASE in env-var context.
        if _IGNORE_CONTEXT_RE.search(line):
            continue  # this line explicitly marks var(s) as inactive

        # Collect SCREAMING_CASE tokens from inside each ``...`` span.
        screaming_vars = [
            vm.group(1)
            for sm in _BACKTICK_SPAN.finditer(line)
            for vm in _SCREAMING_WITH_UNDERSCORE.finditer(sm.group(1))
        ]
        if not screaming_vars:
            continue

        # Context keyword may be on this line or an immediate neighbor (±1).
        window = [line]
        if i > 0:
            window.append(lines[i - 1])
        if i < len(lines) - 1:
            window.append(lines[i + 1])
        if any(_ENV_CONTEXT_RE.search(w) for w in window):
            found.update(screaming_vars)

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
            # Syntax errors are caught by dedicated CI checks; skip here.
            continue  # pragma: no cover

        rel = str(filepath.relative_to(SRC_ROOT.parent.parent))
        for doc, lineno in _collect_docstrings(tree):
            for var in _env_vars_from_docstring(doc):
                if var not in in_use:
                    violations.append(
                        f"{rel}:{lineno}: docstring references {var!r} "
                        f"but no os.environ read of {var!r} found in src/adcp/"
                    )

    assert not violations, (
        "Docstring/code env-var drift detected.\n"
        "Either add the missing os.environ read, or mark the var as "
        "'deliberately ignored' in the docstring.\n\n" + "\n".join(violations)
    )
