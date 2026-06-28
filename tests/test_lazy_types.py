"""Guards for the lazy type surface.

``adcp`` and ``adcp.types`` are lazy (PEP 562 ``__getattr__``):

- ``import adcp`` does not build the generated Pydantic schema graph or import
  the client / server / a2a stack.
- ``import adcp.types`` does not build the schema graph either; the graph
  (``adcp.types._eager``) is realized on first access to a type symbol.
- ``from adcp import ADCPError`` (a non-schema symbol) stays light; importing
  a schema symbol (``Product``) triggers the build.
- The curated partial modules (``adcp.types.media_buy`` etc.) are lazy facades
  over ``adcp.types`` that never touch the internal generated layer.

These tests run import-cost assertions in fresh subprocesses so a prior import
elsewhere in the suite can't mask a regression.
"""

from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

import pytest

SRC_TYPES = Path(__file__).parent.parent / "src" / "adcp" / "types"
PARTIAL_MODULES = ("media_buy", "creative", "signals", "protocol", "buyer", "seller")


def _run(code: str) -> str:
    """Run ``code`` in a fresh interpreter; return stdout (asserts exit 0)."""
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, f"subprocess failed:\n{result.stderr}"
    return result.stdout.strip()


# --------------------------------------------------------------------------- #
# Laziness (fresh-interpreter import cost)
# --------------------------------------------------------------------------- #


def test_import_adcp_does_not_build_schema_graph() -> None:
    """``import adcp`` must not import the generated schema graph or a2a stack."""
    out = _run(
        "import sys, adcp;"
        "heavy = [m for m in ('adcp.types._eager','adcp.types._generated',"
        "'adcp.client','adcp.testing','adcp.server','adcp.decisioning') if m in sys.modules];"
        "a2a = [m for m in sys.modules if m.startswith('a2a')];"
        "print(repr(heavy)); print(len(a2a))"
    )
    heavy, a2a_count = out.splitlines()
    assert heavy == "[]", f"import adcp eagerly loaded heavy modules: {heavy}"
    assert a2a_count == "0", "import adcp eagerly loaded the a2a stack"


def test_import_adcp_does_not_load_importlib_metadata() -> None:
    """``import adcp`` must defer ``importlib.metadata`` (the version lookup).

    ``importlib.metadata`` pulls in a sizable stdlib subtree (~15ms); the
    version is resolved lazily on first ``adcp.__version__`` access instead.
    """
    out = _run("import sys, adcp; print('importlib.metadata' in sys.modules)")
    assert out == "False", "import adcp eagerly imported importlib.metadata"


def test_version_resolves_lazily() -> None:
    """``adcp.__version__`` resolves on demand and only then loads the metadata API."""
    out = _run(
        "import sys, adcp;"
        "before = 'importlib.metadata' in sys.modules;"
        "v = adcp.__version__;"
        "print(before); print(isinstance(v, str) and len(v) > 0);"
        "print('importlib.metadata' in sys.modules)"
    )
    before, is_str, after = out.splitlines()
    assert before == "False", "metadata loaded before __version__ was accessed"
    assert is_str == "True", "__version__ should be a non-empty string"
    assert after == "True", "accessing __version__ should resolve via importlib.metadata"


def test_version_not_clobbered_by_version_submodule() -> None:
    """``adcp.__version__`` stays a string even after importing ``adcp._version``.

    Guards against the version cache being named ``_version`` (which collides
    with the ``adcp._version`` submodule and would make ``__version__`` resolve
    to the module object).
    """
    out = _run(
        "import adcp, adcp._version;"  # importing the submodule sets adcp._version
        "v = adcp.__version__;"
        "print(type(v).__name__); print(v)"
    )
    type_name, value = out.splitlines()
    assert type_name == "str", f"adcp.__version__ resolved to {type_name}, not str"
    assert value and value != "MISSING"


# Modules that cross-import other ``adcp`` modules and are realistically
# imported on their own (before ``adcp`` or each other). The eager ``import
# adcp`` used to mask import-order cycles by loading everything in a fixed
# order; under the lazy facade each must import standalone. Guards the
# webhooks <-> webhook_sender cycle (and any sibling) from regressing.
_STANDALONE_FIRST_IMPORT = [
    "adcp.webhook_sender",
    "adcp.webhooks",
    "adcp.webhook_supervisor",
    "adcp.webhook_supervisor_pg",
    "adcp.webhook_receiver",
    "adcp.simple",
    "adcp.feed_mirror",
    "adcp.client",
    "adcp.registry",
    "adcp.adagents",
    "adcp.server",
]


@pytest.mark.parametrize("module", _STANDALONE_FIRST_IMPORT)
def test_submodule_imports_standalone(module: str) -> None:
    """Each cross-importing submodule must import first in a fresh interpreter.

    A circular import only manifests when the module is imported *before* the
    module it cycles with, so each runs in its own subprocess.
    """
    result = subprocess.run(
        [sys.executable, "-c", f"import {module}"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, f"`import {module}` failed standalone:\n{result.stderr}"


def test_import_adcp_types_does_not_build_schema_graph() -> None:
    """``import adcp.types`` must not realize the eager graph."""
    out = _run(
        "import sys, adcp.types;"
        "print('adcp.types._eager' in sys.modules);"
        "print('adcp.types._generated' in sys.modules)"
    )
    eager, generated = out.splitlines()
    assert eager == "False" and generated == "False", out


def test_importing_a_partial_module_is_lazy() -> None:
    """Importing a partial module is cheap; the graph builds on first name access."""
    out = _run(
        "import sys, adcp.types.media_buy as mb;"
        "before = 'adcp.types._eager' in sys.modules;"
        "x = mb.CreateMediaBuyRequest;"
        "after = 'adcp.types._eager' in sys.modules;"
        "print(before); print(after)"
    )
    before, after = out.splitlines()
    assert before == "False", "importing the partial module eagerly built the graph"
    assert after == "True", "accessing a name did not trigger the graph build"


def test_non_schema_symbol_stays_light() -> None:
    """``from adcp import ADCPError`` must not build the schema graph."""
    out = _run(
        "import sys; from adcp import ADCPError;"
        "print('adcp.types._generated' in sys.modules);"
        "print('adcp.client' in sys.modules)"
    )
    assert out.splitlines() == ["False", "False"], out


def test_schema_symbol_triggers_build() -> None:
    """Accessing a schema symbol realizes the graph — 'asking for schema symbols'."""
    out = _run(
        "import sys; from adcp import Product;" "print('adcp.types._generated' in sys.modules)"
    )
    assert out == "True"


# --------------------------------------------------------------------------- #
# Surface equivalence (lazy facade == eager realization)
# --------------------------------------------------------------------------- #


def test_lazy_types_surface_matches_eager() -> None:
    """Every ``adcp.types.__all__`` name resolves to the same object as ``_eager``.

    The lazy facade must be behaviourally identical to the eager realization:
    each advertised name resolves (no dead entries) and to the *same object*.
    """
    import adcp.types
    from adcp.types import _eager

    submodules = set(adcp.types._PARTIAL_MODULES) | {"generated", "aliases"}
    mismatches = []
    for name in adcp.types.__all__:
        if name in submodules:
            continue  # submodule re-exports, checked separately
        if getattr(adcp.types, name) is not getattr(_eager, name):
            mismatches.append(name)
    assert not mismatches, f"lazy/eager surface diverged for: {mismatches}"


def test_eager_only_extras_matches_eager_namespace() -> None:
    """``_EAGER_ONLY_EXTRAS`` must list exactly the importable helpers not in __all__.

    These are re-exported for back-compat (response guards + a few helpers) but
    kept out of the documented ``__all__``. The fast-fail in ``__getattr__``
    relies on this set being complete; if codegen adds/removes one, this fails
    loudly so the constant gets updated.
    """
    import types as _types

    import adcp.types
    from adcp.types import _eager

    actual = {
        name
        for name, value in vars(_eager).items()
        if not name.startswith("_")
        and name != "annotations"
        and not isinstance(value, _types.ModuleType)
        and name not in set(adcp.types.__all__)
    }
    assert adcp.types._EAGER_ONLY_EXTRAS == actual, (
        "adcp.types._EAGER_ONLY_EXTRAS drifted from _eager's namespace; "
        f"only in constant: {adcp.types._EAGER_ONLY_EXTRAS - actual}; "
        f"only in _eager: {actual - adcp.types._EAGER_ONLY_EXTRAS}"
    )


def test_unknown_attribute_fails_fast_without_building_graph() -> None:
    """A missing/typo'd name raises AttributeError without realizing the graph."""
    out = _run(
        "import sys, adcp.types;"
        "import pytest;"
        "exc=None;\n"
        "try:\n"
        "    adcp.types.NoSuchTypeXYZ\n"
        "except AttributeError as e:\n"
        "    exc=e\n"
        "print(exc is not None);"
        "print('adcp.types._eager' in sys.modules)"
    )
    raised, eager_loaded = out.splitlines()
    assert raised == "True", "expected AttributeError for unknown name"
    assert eager_loaded == "False", "unknown name should not build the eager graph"


def test_star_import_from_adcp_types_resolves_all() -> None:
    """``from adcp.types import *`` resolves every advertised name (no dead entries)."""
    import adcp.types

    ns: dict[str, object] = {}
    exec("from adcp.types import *", ns)  # noqa: S102
    missing = [n for n in adcp.types.__all__ if n not in ns]
    assert not missing, f"star-import did not resolve: {missing}"


def test_partial_module_names_all_resolve_via_adcp_types() -> None:
    """Each partial module exposes exactly its ``__all__``, sourced from adcp.types."""
    import importlib

    import adcp.types

    for mod_name in PARTIAL_MODULES:
        module = importlib.import_module(f"adcp.types.{mod_name}")
        assert module.__all__, f"{mod_name} has empty __all__"
        for name in module.__all__:
            assert getattr(module, name) is getattr(
                adcp.types, name
            ), f"adcp.types.{mod_name}.{name} is not the same object as adcp.types.{name}"
        assert sorted(dir(module)) == sorted(module.__all__)


def test_partial_modules_are_in_types_all() -> None:
    """The six partial modules are advertised in ``adcp.types.__all__``."""
    import adcp.types

    for mod_name in PARTIAL_MODULES:
        assert mod_name in adcp.types.__all__


@pytest.mark.parametrize("mod_name", PARTIAL_MODULES)
def test_partial_modules_never_import_generated_layer(mod_name: str) -> None:
    """Partial modules must not import the internal generated layer (item 4)."""
    tree = ast.parse((SRC_TYPES / f"{mod_name}.py").read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            assert not node.module.startswith(
                ("adcp.types._generated", "adcp.types.generated_poc")
            ), f"{mod_name}.py imports the internal generated layer: {node.module}"


@pytest.mark.parametrize("mod_name", PARTIAL_MODULES)
def test_partial_modules_avoid_numbered_codegen_names(mod_name: str) -> None:
    """Curated partials must not expose codegen-numbered names (e.g. ``*Response1``).

    These trailing-digit names are datamodel-code-generator artifacts that churn
    on schema regen; where a name like ``CreateMediaBuyResponse1`` is exposed, the
    curated surface should use its semantic alias (``CreateMediaBuySuccessResponse``)
    instead. They remain available on the full ``adcp.types`` surface.
    """
    import importlib
    import re

    module = importlib.import_module(f"adcp.types.{mod_name}")
    numbered = [n for n in module.__all__ if re.search(r"[A-Za-z]\d+$", n)]
    assert not numbered, (
        f"adcp.types.{mod_name} exposes codegen-numbered names {numbered}; "
        "use the semantic alias instead (these churn on schema regen)."
    )


# --------------------------------------------------------------------------- #
# Behavior preserved by the lazy facade
# --------------------------------------------------------------------------- #


def test_dir_includes_all() -> None:
    """``dir()`` covers ``__all__`` for tooling/autocomplete."""
    import adcp
    import adcp.types

    assert set(adcp.__all__) <= set(dir(adcp))
    assert set(adcp.types.__all__) <= set(dir(adcp.types))


def test_star_import_from_adcp_resolves_all() -> None:
    """``from adcp import *`` resolves every advertised name."""
    import adcp

    ns: dict[str, object] = {}
    exec("from adcp import *", ns)  # noqa: S102
    missing = [n for n in adcp.__all__ if n not in ns]
    assert not missing, f"star-import did not resolve: {missing}"


def test_geopostalarea_deprecation_warns_once_then_caches() -> None:
    """GeoPostalArea resolves to PostalArea5 and warns once (then is cached).

    The lazy facade caches the resolved alias into the module namespace so the
    DeprecationWarning fires on first access only — subsequent accesses hit the
    module dict directly and skip ``__getattr__``.
    """
    import warnings

    import adcp.types

    # Start uncached so this test observes the first-access warning regardless
    # of what ran before it.
    adcp.types.__dict__.pop("GeoPostalArea", None)

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        first = adcp.types.GeoPostalArea
        second = adcp.types.GeoPostalArea
        third = adcp.types.GeoPostalArea

    assert first.__name__ == "PostalArea5"
    assert second is first and third is first
    deprecations = [w for w in caught if issubclass(w.category, DeprecationWarning)]
    assert len(deprecations) == 1, f"expected one warning, got {len(deprecations)}"
    assert "GeoPostalArea is deprecated" in str(deprecations[0].message)
    assert "GeoPostalArea" in vars(adcp.types), "alias should be cached after first access"


def test_removed_v4_names_take_precedence_over_lazy() -> None:
    """Removed-in-4.0 names raise ImportError even though __getattr__ is lazy."""
    import adcp

    removed = (
        "BrandManifest",
        "FormatCategory",
        "DeliverTo",
        "PromotedProducts",
        "PromotedOfferings",
        "Pricing",
        "PackageStatus",
    )
    for name in removed:
        with pytest.raises(ImportError) as exc:
            getattr(adcp, name)
        assert "MIGRATION_v3_to_v4.md" in str(exc.value)
