"""Types the AdCP ``property`` schemas declare.

Importing from the domain says which variant you mean, where the flat
``adcp.types`` namespace can only bind one class per name:

    from adcp.types.domains.property import <Type>

A type this domain declares in more than one schema is not here: import
    it from its own schema's module, ``adcp.types.domains.property.<schema>``.
Nothing here is renamed.

This root is lazy (PEP 562): importing it, or a schema module under it,
does not import every schema in the domain. A name is bound from its
declaring module on first access and cached here. That is what keeps a
schema module's ``from ...<domain> import <sibling>`` from re-entering a
half-initialized module through this package (#1784).

Auto-generated from the generated domain tree. DO NOT EDIT MANUALLY.
Generation date: 2026-11-01 23:49:37 UTC
"""

# ruff: noqa: E501, I001
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
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


#: The schema module that declares each name, for the lazy binding below.
_DECLARED_IN = {
    "Aggregate": "adcp.types.domains.property.validate_property_delivery_response",
    "AuthorizationResult": "adcp.types.domains.property.authorization_result",
    "AuthorizationSummary": "adcp.types.domains.property.validate_property_delivery_response",
    "BasePropertySource": "adcp.types.domains.property.base_property_source",
    "BasePropertySource1": "adcp.types.domains.property.base_property_source",
    "BasePropertySource2": "adcp.types.domains.property.base_property_source",
    "BasePropertySource3": "adcp.types.domains.property.base_property_source",
    "ChangeSummary": "adcp.types.domains.property.property_list_changed_webhook",
    "Code": "adcp.types.domains.property.property_error",
    "CountriesAllItem": "adcp.types.domains.property.property_list_filters",
    "Coverage": "adcp.types.domains.property.property_feature_definition",
    "CoverageStatus": "adcp.types.domains.property.property_feature_result",
    "CreatePropertyListRequest": "adcp.types.domains.property.create_property_list_request",
    "CreatePropertyListResponse": "adcp.types.domains.property.create_property_list_response",
    "DeletePropertyListRequest": "adcp.types.domains.property.delete_property_list_request",
    "DeletePropertyListResponse": "adcp.types.domains.property.delete_property_list_response",
    "DeliveryRecord": "adcp.types.domains.property.delivery_record",
    "Feature": "adcp.types.domains.property.validation_result",
    "GetPropertyListRequest": "adcp.types.domains.property.get_property_list_request",
    "GetPropertyListResponse": "adcp.types.domains.property.get_property_list_response",
    "ListPropertyListsRequest": "adcp.types.domains.property.list_property_lists_request",
    "ListPropertyListsResponse": "adcp.types.domains.property.list_property_lists_response",
    "Pagination": "adcp.types.domains.property.get_property_list_request",
    "PropertyError": "adcp.types.domains.property.property_error",
    "PropertyFeature": "adcp.types.domains.property.property_feature",
    "PropertyFeatureDefinition": "adcp.types.domains.property.property_feature_definition",
    "PropertyFeatureResult": "adcp.types.domains.property.property_feature_result",
    "PropertyFeatureValue": "adcp.types.domains.property.property_feature_value",
    "PropertyList": "adcp.types.domains.property.property_list",
    "PropertyListChangedWebhook": "adcp.types.domains.property.property_list_changed_webhook",
    "PropertyListFilters": "adcp.types.domains.property.property_list_filters",
    "Range": "adcp.types.domains.property.property_feature_definition",
    "Requirement": "adcp.types.domains.property.validation_result",
    "Summary": "adcp.types.domains.property.validate_property_delivery_response",
    "Type": "adcp.types.domains.property.property_feature_definition",
    "UpdatePropertyListRequest": "adcp.types.domains.property.update_property_list_request",
    "UpdatePropertyListResponse": "adcp.types.domains.property.update_property_list_response",
    "ValidatePropertyDeliveryRequest": "adcp.types.domains.property.validate_property_delivery_request",
    "ValidatePropertyDeliveryResponse": "adcp.types.domains.property.validate_property_delivery_response",
    "ValidationResult": "adcp.types.domains.property.validation_result",
    "Violation": "adcp.types.domains.property.authorization_result",
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
