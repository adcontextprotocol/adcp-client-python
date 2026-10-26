"""Explicit deferred-schema setup for tests that patch datetime classes."""

from __future__ import annotations

import importlib
import sys
from types import ModuleType

from pydantic import BaseModel

from adcp._deferred_adapters import build_registered_adapters

#: Passes the lazy-export walk may take before it is declared non-terminating.
#: A pass only follows one that imported new modules; the SDK's own surface
#: stabilizes in a handful, so this is a backstop, not a tuning knob.
_MAX_REALIZE_PASSES = 100


def _realize_lazy_exports() -> None:
    # Freezegun enumerates dir(module), including unbound lazy names. Resolve the
    # same advertised surfaces before it patches datetime. Imports can introduce
    # additional lazy facades, so take fresh snapshots until the walk stabilizes.
    #
    # A module can sit in sys.modules under more than one name: the deprecated
    # ``adcp.types.generated_poc.*`` alias registers the canonical
    # ``adcp.types.domains.*`` object under its old name, so the KEY is not the
    # object's ``__name__``. Mark the key the snapshot filtered on (so every pass
    # retires what it found) and walk each module object once.
    importlib.import_module("adcp.types")
    seen_names: set[str] = set()
    walked: dict[int, ModuleType] = {}

    def unseen() -> list[tuple[str, ModuleType]]:
        return [
            (name, module)
            for name, module in tuple(sys.modules.items())
            if (name == "adcp" or name.startswith("adcp."))
            and isinstance(module, ModuleType)
            and name not in seen_names
        ]

    for _ in range(_MAX_REALIZE_PASSES):
        pending = unseen()
        if not pending:
            return
        for name, module in pending:
            seen_names.add(name)
            if id(module) in walked:
                continue
            # Holding the object keeps its id from being reused by another.
            walked[id(module)] = module
            if "__getattr__" in vars(module):
                for attr in dir(module):
                    if not attr.startswith("_"):
                        getattr(module, attr)
    remaining = sorted(name for name, _ in unseen())
    if not remaining:
        return
    raise RuntimeError(
        f"build_all_models(): realizing lazy exports did not stabilize after "
        f"{_MAX_REALIZE_PASSES} passes; the last pass still imported "
        f"{', '.join(remaining[:5])}"
        + (f" and {len(remaining) - 5} more" if len(remaining) > 5 else "")
    )


def build_all_models() -> None:
    """Prebuild imported SDK models/adapters before freezing datetime.

    In ``tests/conftest.py``, after importing adopter models and before any
    ``freeze_time`` block::

        import freezegun
        from adcp.testing import build_all_models

        freezegun.configure(extend_ignore_list=["adcp"])
        build_all_models()

    Realizes advertised lazy exports on ``adcp``, ``adcp.types`` and every
    imported SDK facade (including facades imported during this walk). Then
    recursively builds imported SDK models, their adopter subclasses, and
    registered SDK TypeAdapters. Completed schemas and adapters are cached;
    repeated calls do not force rebuilds. This does not import arbitrary SDK
    submodules or discover application modules. Import additional application
    models/submodules first, and call again before freezing if imports change.
    Keep ``adcp`` on freezegun's ignore list so its retained datetime references
    are not patched. Call this helper while real datetime classes are active.
    """
    _realize_lazy_exports()
    visited: set[type[BaseModel]] = set()

    def build(cls: type[BaseModel]) -> None:
        if cls in visited:
            return
        visited.add(cls)
        sdk_model = any(base.__module__.startswith("adcp.") for base in cls.__mro__)
        if sdk_model and not cls.__pydantic_complete__:
            cls.model_rebuild(force=False)
        for child in cls.__subclasses__():
            build(child)

    build(BaseModel)
    build_registered_adapters()
