"""Types the AdCP ``property`` schemas declare.

Importing from the domain says which variant you mean, where the flat
``adcp.types`` namespace can only bind one class per name:

    from adcp.types.domains.property import <Type>

A type this domain declares in more than one schema is not here: import
    it from its own schema's module, ``adcp.types.domains.property.<schema>``.
Nothing here is renamed.

Auto-generated from the generated_poc module tree. DO NOT EDIT MANUALLY.
Generation date: 2026-10-03 13:33:59 UTC
"""

# ruff: noqa: E501, I001
from __future__ import annotations

from adcp.types.generated_poc.property.authorization_result import AuthorizationResult, Violation
from adcp.types.generated_poc.property.base_property_source import (
    BasePropertySource,
    BasePropertySource1,
    BasePropertySource2,
    BasePropertySource3,
)
from adcp.types.generated_poc.property.create_property_list_request import CreatePropertyListRequest
from adcp.types.generated_poc.property.create_property_list_response import (
    CreatePropertyListResponse,
)
from adcp.types.generated_poc.property.delete_property_list_request import DeletePropertyListRequest
from adcp.types.generated_poc.property.delete_property_list_response import (
    DeletePropertyListResponse,
)
from adcp.types.generated_poc.property.delivery_record import DeliveryRecord
from adcp.types.generated_poc.property.get_property_list_request import (
    GetPropertyListRequest,
    Pagination,
)
from adcp.types.generated_poc.property.get_property_list_response import GetPropertyListResponse
from adcp.types.generated_poc.property.list_property_lists_request import ListPropertyListsRequest
from adcp.types.generated_poc.property.list_property_lists_response import ListPropertyListsResponse
from adcp.types.generated_poc.property.property_error import Code, PropertyError
from adcp.types.generated_poc.property.property_feature import PropertyFeature
from adcp.types.generated_poc.property.property_feature_definition import (
    Coverage,
    PropertyFeatureDefinition,
    Range,
    Type,
)
from adcp.types.generated_poc.property.property_feature_result import (
    CoverageStatus,
    PropertyFeatureResult,
)
from adcp.types.generated_poc.property.property_feature_value import PropertyFeatureValue
from adcp.types.generated_poc.property.property_list import PropertyList
from adcp.types.generated_poc.property.property_list_changed_webhook import (
    ChangeSummary,
    PropertyListChangedWebhook,
)
from adcp.types.generated_poc.property.property_list_filters import (
    CountriesAllItem,
    PropertyListFilters,
)
from adcp.types.generated_poc.property.update_property_list_request import UpdatePropertyListRequest
from adcp.types.generated_poc.property.update_property_list_response import (
    UpdatePropertyListResponse,
)
from adcp.types.generated_poc.property.validate_property_delivery_request import (
    ValidatePropertyDeliveryRequest,
)
from adcp.types.generated_poc.property.validate_property_delivery_response import (
    Aggregate,
    AuthorizationSummary,
    Summary,
    ValidatePropertyDeliveryResponse,
)
from adcp.types.generated_poc.property.validation_result import (
    Feature,
    Requirement,
    ValidationResult,
)

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
