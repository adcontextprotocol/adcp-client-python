"""``AdcpError``'s recovery is the pinned classification, not a hardcoded default.

``enums/error-code.json``'s ``enumMetadata`` block classifies all 120 spec codes,
and its own ``$comment`` is addressed at SDKs: "SDKs MUST consume this block
instead of parsing 'Recovery: X' from enumDescriptions prose [...] the recovery
classification embedded in that prose is normative and MUST match the value
here."

This SDK already consumes it. :func:`adcp.server.helpers._build_standard_error_codes`
reads the block, fails closed on an unclassified code, and publishes
:data:`STANDARD_ERROR_CODES`; :func:`adcp.server.helpers.adcp_error` resolves a
caller's omitted ``recovery`` through it. So there is one owner and it is derived
rather than declared.

:class:`adcp.decisioning.types.AdcpError` — the exception adopters actually raise
— bypassed that owner and defaulted ``recovery="terminal"``. Two consequences,
and the tests below are one each:

* **A raise site that states no recovery published ``terminal`` for every code.**
  101 of the 120 spec codes are registered ``correctable``, so the default was
  wrong far more often than right. ``recovery`` exists so a buyer that does not
  recognise a code can still classify it, and ``terminal`` means "no autonomous
  recovery, operator intervention required" where ``correctable`` means "fix the
  request and resend" — so the default told buyers to stop retrying things they
  could have fixed.
* **An unregistered code keeps ``terminal``.** ``INTERNAL_ERROR`` is raised at 55
  sites and is in neither the spec enum nor the SDK extensions, so it has no
  registered classification. ``adcp_error`` already answers ``terminal`` for an
  unregistered code and ``AdcpError`` now agrees with it. This deliberately
  differs from :func:`adcp.signing.recovery_for`, which RAISES for an
  unclassified code: that taxonomy has no unregistered-code legacy to serve, and
  raising here would break 55 live raise sites rather than classify them.

An explicit ``recovery=`` still wins, because an adopter has to be able to
override a classification for their own deployment. What the third test forbids
is an override inside THIS package that merely restates or contradicts the
registered value — the 53 contradictions and 70 restatements this change removed.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from adcp.decisioning.types import AdcpError
from adcp.server.helpers import STANDARD_ERROR_CODES

_REPO_ROOT = Path(__file__).resolve().parent.parent
_SRC = _REPO_ROOT / "src" / "adcp"

#: ``retry_with_changes`` is a legacy alias ``AdcpError`` still accepts for
#: ``correctable``; the pin registers only the three canonical values.
_ALIAS = {"retry_with_changes": "correctable"}


def test_every_registered_code_derives_its_registered_recovery() -> None:
    """With no ``recovery`` stated, the code's registered classification is used."""
    assert len(STANDARD_ERROR_CODES) >= 100, (
        f"only {len(STANDARD_ERROR_CODES)} codes registered — the table has stopped "
        "resolving and this test grades nothing"
    )

    wrong = []
    for code, info in sorted(STANDARD_ERROR_CODES.items()):
        got = AdcpError(code).recovery
        if got != info["recovery"]:
            wrong.append(f"{code}: AdcpError gives {got}, the pin registers {info['recovery']}")

    assert wrong == [], "These codes do not derive their registered recovery:\n" + "\n".join(
        f"  {line}" for line in wrong
    )


def test_an_unregistered_code_is_terminal_and_agrees_with_adcp_error() -> None:
    """An unregistered code has no classification to derive, so it stays terminal."""
    assert "INTERNAL_ERROR" not in STANDARD_ERROR_CODES, (
        "INTERNAL_ERROR is now registered, so this test no longer covers the "
        "unregistered-code path — pick another unregistered code"
    )
    assert AdcpError("INTERNAL_ERROR").recovery == "terminal"
    assert AdcpError("X_VENDOR_UNKNOWN").recovery == "terminal"


@pytest.mark.parametrize("recovery", ["transient", "correctable", "terminal"])
def test_an_explicit_recovery_still_overrides_the_table(recovery: str) -> None:
    """An adopter's stated classification wins over the registered one."""
    # VALIDATION_ERROR is registered correctable, so for two of the three values
    # this asserts the override is honoured against a DIFFERING registered value.
    assert AdcpError("VALIDATION_ERROR", recovery=recovery).recovery == recovery


def _stated_recovery_sites() -> list[tuple[str, int, str, str]]:
    """Every in-package call stating both a literal code and a literal recovery.

    The code is the ``code=`` keyword OR the first positional argument: reading
    only the keyword finds nothing, because every call site passes it
    positionally.
    """
    found: list[tuple[str, int, str, str]] = []
    for path in sorted(_SRC.rglob("*.py")):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)
            if name not in {"AdcpError", "adcp_error"}:
                continue

            code_node: ast.AST | None = next(
                (kw.value for kw in node.keywords if kw.arg == "code"), None
            )
            if code_node is None and node.args:
                code_node = node.args[0]
            recovery_node = next((kw.value for kw in node.keywords if kw.arg == "recovery"), None)
            if code_node is None or recovery_node is None:
                continue
            if not (isinstance(code_node, ast.Constant) and isinstance(code_node.value, str)):
                continue
            if not (
                isinstance(recovery_node, ast.Constant) and isinstance(recovery_node.value, str)
            ):
                continue
            found.append(
                (
                    str(path.relative_to(_REPO_ROOT)),
                    node.lineno,
                    code_node.value,
                    recovery_node.value,
                )
            )
    return found


def _restatements(sites: list[tuple[str, int, str, str]]) -> list[str]:
    """Sites whose stated recovery EQUALS the registered one. The forbidden set."""
    found = []
    for rel, line, code, stated in sites:
        info = STANDARD_ERROR_CODES.get(code)
        if info is None:
            continue  # unregistered: the table owns nothing to restate
        if _ALIAS.get(stated, stated) == info["recovery"]:
            found.append(f"{rel}:{line}: {code} states {stated!r}, which is what the pin registers")
    return found


def test_no_site_in_this_package_restates_the_registered_recovery() -> None:
    """A stated recovery EQUAL to the registered one is a copy, and copies drift.

    Scoped to restatements on purpose, and the scope is the whole point. A
    CONTRADICTION is not automatically a defect here: this package raises
    several buyer-facing codes from boot-time and wiring validators of the
    ADOPTER's own configuration, where the registered classification is wrong
    for the audience. ``validate_webhook_signing_for_capabilities`` annotates
    its own raises "boot-time validators (terminal)";
    ``validate_capabilities._violation`` says "Same shape as the other
    server-boot fail-fast errors (terminal recovery + structured details)". A
    buyer cannot fix "DecisioningPlatform.accounts is None — subclasses MUST set
    an AccountStore" by resending, so ``terminal`` there is a judgement, not a
    mistake, and those errors do reach the wire — four of those validators run
    inside ``get_adcp_capabilities`` (salesagent-8qela.54).

    An earlier version of this test forbade EVERY stated recovery. That would
    have made the correct state fail the build, so the next person would have
    "fixed" the code to satisfy the guard and published ``correctable`` at ~45
    sites where a buyer can do nothing. A guard that cannot tell a justified
    override from a copy enforces the regression it was written to prevent.

    A restatement has no such defence: it says exactly what the table says, so
    it cannot be read as an override, and when the pin's classification moves it
    becomes a silent contradiction. The pin WILL move — this epic exists because
    of a version bump.
    """
    offenders = _restatements(_stated_recovery_sites())
    assert offenders == [], (
        f"{len(offenders)} site(s) restate a recovery the table already owns; delete the "
        "`recovery=` argument and let the constructor derive it:\n"
        + "\n".join(f"  {line}" for line in sorted(offenders))
    )


def test_the_restatement_guard_distinguishes_a_copy_from_a_justified_override() -> None:
    """The guard above must fire on a restatement and stay silent on an override.

    This is the guard's own fire probe, and it exists because the first version
    of that guard could not make this distinction — it flagged both, which is
    the failure mode that enforces a regression. Run against synthetic rows
    rather than the tree so it keeps testing the discrimination after every real
    restatement is gone.
    """
    correctable = next(c for c, i in STANDARD_ERROR_CODES.items() if i["recovery"] == "correctable")
    terminal = next(c for c, i in STANDARD_ERROR_CODES.items() if i["recovery"] == "terminal")

    # A copy of the registered value: must be caught.
    assert _restatements(
        [("f.py", 1, correctable, "correctable")]
    ), "the guard did not catch a restatement — it grades nothing"
    # The legacy alias is still a restatement of `correctable`.
    assert _restatements(
        [("f.py", 1, correctable, "retry_with_changes")]
    ), "the guard did not recognise retry_with_changes as a restatement of correctable"
    # A justified override contradicting the registry: must NOT be caught.
    assert _restatements([("f.py", 1, correctable, "terminal")]) == [], (
        "the guard flagged a CONTRADICTION — this is the version that would enforce the "
        "regression at the adopter-configuration validators"
    )
    assert (
        _restatements([("f.py", 1, terminal, "correctable")]) == []
    ), "the guard flagged a contradiction in the other direction"
    # An unregistered code has nothing to restate.
    assert _restatements([("f.py", 1, "X_VENDOR_UNKNOWN", "terminal")]) == []
