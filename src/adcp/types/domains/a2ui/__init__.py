"""Types the AdCP ``a2ui`` schemas declare.

Importing from the domain says which variant you mean, where the flat
``adcp.types`` namespace can only bind one class per name:

    from adcp.types.domains.a2ui import <Type>

A type this domain declares in more than one schema is not here: import
    it from its own schema's module, ``adcp.types.domains.a2ui.<schema>``.
Nothing here is renamed.

This root is lazy (PEP 562): importing it, or a schema module under it,
does not import every schema in the domain. A name is bound from its
declaring module on first access and cached here. That is what keeps a
schema module's ``from ...<domain> import <sibling>`` from re-entering a
half-initialized module through this package (#1784).

Auto-generated from the generated domain tree. DO NOT EDIT MANUALLY.
Generation date: 2026-11-01 23:44:03 UTC
"""

# ruff: noqa: E501, I001
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
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
        Action25,
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
    "Action25",
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


#: The schema module that declares each name, for the lazy binding below.
_DECLARED_IN = {
    "A2UiBoundValue": "adcp.types.domains.a2ui.bound_value",
    "A2UiBoundValue1": "adcp.types.domains.a2ui.bound_value",
    "A2UiBoundValue2": "adcp.types.domains.a2ui.bound_value",
    "A2UiBoundValue3": "adcp.types.domains.a2ui.bound_value",
    "A2UiBoundValue4": "adcp.types.domains.a2ui.bound_value",
    "A2UiBoundValue5": "adcp.types.domains.a2ui.bound_value",
    "A2UiComponent": "adcp.types.domains.a2ui.component",
    "A2UiSurface": "adcp.types.domains.a2ui.surface",
    "A2UiUserAction": "adcp.types.domains.a2ui.user_action",
    "Action25": "adcp.types.domains.a2ui.si_catalog",
    "Align": "adcp.types.domains.a2ui.si_catalog",
    "AppHandoff": "adcp.types.domains.a2ui.si_catalog",
    "Apps": "adcp.types.domains.a2ui.si_catalog",
    "Button": "adcp.types.domains.a2ui.si_catalog",
    "Card": "adcp.types.domains.a2ui.si_catalog",
    "Column": "adcp.types.domains.a2ui.si_catalog",
    "Component": "adcp.types.domains.a2ui.si_catalog",
    "Image": "adcp.types.domains.a2ui.si_catalog",
    "IntegrationAction": "adcp.types.domains.a2ui.si_catalog",
    "Justify": "adcp.types.domains.a2ui.si_catalog",
    "Layout": "adcp.types.domains.a2ui.si_catalog",
    "Link": "adcp.types.domains.a2ui.si_catalog",
    "List": "adcp.types.domains.a2ui.si_catalog",
    "ProductCard": "adcp.types.domains.a2ui.si_catalog",
    "Row": "adcp.types.domains.a2ui.si_catalog",
    "SiComponentCatalog": "adcp.types.domains.a2ui.si_catalog",
    "Template": "adcp.types.domains.a2ui.si_catalog",
    "Text": "adcp.types.domains.a2ui.si_catalog",
    "Type": "adcp.types.domains.a2ui.si_catalog",
    "Variant": "adcp.types.domains.a2ui.si_catalog",
    "Variant4": "adcp.types.domains.a2ui.si_catalog",
}

if not TYPE_CHECKING:
    import importlib as _importlib

    def __getattr__(name: str) -> object:
        module_name = _DECLARED_IN.get(name)
        if module_name is None:
            # Not a declared name: let the import system try a submodule, or fail.
            raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
        value = getattr(_importlib.import_module(module_name), name)
        globals()[name] = value
        return value

    def __dir__() -> list[str]:
        return sorted(set(globals()) | set(__all__))
