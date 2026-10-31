"""Types the AdCP ``a2ui`` schemas declare.

Importing from the domain says which variant you mean, where the flat
``adcp.types`` namespace can only bind one class per name:

    from adcp.types.domains.a2ui import <Type>

A type this domain declares in more than one schema is not here: import
    it from its own schema's module, ``adcp.types.domains.a2ui.<schema>``.
Nothing here is renamed.

Auto-generated from the generated domain tree. DO NOT EDIT MANUALLY.
Generation date: 2026-10-30 14:25:27 UTC
"""

# ruff: noqa: E501, I001
from __future__ import annotations

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
