"""Spec-conformance test: AdcpError code names vs. the error-code.json enum.

Walks every AdcpError(...) constructor call in src/adcp/ using the AST and
asserts each literal code string is either:
  - in the canonical schemas/cache/enums/error-code.json enum, OR
  - prefixed with X_ (vendor-extension mechanism per the spec).

If this test fails on your PR:
  - If your code is a custom/platform-specific extension: prefix with X_
    (e.g. X_CHECKOUT_BLOCKED).
  - If your code belongs in the spec: open an issue on adcontextprotocol/adcp
    first; add it to the allowlist below once the spec PR merges.

Born from issue #375 / PR #393, where AGENT_SUSPENDED, AGENT_BLOCKED,
REQUEST_AUTH_UNRECOGNIZED_AGENT, and INVALID_BILLING_MODEL shipped as non-spec
codes for months before being caught in manual review.

Note: examples/ is excluded from this scan. The reference seller in
examples/v3_reference_seller/ may use non-spec codes that pre-date this
conformance gate; fixing them is tracked separately.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

_SCHEMA_PATH = (
    Path(__file__).parent.parent / "schemas" / "cache" / "enums" / "error-code.json"
)
_SRC_PATH = Path(__file__).parent.parent / "src" / "adcp"

# Codes not yet in the spec enum but named in the spec prose as forthcoming.
# Each entry MUST have a comment citing the spec reference so it can be removed
# once the spec formalizes the split.
#
# AUTH_INVALID: the AUTH_REQUIRED enumDescription explicitly states "A future
# minor release splits this code into AUTH_MISSING (correctable) and AUTH_INVALID
# (terminal)". Keep this entry until that split lands in the schema.
_PROVISIONAL_FUTURE_SPEC_CODES: frozenset[str] = frozenset({"AUTH_INVALID"})


def _collect_adcp_error_codes(
    src_path: Path,
) -> tuple[list[tuple[str, str, int]], int]:
    """AST-walk src_path for AdcpError() calls; return (literals, dynamic_count).

    literals: list of (code_value, repo-relative path, lineno) for calls where
              the code argument is a string constant.
    dynamic_count: number of AdcpError() calls where the code is not a literal
                   (e.g. a variable or f-string). Should stay zero — any
                   non-zero value means new dynamic code construction was added,
                   which bypasses this conformance gate.
    """
    literals: list[tuple[str, str, int]] = []
    dynamic_count = 0
    repo_root = src_path.parent.parent

    for py_file in src_path.rglob("*.py"):
        # Skip auto-generated code — do not edit generated_poc/ files.
        if "generated_poc" in py_file.parts:
            continue
        try:
            source = py_file.read_text(encoding="utf-8")
            tree = ast.parse(source, filename=str(py_file))
        except SyntaxError:
            continue

        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if not (isinstance(func, ast.Name) and func.id == "AdcpError"):
                continue

            # Locate the code argument: first positional or keyword 'code'.
            code_node: ast.expr | None = None
            if node.args:
                code_node = node.args[0]
            else:
                for kw in node.keywords:
                    if kw.arg == "code":
                        code_node = kw.value
                        break

            if code_node is None:
                continue

            rel = str(py_file.relative_to(repo_root))
            if isinstance(code_node, ast.Constant) and isinstance(
                code_node.value, str
            ):
                literals.append((code_node.value, rel, node.lineno))
            else:
                dynamic_count += 1

    return literals, dynamic_count


@pytest.fixture(scope="module")
def spec_codes() -> frozenset[str]:
    if not _SCHEMA_PATH.exists():
        pytest.skip(
            "schemas/cache/enums/error-code.json not found — run from repo checkout"
        )
    data = json.loads(_SCHEMA_PATH.read_text(encoding="utf-8"))
    return frozenset(data["enum"])


def test_no_dynamic_adcp_error_codes() -> None:
    """AdcpError code args must always be string literals so the AST scan is complete."""
    _, dynamic_count = _collect_adcp_error_codes(_SRC_PATH)
    assert dynamic_count == 0, (
        f"Found {dynamic_count} AdcpError() call(s) where the code argument is "
        "not a string literal (variable, f-string, or expression). "
        "Dynamic codes bypass this conformance gate — use a literal string instead."
    )


def test_all_adcp_error_codes_are_spec_or_vendor_prefixed(
    spec_codes: frozenset[str],
) -> None:
    """Every AdcpError code in src/adcp/ must be in the spec enum or use X_ prefix."""
    literals, _ = _collect_adcp_error_codes(_SRC_PATH)

    violations = [
        (code, path, lineno)
        for code, path, lineno in literals
        if not (
            code in spec_codes
            or code.startswith("X_")
            or code in _PROVISIONAL_FUTURE_SPEC_CODES
        )
    ]

    assert not violations, (
        "Non-spec AdcpError codes found. Each must be in "
        "schemas/cache/enums/error-code.json, use the X_ vendor prefix, "
        "or be added to _PROVISIONAL_FUTURE_SPEC_CODES with a spec citation.\n"
        + "\n".join(
            f"  {code!r} at {path}:{lineno}"
            f" — add X_ prefix or open a spec issue"
            for code, path, lineno in violations
        )
    )
