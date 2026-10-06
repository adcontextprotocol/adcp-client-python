"""The pre-9.0 generated tree's import path, aliased to the module it moved to.

The generator used to write its models to ``adcp.types.generated_poc`` and the
public surface re-exported them. It now writes them to ``adcp.types.domains``
directly: the module that defines a class is the module adopters import it
from, so there is no private tree left to re-export.

Every module stem moved unchanged, so the migration is a prefix rename::

    -from adcp.types.generated_poc.media_buy.package_request import PackageRequest
    +from adcp.types.domains.media_buy.package_request import PackageRequest

``adcp migrate v3-to-v4`` rewrites those lines; ``adcp.types`` remains the
first choice, and a domain path is for a name the flat namespace cannot bind,
as ``docs/type-surface.md`` describes.

#1360 measured 110 such imports in ONE production seller, so 9.0 keeps the old
path working for the 9.x line and warns, instead of breaking every one of them
at once. **It is removed in v10** — and
``tests/test_generated_poc_alias.py::test_the_alias_does_not_outlive_its_removal_major``
fails the build if this module is still here then.

The one property that makes this an alias rather than a second copy of the
tree: ``sys.modules`` holds ONE module object under both names.

    >>> import adcp.types.generated_poc.core.format_id as old
    >>> import adcp.types.domains.core.format_id as new
    >>> old is new
    True
    >>> old.FormatReferenceStructuredObject is new.FormatReferenceStructuredObject
    True

Redirecting a package's ``__path__`` instead would make the import machinery
LOAD each module a second time under the old name, giving two module objects
and two class objects per class — and `isinstance` against the wrong one fails
for no visible reason. ``tests/test_export_surface_is_derived.py`` exists to
refuse exactly that, by asserting every generated class is DEFINED at the
address it is imported from. So the loader here never executes anything: it
imports the canonical module and hands that object back.

The one write the import machinery makes to a module handed back from
``create_module`` is ``__spec__`` (``importlib._bootstrap._init_module_attrs``
sets ``__name__``, ``__loader__``, ``__package__``, ``__path__`` and
``__file__`` only when they are absent, and they are not). ``exec_module``
puts the canonical spec back, so the shared object carries no trace of having
been reached through the old name.
"""

from __future__ import annotations

import importlib
import importlib.util
import sys
import warnings
from collections.abc import Sequence
from importlib.machinery import ModuleSpec
from types import ModuleType
from typing import TYPE_CHECKING, cast

if TYPE_CHECKING:
    # For the one ``cast`` below, and nothing else. ``importlib.abc`` costs
    # ~9ms to import because it pulls ``importlib.resources`` ->
    # ``inspect``/``tempfile``/``typing``, and this module sits on
    # ``adcp.types``' import path — the same path whose cost ``adcp.types``'
    # PEP 562 ``__getattr__`` and ``adcp``'s deferred ``importlib.metadata``
    # exist to keep down. The import system reaches a loader and a finder
    # structurally, so neither class needs the ABC at runtime.
    from importlib.abc import Loader

#: The pre-9.0 path, and the path it moved to. Every name under the first is
#: served by the identically-stemmed module under the second.
DEPRECATED_ROOT = "adcp.types.generated_poc"
CANONICAL_ROOT = "adcp.types.domains"

#: The major version that removes this module. Stated here because a
#: deprecation with no expiry is a permanent second surface.
REMOVED_IN_MAJOR = 10

#: Per-module name overrides for names that moved to a *different* canonical
#: module when ``generated_poc`` was reorganised (e.g. a flat file split into
#: a package + sibling module).
#:
#: Format: deprecated_full_name -> {attr_name -> canonical_source_module}
#:
#: Only names **absent** from the default canonical target are listed — a name
#: already present in ``domains.<stem>`` is served by the normal redirect and
#: never needs an entry here.
_NAME_OVERRIDES: dict[str, dict[str, str]] = {
    # ``generated_poc/brand_discovery.py`` was a flat sibling of the
    # ``brand/`` package.  When the tree moved to ``domains/``, Brand,
    # BrandDiscovery3, and LocalizedName stayed in ``domains/brand_discovery.py``
    # rather than landing in ``domains/brand/__init__.py``.  Adopters whose
    # code pre-dates that split wrote
    #   ``from adcp.types.generated_poc.brand import Brand``
    # and the simple prefix rename redirects them to ``domains.brand``, which
    # does not carry those names.  This entry injects them so the import works.
    "adcp.types.generated_poc.brand": {
        "Brand": "adcp.types.domains.brand_discovery",
        "BrandDiscovery3": "adcp.types.domains.brand_discovery",
        "LocalizedName": "adcp.types.domains.brand_discovery",
    },
}


def canonical_name(deprecated: str) -> str:
    """``adcp.types.generated_poc.core.x`` -> ``adcp.types.domains.core.x``."""

    return CANONICAL_ROOT + deprecated[len(DEPRECATED_ROOT) :]


class _AliasLoader:
    """Hands back the canonical module object instead of loading anything."""

    def __init__(
        self,
        canonical: str,
        name_overrides: dict[str, str] | None = None,
    ) -> None:
        self._canonical = canonical
        # {attr_name -> canonical_source_module} for names that moved to a
        # different module than the simple prefix rename would produce.
        self._name_overrides = name_overrides
        self._canonical_spec: ModuleSpec | None = None

    def create_module(self, spec: ModuleSpec) -> ModuleType:
        module = importlib.import_module(self._canonical)
        # Captured before ``_init_module_attrs`` overwrites it with *spec*.
        self._canonical_spec = module.__spec__
        return module

    def exec_module(self, module: ModuleType) -> None:
        module.__spec__ = self._canonical_spec
        if self._name_overrides:
            # Inject names that moved to a sibling canonical module when the
            # deprecated module was split.  We only add names that are absent —
            # canonical names always win — so the canonical module is never
            # corrupted and the invariant (one object under both keys) holds.
            for attr_name, source_module_name in self._name_overrides.items():
                if hasattr(module, attr_name):
                    continue
                try:
                    src = importlib.import_module(source_module_name)
                    value = getattr(src, attr_name, None)
                    if value is not None:
                        setattr(module, attr_name, value)
                except ImportError:
                    pass


class _AliasFinder:
    """Serves every ``adcp.types.generated_poc`` name from its new address.

    Installed at the front of ``sys.meta_path`` so it is consulted before
    ``PathFinder``. That ordering is the whole mechanism for the submodules: a
    deprecated package resolves to the canonical package, whose ``__path__``
    points at the real directory, and ``PathFinder`` would happily load a
    second copy of every module on it under the deprecated name.

    The warning fires once per deprecated MODULE, tracked here rather than
    left to the import machinery's own caching: ``find_spec`` is called again
    for a name already in ``sys.modules`` whenever something probes it (
    ``adcp.types.__getattr__`` does, for its submodule passthrough), and a
    rename is one fact however many times it is looked up.
    """

    def __init__(self) -> None:
        self._warned: set[str] = set()

    def find_spec(
        self,
        fullname: str,
        path: Sequence[str] | None = None,
        target: ModuleType | None = None,
    ) -> ModuleSpec | None:
        if fullname != DEPRECATED_ROOT and not fullname.startswith(DEPRECATED_ROOT + "."):
            return None
        canonical = canonical_name(fullname)
        try:
            if importlib.util.find_spec(canonical) is None:
                return None
        except (ImportError, AttributeError, ValueError):
            # No such module under the new root either. Returning None lets the
            # ModuleNotFoundError name what the caller actually asked for.
            return None
        if fullname not in self._warned:
            self._warned.add(fullname)
            warnings.warn(
                f"{fullname} is deprecated since adcp 9.0 and is removed in "
                f"v{REMOVED_IN_MAJOR}; import {canonical} instead. The module "
                f"stems are unchanged, so this is a prefix rename, and "
                f"`adcp migrate v3-to-v4` rewrites it.",
                DeprecationWarning,
                stacklevel=2,
            )
        # ``ModuleSpec`` declares its loader as ``importlib.abc.Loader``, which
        # is a registration ABC: what the import system actually calls is
        # ``create_module`` / ``exec_module``, and ``_AliasLoader`` defines
        # both. typeshed's structural alternative, ``LoaderProtocol``, declares
        # only ``load_module`` — removed from ``Loader`` in 3.12 — so
        # implementing it would be the less honest of the two options.
        overrides = _NAME_OVERRIDES.get(fullname)
        return ModuleSpec(fullname, cast("Loader", _AliasLoader(canonical, overrides)))


def install() -> None:
    """Put the finder at the front of ``sys.meta_path``, at most once."""

    if not any(isinstance(finder, _AliasFinder) for finder in sys.meta_path):
        sys.meta_path.insert(0, _AliasFinder())
