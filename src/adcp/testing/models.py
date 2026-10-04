"""Explicit deferred-schema setup for tests that patch datetime classes."""

from __future__ import annotations

import importlib
import sys
from types import ModuleType

from pydantic import BaseModel

from adcp._deferred_adapters import build_registered_adapters


def _realize_lazy_exports() -> None:
    # Freezegun enumerates dir(module), including unbound lazy names. Resolve the
    # same advertised surfaces before it patches datetime. Imports can introduce
    # additional lazy facades, so take fresh snapshots until the walk stabilizes.
    importlib.import_module("adcp.types")
    visited: set[str] = set()
    while True:
        modules = [
            module
            for name, module in tuple(sys.modules.items())
            if (name == "adcp" or name.startswith("adcp."))
            and isinstance(module, ModuleType)
            and name not in visited
        ]
        if not modules:
            return
        for module in modules:
            visited.add(module.__name__)
            if "__getattr__" in vars(module):
                for name in dir(module):
                    if not name.startswith("_"):
                        getattr(module, name)


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
