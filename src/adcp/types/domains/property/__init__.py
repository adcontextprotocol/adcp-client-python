"""Types the AdCP ``property`` schemas declare.

Importing from the domain says which variant you mean, where the flat
``adcp.types`` namespace can only bind one class per name:

    from adcp.types.domains.property import <Type>

A type this domain declares in more than one schema is not here: import
    it from its own schema's module, ``adcp.types.domains.property.<schema>``.
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
    from adcp.types.domains.property.authorization_result import AuthorizationResult, Violation
    from adcp.types.domains.property.base_property_source import (
        BasePropertySource,
        BasePropertySource1,
        BasePropertySource2,
        BasePropertySource3,
    )
    from adcp.types.domains.property.create_property_list_request import CreatePropertyListRequest
    from adcp.types.domains.property.create_property_list_response import CreatePropertyListResponse
    from adcp.types.domains.property.delete_property_list_request import DeletePropertyListRequest
    from adcp.types.domains.property.delete_property_list_response import DeletePropertyListResponse
    from adcp.types.domains.property.delivery_record import DeliveryRecord
    from adcp.types.domains.property.get_property_list_request import (
        GetPropertyListRequest,
        Pagination,
    )
    from adcp.types.domains.property.get_property_list_response import GetPropertyListResponse
    from adcp.types.domains.property.list_property_lists_request import ListPropertyListsRequest
    from adcp.types.domains.property.list_property_lists_response import ListPropertyListsResponse
    from adcp.types.domains.property.property_error import Code, PropertyError
    from adcp.types.domains.property.property_feature import PropertyFeature
    from adcp.types.domains.property.property_feature_definition import (
        Coverage,
        PropertyFeatureDefinition,
        Range,
        Type,
    )
    from adcp.types.domains.property.property_feature_result import (
        CoverageStatus,
        PropertyFeatureResult,
    )
    from adcp.types.domains.property.property_feature_value import PropertyFeatureValue
    from adcp.types.domains.property.property_list import PropertyList
    from adcp.types.domains.property.property_list_changed_webhook import (
        ChangeSummary,
        PropertyListChangedWebhook,
    )
    from adcp.types.domains.property.property_list_filters import (
        CountriesAllItem,
        PropertyListFilters,
    )
    from adcp.types.domains.property.update_property_list_request import UpdatePropertyListRequest
    from adcp.types.domains.property.update_property_list_response import UpdatePropertyListResponse
    from adcp.types.domains.property.validate_property_delivery_request import (
        ValidatePropertyDeliveryRequest,
    )
    from adcp.types.domains.property.validate_property_delivery_response import (
        Aggregate,
        AuthorizationSummary,
        Summary,
        ValidatePropertyDeliveryResponse,
    )
    from adcp.types.domains.property.validation_result import Feature, Requirement, ValidationResult

#: Which schema module declares each re-exported name.
#:
#: Resolution is PER NAME, a deliberate divergence from
#: ``adcp.types.__init__``, which routes every name through one ``_eager``
#: module. Importing a whole domain graph on first access can re-enter the
#: cycle this laziness exists to break, when that first access happens while
#: a canonical format module is still initializing. Importing only the one
#: declaring module cannot.
_DECLARED_BY = {
    "property.authorization_result": (
        "AuthorizationResult",
        "Violation",
    ),
    "property.base_property_source": (
        "BasePropertySource",
        "BasePropertySource1",
        "BasePropertySource2",
        "BasePropertySource3",
    ),
    "property.create_property_list_request": ("CreatePropertyListRequest",),
    "property.create_property_list_response": ("CreatePropertyListResponse",),
    "property.delete_property_list_request": ("DeletePropertyListRequest",),
    "property.delete_property_list_response": ("DeletePropertyListResponse",),
    "property.delivery_record": ("DeliveryRecord",),
    "property.get_property_list_request": (
        "GetPropertyListRequest",
        "Pagination",
    ),
    "property.get_property_list_response": ("GetPropertyListResponse",),
    "property.list_property_lists_request": ("ListPropertyListsRequest",),
    "property.list_property_lists_response": ("ListPropertyListsResponse",),
    "property.property_error": (
        "Code",
        "PropertyError",
    ),
    "property.property_feature": ("PropertyFeature",),
    "property.property_feature_definition": (
        "Coverage",
        "PropertyFeatureDefinition",
        "Range",
        "Type",
    ),
    "property.property_feature_result": (
        "CoverageStatus",
        "PropertyFeatureResult",
    ),
    "property.property_feature_value": ("PropertyFeatureValue",),
    "property.property_list": ("PropertyList",),
    "property.property_list_changed_webhook": (
        "ChangeSummary",
        "PropertyListChangedWebhook",
    ),
    "property.property_list_filters": (
        "CountriesAllItem",
        "PropertyListFilters",
    ),
    "property.update_property_list_request": ("UpdatePropertyListRequest",),
    "property.update_property_list_response": ("UpdatePropertyListResponse",),
    "property.validate_property_delivery_request": ("ValidatePropertyDeliveryRequest",),
    "property.validate_property_delivery_response": (
        "Aggregate",
        "AuthorizationSummary",
        "Summary",
        "ValidatePropertyDeliveryResponse",
    ),
    "property.validation_result": (
        "Feature",
        "Requirement",
        "ValidationResult",
    ),
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
    "Aggregate",
    "AuthorizationResult",
    "AuthorizationSummary",
    "BasePropertySource",
    "BasePropertySource1",
    "BasePropertySource2",
    "BasePropertySource3",
    "ChangeSummary",
    "Code",
    "CountriesAllItem",
    "Coverage",
    "CoverageStatus",
    "CreatePropertyListRequest",
    "CreatePropertyListResponse",
    "DeletePropertyListRequest",
    "DeletePropertyListResponse",
    "DeliveryRecord",
    "Feature",
    "GetPropertyListRequest",
    "GetPropertyListResponse",
    "ListPropertyListsRequest",
    "ListPropertyListsResponse",
    "Pagination",
    "PropertyError",
    "PropertyFeature",
    "PropertyFeatureDefinition",
    "PropertyFeatureResult",
    "PropertyFeatureValue",
    "PropertyList",
    "PropertyListChangedWebhook",
    "PropertyListFilters",
    "Range",
    "Requirement",
    "Summary",
    "Type",
    "UpdatePropertyListRequest",
    "UpdatePropertyListResponse",
    "ValidatePropertyDeliveryRequest",
    "ValidatePropertyDeliveryResponse",
    "ValidationResult",
    "Violation",
]
