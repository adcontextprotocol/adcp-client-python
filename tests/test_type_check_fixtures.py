"""The adopter type-check fixtures are graded by the suite, not only by a Makefile.

WHY THIS EXISTS. ``tests/type_checks/`` is the adopter-pattern fixture set: it
encodes what a consumer of the published types is entitled to assume, and it is
the only thing in the tree that notices a public type-contract change. But
nothing ran it. ``make typecheck`` is ``mypy src/`` only; ``make typecheck-all``
does include the fixtures but is not what anyone runs while iterating; and the
fixtures were in no pytest suite at all, so no suite run could see them.

The consequence, measured: c045fe617 made ``Product.format_options`` and
``SyncCreativesRequest.creatives`` optional to match the pin, left
``canonical_override_field_types.py`` asserting the old required shape, and
shipped 4 ``mypy --strict`` errors. It merged, was reported verified, and sat in
the integrated tree THROUGH a graded baseline run — invisible, because that run
is pytest and these are mypy. The author ran ``mypy src/`` and not
``mypy --strict tests/type_checks/``: the right instrument, correctly used,
pointed at the wrong target (salesagent-8qela.87).

So the rule — a change to a public field's type or requiredness must run the
type-check fixtures — is enforced here instead of documented somewhere. The next
person to change a requiredness does not need to know it.

THE PRECEDENT is ``tests/test_canonical_removed_fields.py``, which already spawns
mypy AND pyright on snippets and asserts both refuse a removed keyword. A pytest
test that runs a type checker is established practice in this tree; this one
simply points it at the fixture directory.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
_FIXTURE_DIR = _REPO_ROOT / "tests" / "type_checks"

#: THE CEILING IS GONE, AND THERE IS NOTHING TO RAISE. It used to read 1, for
#: ``creative_asset_binding.py:82`` asserting the readback manifest's ``format_kind``
#: is ``str | None`` while ``_DeliveryCreativeManifest`` was bound to the manifest
#: root's canonical-format ARM, where that field is required ``str``.
#:
#: The note that stood here said clearing it "makes it validate as the root's UNION",
#: and that was wrong on the mechanism: ``BranchDispatchMixin`` hands the branches'
#: union only to the class that DECLARES ``_branch_models``, so a refinement declared
#: in ``adcp.types.canonical_creative`` gets its own plain shape either way. The real
#: cost was narrower — a root-bound readback instance is an instance of neither arm,
#: so revalidating it THROUGH the generated root refuses it as ``model_type`` — and it
#: was a cost worth paying, because arm-binding refused ``oneOf[0]`` documents the pin
#: permits on a delivery row. Both are now settled in the readback suite, so the
#: residual is fixed rather than tolerated and the count is zero.
#:
#: Do not reintroduce a ceiling. An error in these fixtures is a public type-contract
#: change: fix the type or update the fixture and say why in the commit.
_ERROR_LINE = re.compile(r"^(?P<path>[^:]+):(?P<line>\d+): error: (?P<message>.*)$")


def _fixtures() -> list[Path]:
    """Every fixture module, derived from the directory rather than listed.

    A hand-list would go stale exactly the way the gap this file closes did.
    """
    return sorted(p for p in _FIXTURE_DIR.glob("*.py") if p.name != "__init__.py")


@pytest.fixture(scope="module")
def strict_errors() -> list[tuple[str, int, str]]:
    """``mypy --strict`` over the fixture directory, as ``(file, line, message)``.

    Module-scoped: one mypy process for the whole file. Spawned the way
    ``test_canonical_removed_fields`` spawns its checkers, including ``MYPYPATH``,
    so the two tests agree about what they are grading.
    """
    result = subprocess.run(
        [sys.executable, "-m", "mypy", "--strict", str(_FIXTURE_DIR)],
        cwd=_REPO_ROOT,
        env=dict(os.environ, MYPYPATH=str(_REPO_ROOT / "src")),
        capture_output=True,
        text=True,
        timeout=600,
    )
    found = []
    for raw in result.stdout.splitlines():
        match = _ERROR_LINE.match(raw)
        if match:
            found.append((Path(match["path"]).name, int(match["line"]), match["message"]))
    assert found or "Success" in result.stdout, (
        "mypy produced neither an error line nor a success banner, so this test is "
        f"grading nothing. stdout:\n{result.stdout[-2000:]}\nstderr:\n{result.stderr[-2000:]}"
    )
    return found


def test_the_fixture_population_is_derived_and_not_empty() -> None:
    """The directory resolves, so the test below is checking something."""
    names = [p.name for p in _fixtures()]
    assert len(names) >= 40, (
        f"only {len(names)} fixture modules found under {_FIXTURE_DIR}, expected at "
        "least 40 — the directory has moved and this file no longer grades the "
        "adopter surface"
    )
    assert "canonical_override_field_types.py" in names, (
        "canonical_override_field_types.py is the module whose staleness this file "
        f"exists to catch and it is absent from the derived population: {names}"
    )


def test_the_adopter_fixtures_typecheck_under_strict(
    strict_errors: list[tuple[str, int, str]],
) -> None:
    """No NEW ``mypy --strict`` error in the adopter fixtures.

    This is the assertion c045fe617 needed and did not have. A change to a public
    field's type or requiredness that leaves a fixture asserting the old shape
    fails here, in the suite, rather than in a Makefile target nobody invoked.

    ANY error, in any fixture. There is no ceiling and no permitted residual: the
    one that used to be here (``creative_asset_binding.py:82``) is fixed, and the
    CI step this mirrors — ``mypy --strict tests/type_checks/ ...`` on the 3.12
    leg — tolerates nothing either, so a ceiling here could only hide something
    that reddens there.
    """
    assert strict_errors == [], (
        "mypy --strict reports errors in the adopter type-check fixtures:\n"
        + "\n".join(f"  {name}:{line}: {message}" for name, line, message in strict_errors)
        + "\n\nThese fixtures encode what a consumer of the published types may "
        "assume. If a public type changed on purpose, update the fixture AND say in "
        "the commit why the assertion it carried is now stale. If it changed by "
        "accident, this is the regression."
    )
