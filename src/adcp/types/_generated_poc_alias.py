"""The pre-9.0 generated tree's import path, aliased to the module it moved to.

The generator used to write its models to ``adcp.types.generated_poc`` and the
public surface re-exported them. It now writes them to ``adcp.types.domains``
directly: the module that defines a class is the module adopters import it
from, so there is no private tree left to re-export.

Almost every module stem moved unchanged, so the migration is mostly a prefix
rename::

    -from adcp.types.generated_poc.media_buy.package_request import PackageRequest
    +from adcp.types.domains.media_buy.package_request import PackageRequest

``adcp migrate v3-to-v4 --apply`` rewrites those lines (and resolves the
split stem below per imported name); ``adcp.types`` remains the first choice,
and a domain path is for a name the flat namespace cannot bind, as
``docs/type-surface.md`` describes.

**A root discovery schema is the exception, and a prefix rename loses it.**
``scripts/generate_types.py``'s ``ROOT_DISCOVERY_SCHEMAS`` names every schema
whose basename collides with a task-schema DIRECTORY: ``brand.json`` against
``brand/*.json``. Pre-9.0 the generator wrote such a schema's models into the
colliding package's ``__init__`` — ``generated_poc/brand/__init__.py`` WAS
``brand.json``, 140 classes including ``Brand`` — and 9.0 writes them to
``domains/<stem>_discovery.py`` instead, leaving ``domains/<stem>/__init__.py``
to the generated domain aggregator. So one deprecated name has two canonical
halves, and ``generated_poc.brand`` resolving to ``domains.brand`` alone raises
``ImportError: cannot import name 'Brand'`` — an error naming a module the
adopter never wrote. ``_SplitAliasModule`` below serves both halves under the
one deprecated name, discovery half first because that is the surface the
pre-9.0 module had. The ``_discovery`` suffix is the generator's own naming
(``ROOT_DISCOVERY_SCHEMAS``' value for ``brand.json`` is ``brand_discovery.py``)
rather than a second list here, and
``tests/test_generated_poc_alias.py::test_a_root_discovery_module_is_named_the_way_the_alias_derives_it``
fails the build if the generator ever names one some other way.

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

A split module is the one place that cannot hold: two canonical modules cannot
be one object. The property that MATTERS is the second one, and the shim keeps
it — it binds no class of its own, it reads the two canonical modules.

    >>> from adcp.types.generated_poc.brand import Brand
    >>> from adcp.types.domains.brand_discovery import Brand as Canonical
    >>> Brand is Canonical
    True

Redirecting a package's ``__path__`` instead would make the import machinery
LOAD each module a second time under the old name, giving two module objects
and two class objects per class — and `isinstance` against the wrong one fails
for no visible reason. ``tests/test_export_surface_is_derived.py`` exists to
refuse exactly that, by asserting every generated class is DEFINED at the
address it is imported from. So the loader here never executes anything: it
imports the canonical module and hands that object back. A split name gets a
shim instead of one of its two halves, and the shim executes nothing either —
it binds no class, it reads the halves.

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
#: served by the identically-stemmed module under the second, plus — for a
#: root discovery schema — that module's ``_discovery`` sibling.
DEPRECATED_ROOT = "adcp.types.generated_poc"
CANONICAL_ROOT = "adcp.types.domains"

#: The major version that removes this module. Stated here because a
#: deprecation with no expiry is a permanent second surface.
REMOVED_IN_MAJOR = 10

#: The suffix ``scripts/generate_types.py`` gives a root discovery schema's
#: module when its basename collides with a task-schema directory. Derived from
#: there rather than restated as a list of stems, so a schema added to
#: ``ROOT_DISCOVERY_SCHEMAS`` tomorrow is served without touching this file.
ROOT_DISCOVERY_SUFFIX = "_discovery"


def canonical_name(deprecated: str) -> str:
    """``adcp.types.generated_poc.core.x`` -> ``adcp.types.domains.core.x``."""

    return CANONICAL_ROOT + deprecated[len(DEPRECATED_ROOT) :]


def discovery_half(canonical: str) -> str | None:
    """The second canonical module a split deprecated name also has to serve.

    ``adcp.types.domains.brand`` -> ``adcp.types.domains.brand_discovery``, and
    ``None`` for every name that did not split. A root discovery schema sits at
    the schema root, so only a DOMAIN-level package can be the colliding half;
    anything deeper, and any domain with no ``<stem>_discovery`` sibling, is an
    ordinary prefix rename.
    """

    stem = canonical[len(CANONICAL_ROOT) + 1 :]
    if not stem or "." in stem:
        return None
    candidate = f"{canonical}{ROOT_DISCOVERY_SUFFIX}"
    try:
        if importlib.util.find_spec(candidate) is None:
            return None
    except (ImportError, AttributeError, ValueError):
        return None
    return candidate


class _SplitAliasModule(ModuleType):
    """One deprecated name over the two canonical halves of a split module.

    Reads both; defines nothing. ``__getattr__`` fires only after normal
    instance lookup misses, so the two halves live in the instance dict and the
    attributes the import system writes (``__name__``, ``__spec__``, ``__path__``
    and friends) resolve without reaching it.

    ``__path__`` is the aggregator package's, which is what keeps
    ``generated_poc.brand.acquire_rights_request`` resolving: ``_find_and_load``
    reads the parent's ``__path__`` before any meta-path finder is consulted, so
    a shim that could not answer for it would raise
    "'adcp.types.generated_poc.brand' is not a package" for all 20 of that
    domain's leaf modules. Bound here rather than left to ``__getattr__``, which
    would reach the same object by the same fallback: a package attribute the
    import system reads on every submodule import is not a compatibility
    lookup, and ``_init_module_attrs`` skips it only because it is already set.

    The discovery half is tried first because it is the surface the pre-9.0
    module HAD — ``generated_poc/brand/__init__.py`` was ``brand.json`` and
    nothing else. The two halves share 12 names (``Logo``, ``Colors``,
    ``Fonts``, ...), and for those the pre-9.0 import got the discovery class.
    """

    def __init__(self, name: str, discovery: ModuleType, aggregator: ModuleType) -> None:
        super().__init__(name)
        self.__path__ = aggregator.__path__
        # Set through ``__dict__`` so ``__getattr__`` can read it without the
        # risk of recursing through a half-initialized instance.
        self.__dict__["_halves"] = (discovery, aggregator)

    def __getattr__(self, attr: str) -> object:
        for half in self.__dict__["_halves"]:
            try:
                return getattr(half, attr)
            except AttributeError:
                continue
        halves = ", ".join(half.__name__ for half in self.__dict__["_halves"])
        raise AttributeError(
            f"module {self.__name__!r} has no attribute {attr!r} (served from {halves})"
        )


class _AliasLoader:
    """Hands back the canonical module object instead of loading anything."""

    def __init__(self, canonical: str) -> None:
        self._canonical = canonical
        self._canonical_spec: ModuleSpec | None = None

    def create_module(self, spec: ModuleSpec) -> ModuleType:
        module = importlib.import_module(self._canonical)
        discovery = discovery_half(self._canonical)
        if discovery is not None:
            # A split name is the one case that gets its own module object: two
            # canonical modules cannot be one. It binds no class, so every class
            # reached through it is still the canonical class.
            return _SplitAliasModule(spec.name, importlib.import_module(discovery), module)
        # Captured before ``_init_module_attrs`` overwrites it with *spec*.
        self._canonical_spec = module.__spec__
        return module

    def exec_module(self, module: ModuleType) -> None:
        if isinstance(module, _SplitAliasModule):
            # The shim IS its own module; it keeps the spec naming itself, so a
            # reload re-runs this loader rather than one canonical half.
            return
        module.__spec__ = self._canonical_spec


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
            # A split name's advice is not the prefix rename, because the
            # classes this module used to hold are under the other half. Saying
            # "import adcp.types.domains.brand instead" and nothing more sends
            # the reader to a module that does not bind what they imported.
            discovery = discovery_half(canonical)
            if discovery is None:
                advice = (
                    f"import {canonical} instead. The module stems are "
                    f"unchanged, so this is a prefix rename, and "
                    f"`adcp migrate v3-to-v4 --apply` rewrites it."
                )
            else:
                advice = (
                    f"import {discovery} for the classes this module declared "
                    f"and {canonical} for the ones its domain aggregates — this "
                    f"stem split in two, so it is the one case that is not a "
                    f"prefix rename; `adcp migrate v3-to-v4 --apply` resolves each "
                    f"imported name to its half. See docs/types-9-migration.md."
                )
            warnings.warn(
                f"{fullname} is deprecated since adcp 9.0 and is removed in "
                f"v{REMOVED_IN_MAJOR}; {advice}",
                DeprecationWarning,
                stacklevel=2,
            )
        # ``ModuleSpec`` declares its loader as ``importlib.abc.Loader``, which
        # is a registration ABC: what the import system actually calls is
        # ``create_module`` / ``exec_module``, and ``_AliasLoader`` defines
        # both. typeshed's structural alternative, ``LoaderProtocol``, declares
        # only ``load_module`` — removed from ``Loader`` in 3.12 — so
        # implementing it would be the less honest of the two options.
        return ModuleSpec(fullname, cast("Loader", _AliasLoader(canonical)))


def install() -> None:
    """Put the finder at the front of ``sys.meta_path``, at most once."""

    if not any(isinstance(finder, _AliasFinder) for finder in sys.meta_path):
        sys.meta_path.insert(0, _AliasFinder())
