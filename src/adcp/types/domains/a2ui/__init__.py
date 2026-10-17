"""Types the AdCP ``a2ui`` schemas declare.

Importing from the domain says which variant you mean, where the flat
``adcp.types`` namespace can only bind one class per name:

    from adcp.types.domains.a2ui import <Type>

A type this domain declares in more than one schema is not here: import
    it from its own schema's module, ``adcp.types.domains.a2ui.<schema>``.
Nothing here is renamed.

This facade is LAZY (PEP 562). It binds no name at import time, because an
eager one makes every leaf of this domain depend on every other leaf: a
submodule import runs this ``__init__``, so one leaf's dependencies become
the whole domain's. That turned a benign submodule-level cycle between
``core`` and ``formats.canonical`` -- two domains whose schemas reference
each other, which Python handles -- into a package-level one that made all
16 modules under ``formats/canonical/`` unimportable on their own.

Auto-generated from the generated domain tree. DO NOT EDIT MANUALLY.
Generation date: 2026-10-16 22:14:49 UTC
"""

# ruff: noqa: E501, I001
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    # The whole importable surface, so type checkers and IDEs see it. Runtime
    # resolution is ``__getattr__`` below; mypy never sees that, so a typo'd
    # import is still an error rather than ``object``.
    from adcp.types.domains.a2ui.bound_value import (
        A2UiBoundValue,
        A2UiBoundValue1,
        A2UiBoundValue2,
        A2UiBoundValue3,
        A2UiBoundValue4,
        A2UiBoundValue5,
    )
    from adcp.types.domains.a2ui.component import A2UiComponent
    from adcp.types.domains.a2ui.si_catalog import (
        Action23,
        Align,
        AppHandoff,
        Apps,
        Button,
        Card,
        Column,
        Component,
        Image,
        IntegrationAction,
        Justify,
        Layout,
        Link,
        List,
        ProductCard,
        Row,
        SiComponentCatalog,
        Template,
        Text,
        Type,
        Variant,
        Variant4,
    )
    from adcp.types.domains.a2ui.surface import A2UiSurface
    from adcp.types.domains.a2ui.user_action import A2UiUserAction

#: Which schema module declares each re-exported name.
#:
#: Resolution is PER NAME, a deliberate divergence from
#: ``adcp.types.__init__``, which routes every name through one ``_eager``
#: module. Importing a whole domain graph on first access can re-enter the
#: cycle this laziness exists to break, when that first access happens while
#: a canonical format module is still initializing. Importing only the one
#: declaring module cannot.
_DECLARED_BY = {
    "a2ui.bound_value": (
        "A2UiBoundValue",
        "A2UiBoundValue1",
        "A2UiBoundValue2",
        "A2UiBoundValue3",
        "A2UiBoundValue4",
        "A2UiBoundValue5",
    ),
    "a2ui.component": ("A2UiComponent",),
    "a2ui.si_catalog": (
        "Action23",
        "Align",
        "AppHandoff",
        "Apps",
        "Button",
        "Card",
        "Column",
        "Component",
        "Image",
        "IntegrationAction",
        "Justify",
        "Layout",
        "Link",
        "List",
        "ProductCard",
        "Row",
        "SiComponentCatalog",
        "Template",
        "Text",
        "Type",
        "Variant",
        "Variant4",
    ),
    "a2ui.surface": ("A2UiSurface",),
    "a2ui.user_action": ("A2UiUserAction",),
}

if not TYPE_CHECKING:
    import importlib

    _DECLARING_MODULE = {name: module for module, names in _DECLARED_BY.items() for name in names}

    def __getattr__(name: str) -> object:
        """Resolve a re-exported name by importing the ONE module declaring it."""
        module = _DECLARING_MODULE.get(name)
        if module is None:
            raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
        value = getattr(importlib.import_module(f"adcp.types.domains.{module}"), name)
        globals()[name] = value  # cache: __getattr__ fires once per name
        return value

    def __dir__() -> list[str]:
        return sorted(set(__all__) | set(globals()))


# Explicit exports
__all__ = [
    "A2UiBoundValue",
    "A2UiBoundValue1",
    "A2UiBoundValue2",
    "A2UiBoundValue3",
    "A2UiBoundValue4",
    "A2UiBoundValue5",
    "A2UiComponent",
    "A2UiSurface",
    "A2UiUserAction",
    "Action23",
    "Align",
    "AppHandoff",
    "Apps",
    "Button",
    "Card",
    "Column",
    "Component",
    "Image",
    "IntegrationAction",
    "Justify",
    "Layout",
    "Link",
    "List",
    "ProductCard",
    "Row",
    "SiComponentCatalog",
    "Template",
    "Text",
    "Type",
    "Variant",
    "Variant4",
]
