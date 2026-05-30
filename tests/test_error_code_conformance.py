"""AdCP error-code spec conformance — static AST walker.

Scans all ``.py`` files under ``src/adcp/`` for ``AdcpError(...)`` raise
sites and asserts every string-literal first-positional code is either:

* in the canonical AdCP error-code enum (bundled at
  :file:`src/adcp/types/generated_poc/enums/error_code.py`, generated
  from :file:`schemas/cache/enums/error-code.json`);
* prefixed with ``X_`` per the AdCP vendor-extension convention; or
* explicitly listed in :data:`KNOWN_NON_SPEC_CODES` below — a small,
  documented allowlist for codes the SDK uses intentionally that are
  not (yet) in the enum.

Background — issue #375 / PR #393: four codes shipped for months as
non-spec (``AGENT_SUSPENDED`` / ``AGENT_BLOCKED`` /
``REQUEST_AUTH_UNRECOGNIZED_AGENT`` / ``INVALID_BILLING_MODEL``) before
being migrated to spec-conformant ``PERMISSION_DENIED`` and
``BILLING_NOT_PERMITTED_FOR_AGENT``. This test is the load-bearing CI
signal preventing that drift from recurring.

Why AST and not regex: regex over multi-line raise expressions
(``AdcpError(\n    "FOO",\n    message=...,``) is fragile. ``ast`` walks
the parsed module and picks out exactly the first positional arg of
``Call`` nodes whose ``func`` is named ``AdcpError``.

Limitations (deliberate, documented):

* Only string-literal codes are inspected. Variable / attribute /
  computed codes are skipped — those are rare, intentional in the
  framework's catch-and-re-raise paths, and need separate manual review.
  A count of skipped raise sites is reported alongside failures.
* Only the symbol name ``AdcpError`` is walked (the structured
  server-side error from :mod:`adcp.decisioning.types`). The
  unrelated client-side ``ADCPError`` (all-caps, from
  :mod:`adcp.exceptions`) takes ``(message, ...)`` not ``(code, ...)``
  and is excluded by name.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path

import pytest

from adcp.types.generated_poc.enums.error_code import ErrorCode

# ---------------------------------------------------------------------------
# Allowlist — codes used intentionally by the SDK that are not in the
# canonical enum. Keep this list short and documented; every entry is a
# spec-drift point that should ideally migrate upstream.
# ---------------------------------------------------------------------------
KNOWN_NON_SPEC_CODES: dict[str, str] = {
    # TODO: track upstream addition to error-code.json enum.
    # Universal "framework caught an unhandled exception" wrap. Used by
    # the dispatch layer to project arbitrary Python exceptions to a
    # safe wire shape (without leaking stack traces). The spec
    # description on error-code.json explicitly notes that sellers MAY
    # return codes outside the enum for platform-specific errors;
    # INTERNAL_ERROR is the SDK's canonical fallback.
    "INTERNAL_ERROR": (
        "Universal exception wrap used by adcp.decisioning.dispatch. "
        "Spec allows codes outside the enum; this is the SDK's fallback."
    ),
    # TODO: track upstream 3.1 split of AUTH_REQUIRED → AUTH_MISSING + AUTH_INVALID.
    # 3.1 will split AUTH_REQUIRED into AUTH_MISSING + AUTH_INVALID per
    # the canonical enumDescription on AUTH_REQUIRED. The SDK uses
    # AUTH_INVALID at the FromAuthAccounts gate where the principal is
    # missing/empty after auth verification — distinct from "no
    # credentials presented" (AUTH_REQUIRED).
    "AUTH_INVALID": (
        "Pre-canonical 3.1 split of AUTH_REQUIRED. Documented in the "
        "AUTH_REQUIRED enumDescription as a future spec change."
    ),
    # TODO: track upstream addition to error-code.json enum.
    # Server-side adopter-misconfiguration signal raised at framework
    # seams where the platform's declared shape can't service the
    # request — e.g., DecisioningPlatform.upstream_for() with no
    # ``upstream_url`` and a non-mock account, or a mock-mode account
    # whose ``metadata['mock_upstream_url']`` is missing/empty.
    # Distinct from INVALID_REQUEST (buyer's payload bad) and
    # SERVICE_UNAVAILABLE (transient upstream failure); buyers can't
    # fix this — only the seller's deployment can. Surfaces with
    # recovery=terminal so buyers don't retry.
    "CONFIGURATION_ERROR": (
        "Adopter-misconfiguration signal raised by "
        "DecisioningPlatform.upstream_for. Distinct from INVALID_REQUEST "
        "(buyer-fixable) and SERVICE_UNAVAILABLE (transient)."
    ),
    # TODO: track upstream addition to error-code.json enum (adcp issue #4043).
    # Per docs/proposals/proposal-manager-v15-design.md § D7 / Resolutions §3:
    # PROPOSAL_EXPIRED and PROPOSAL_NOT_COMMITTED already shipped in 3.0;
    # PROPOSAL_NOT_FOUND lands in 3.1 once adcp#4043 closes. The proposal
    # lifecycle framework code (proposal_lifecycle.enforce_proposal_expiry)
    # raises this when a buyer-supplied proposal_id has no record AND when
    # cross-tenant probes are squashed (same-error-as-missing posture
    # mirrors TaskRegistry.get).
    "PROPOSAL_NOT_FOUND": (
        "Pre-canonical 3.1 code raised by proposal_lifecycle when a "
        "create_media_buy(proposal_id=...) call references an unknown "
        "or cross-tenant proposal_id. Spec issue: "
        "https://github.com/adcontextprotocol/adcp/issues/4043."
    ),
    # TODO: drop when ADCP_VERSION >= 3.1.
    # Present in the spec's source-of-truth (`static/schemas/source/enums/
    # error-code.json` on adcontextprotocol/adcp `main`) but not in any
    # tagged 3.0.x dist bundle. The 3.0.x bundle is frozen at 45 codes;
    # the source has 62. The framework's `validate_billing_for_agent`
    # raises this code with `error.details` shaped per the
    # `error-details/billing-not-permitted-for-agent.json` schema —
    # collapsing to PERMISSION_DENIED would erase the `rejected_billing`
    # / `suggested_billing` discriminator the spec defines for this gate.
    "BILLING_NOT_PERMITTED_FOR_AGENT": (
        "Per-agent billing gate raised by validate_billing_for_agent. "
        "In source/main (3.1), absent from 3.0.x dist bundles."
    ),
    # TODO: drop when ADCP_VERSION >= 3.1.
    # AdCP 3.1 (PR adcontextprotocol/adcp#3906) consolidates the 3.0.5
    # `PERMISSION_DENIED + details.status` placeholder into dedicated
    # codes for per-agent commercial-status rejections. The code itself
    # is the discriminator (no `details` payload), mirroring
    # `BILLING_NOT_PERMITTED_FOR_AGENT`. Both carry `recovery="terminal"`
    # at the wire level — the placeholder shape inherited
    # `PERMISSION_DENIED`'s `correctable`, which contradicted the
    # no-retry MUST. Raised by `_resolve_buyer_agent` in
    # `adcp.decisioning.handler` when `BuyerAgent.status` is
    # "suspended" / "blocked" respectively.
    "AGENT_SUSPENDED": (
        "Per-agent suspended status raised by _resolve_buyer_agent. "
        "In source/main (3.1.0-beta.1+), absent from 3.0.x dist bundles."
    ),
    "AGENT_BLOCKED": (
        "Per-agent blocked status raised by _resolve_buyer_agent. "
        "In source/main (3.1.0-beta.1+), absent from 3.0.x dist bundles."
    ),
}

CANONICAL_CODES: frozenset[str] = frozenset(member.value for member in ErrorCode)
SRC_ROOT = Path(__file__).resolve().parent.parent / "src" / "adcp"


@dataclass(frozen=True)
class RaiseSite:
    """A single ``AdcpError(...)`` call site found by the walker."""

    file: Path
    lineno: int
    code: str | None  # None means "non-literal first arg" (skipped)


def _extract_literal_code(call: ast.Call) -> str | None:
    """Return the first positional arg if it is a ``str`` literal, else None.

    Handles:
    * ``AdcpError("CODE", message=...)`` — positional literal
    * ``AdcpError(code="CODE", message=...)`` — keyword literal
    """
    if call.args:
        first = call.args[0]
        if isinstance(first, ast.Constant) and isinstance(first.value, str):
            return first.value
        return None
    for kw in call.keywords:
        if kw.arg == "code":
            if isinstance(kw.value, ast.Constant) and isinstance(kw.value.value, str):
                return kw.value.value
            return None
    return None


def _is_adcp_error_call(call: ast.Call) -> bool:
    """Match ``AdcpError(...)`` and ``module.AdcpError(...)`` calls.

    Excludes the unrelated all-caps ``ADCPError`` (client-side
    connection-error class with a different signature).
    """
    func = call.func
    if isinstance(func, ast.Name):
        return func.id == "AdcpError"
    if isinstance(func, ast.Attribute):
        return func.attr == "AdcpError"
    return False


def _walk_file(path: Path) -> list[RaiseSite]:
    """Parse ``path`` and return every ``AdcpError(...)`` call site."""
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(path))
    sites: list[RaiseSite] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and _is_adcp_error_call(node):
            sites.append(
                RaiseSite(
                    file=path,
                    lineno=node.lineno,
                    code=_extract_literal_code(node),
                )
            )
    return sites


def _collect_raise_sites() -> list[RaiseSite]:
    """Walk every ``.py`` file under ``src/adcp/`` for AdcpError calls."""
    sites: list[RaiseSite] = []
    for path in sorted(SRC_ROOT.rglob("*.py")):
        sites.extend(_walk_file(path))
    return sites


def _is_acceptable_code(code: str) -> bool:
    """Code is in the canonical enum, has X_ vendor prefix, or is allowlisted."""
    if code in CANONICAL_CODES:
        return True
    if code.startswith("X_"):
        return True
    if code in KNOWN_NON_SPEC_CODES:
        return True
    return False


def test_canonical_enum_is_loaded() -> None:
    """Sanity-check: the bundled enum has the expected shape.

    Pins the assumption that the generated ``ErrorCode`` enum mirrors
    the dist-bundle schema for the pinned ``ADCP_VERSION``. If this
    drifts (e.g. the schema gains a code), this test surfaces the drift
    before the conformance walker silently starts accepting it as
    canonical.

    Source of truth is the **dist bundle** for the pinned
    ``ADCP_VERSION`` (``schemas/cache/enums/error-code.json`` after a
    clean ``scripts/sync_schemas.py`` run). Never source from the spec
    repo's ``static/schemas/source/`` directly — that tracks the next
    major-version's WIP and will leak forward state into a maintenance-
    line SDK. PR #429 made that mistake (pinned this count to 60 from
    source/main while ``ADCP_VERSION=3.0.5`` only carried 45 in dist),
    and the regression survived months because no CI step re-ran sync
    and codegen against the pinned count.
    """
    assert "PERMISSION_DENIED" in CANONICAL_CODES
    assert "ACCOUNT_SUSPENDED" in CANONICAL_CODES
    assert "PRIVATE_FIELD_IN_PUBLIC_PLACEMENT" in CANONICAL_CODES
    assert "FORMAT_OPTION_UNRESOLVED" in CANONICAL_CODES
    assert "FORMAT_NOT_SUPPORTED" in CANONICAL_CODES
    # Spot-check a non-spec code that historically got misnamed and is
    # still not in the canonical enum:
    assert "INVALID_BILLING_MODEL" not in CANONICAL_CODES
    assert "REQUEST_AUTH_UNRECOGNIZED_AGENT" not in CANONICAL_CODES
    assert "FORMAT_CAPABILITY_UNRESOLVED" not in CANONICAL_CODES
    # If this assertion fails, the bundled error-code.json was resynced;
    # update both the count AND audit allowlist entries that may now be
    # in the canonical enum.
    assert len(CANONICAL_CODES) == 82, f"Expected 82 spec error codes, got {len(CANONICAL_CODES)}"


def test_adcp_error_codes_are_spec_conformant() -> None:
    """Every literal AdcpError(code, ...) is in the spec enum, X_-prefixed, or allowlisted."""
    sites = _collect_raise_sites()
    assert sites, (
        f"AdcpError raise-site walker found zero call sites under {SRC_ROOT}; "
        "this suggests the walker is broken (the codebase is known to raise "
        "AdcpError in adcp.decisioning.*)."
    )

    violations: list[RaiseSite] = []
    for site in sites:
        if site.code is None:
            continue  # Non-literal — skipped by design (see module docstring).
        if not _is_acceptable_code(site.code):
            violations.append(site)

    if violations:
        lines = [
            f"  {site.file.relative_to(SRC_ROOT.parent.parent)}:{site.lineno}  →  {site.code!r}"
            for site in violations
        ]
        msg = (
            f"Found {len(violations)} non-spec AdcpError code(s):\n"
            + "\n".join(lines)
            + "\n\nEvery AdcpError(code, ...) must use a code from the canonical "
            "AdCP enum (schemas/cache/enums/error-code.json), the X_ "
            "vendor-extension prefix, or be added to KNOWN_NON_SPEC_CODES "
            "in tests/test_error_code_conformance.py with a documented reason."
        )
        pytest.fail(msg)


def test_allowlist_entries_are_actually_used() -> None:
    """Every KNOWN_NON_SPEC_CODES entry must appear in at least one raise site.

    Prevents the allowlist from accumulating dead entries — once a
    non-spec code is migrated to a spec code (or removed), its allowlist
    entry should also be removed. Without this check the allowlist
    becomes a graveyard of historical codes that silently mask future
    drift.
    """
    sites = _collect_raise_sites()
    used_codes = {site.code for site in sites if site.code is not None}
    stale = [code for code in KNOWN_NON_SPEC_CODES if code not in used_codes]
    assert not stale, (
        f"KNOWN_NON_SPEC_CODES entries no longer used in src/adcp/: {stale}. "
        "Remove them — dead entries mask future drift."
    )
