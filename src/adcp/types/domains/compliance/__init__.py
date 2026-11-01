"""Types the AdCP ``compliance`` schemas declare.

Importing from the domain says which variant you mean, where the flat
``adcp.types`` namespace can only bind one class per name:

    from adcp.types.domains.compliance import <Type>

A type this domain declares in more than one schema is not here: import
    it from its own schema's module, ``adcp.types.domains.compliance.<schema>``.
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
    from adcp.types.domains.compliance.comply_test_controller_request import (
        Account,
        Action,
        AdvanceTo,
        AllowedTask,
        Arm,
        ComplyTestControllerRequest,
        InstallmentDeliveryItem,
        InstallmentPropertyDeliveryItem,
        Kind,
        Metric,
        NotYetMeasurableVendorMetric,
        NotYetMeasurableVendorMetricsByPackageItem,
        Operation,
        Params,
        PropertyDeliveryItem,
        PurgeKind,
        ReachWindow,
        ReportedSpend,
        TargetHealth,
    )
    from adcp.types.domains.compliance.comply_test_controller_response import (
        AttestationMode,
        ComplyResponseArm,
        ComplyTestControllerResponse,
        ComplyTestControllerResponse1,
        ComplyTestControllerResponse2,
        ComplyTestControllerResponse3,
        ComplyTestControllerResponse4,
        ComplyTestControllerResponse5,
        ComplyTestControllerResponse6,
        ComplyTestControllerResponse7,
        ComplyTestControllerResponse8,
        Error,
        Forced,
        IdentifierMatchProof,
        Method,
        Purpose,
        RecordedCalls,
        RecordedCalls1,
        RecordedCalls2,
    )
    from adcp.types.domains.compliance.get_creative_features_completion import (
        GetCreativeFeaturesComplianceCompletion,
    )
    from adcp.types.domains.compliance.task_completion_data import ComplianceTaskCompletionData

# Explicit exports
__all__ = [
    "Account",
    "Action",
    "AdvanceTo",
    "AllowedTask",
    "Arm",
    "AttestationMode",
    "ComplianceTaskCompletionData",
    "ComplyResponseArm",
    "ComplyTestControllerRequest",
    "ComplyTestControllerResponse",
    "ComplyTestControllerResponse1",
    "ComplyTestControllerResponse2",
    "ComplyTestControllerResponse3",
    "ComplyTestControllerResponse4",
    "ComplyTestControllerResponse5",
    "ComplyTestControllerResponse6",
    "ComplyTestControllerResponse7",
    "ComplyTestControllerResponse8",
    "Error",
    "Forced",
    "GetCreativeFeaturesComplianceCompletion",
    "IdentifierMatchProof",
    "InstallmentDeliveryItem",
    "InstallmentPropertyDeliveryItem",
    "Kind",
    "Method",
    "Metric",
    "NotYetMeasurableVendorMetric",
    "NotYetMeasurableVendorMetricsByPackageItem",
    "Operation",
    "Params",
    "PropertyDeliveryItem",
    "PurgeKind",
    "Purpose",
    "ReachWindow",
    "RecordedCalls",
    "RecordedCalls1",
    "RecordedCalls2",
    "ReportedSpend",
    "TargetHealth",
]


#: The schema module that declares each name, for the lazy binding below.
_DECLARED_IN = {
    "Account": "adcp.types.domains.compliance.comply_test_controller_request",
    "Action": "adcp.types.domains.compliance.comply_test_controller_request",
    "AdvanceTo": "adcp.types.domains.compliance.comply_test_controller_request",
    "AllowedTask": "adcp.types.domains.compliance.comply_test_controller_request",
    "Arm": "adcp.types.domains.compliance.comply_test_controller_request",
    "AttestationMode": "adcp.types.domains.compliance.comply_test_controller_response",
    "ComplianceTaskCompletionData": "adcp.types.domains.compliance.task_completion_data",
    "ComplyResponseArm": "adcp.types.domains.compliance.comply_test_controller_response",
    "ComplyTestControllerRequest": "adcp.types.domains.compliance.comply_test_controller_request",
    "ComplyTestControllerResponse": "adcp.types.domains.compliance.comply_test_controller_response",
    "ComplyTestControllerResponse1": "adcp.types.domains.compliance.comply_test_controller_response",
    "ComplyTestControllerResponse2": "adcp.types.domains.compliance.comply_test_controller_response",
    "ComplyTestControllerResponse3": "adcp.types.domains.compliance.comply_test_controller_response",
    "ComplyTestControllerResponse4": "adcp.types.domains.compliance.comply_test_controller_response",
    "ComplyTestControllerResponse5": "adcp.types.domains.compliance.comply_test_controller_response",
    "ComplyTestControllerResponse6": "adcp.types.domains.compliance.comply_test_controller_response",
    "ComplyTestControllerResponse7": "adcp.types.domains.compliance.comply_test_controller_response",
    "ComplyTestControllerResponse8": "adcp.types.domains.compliance.comply_test_controller_response",
    "Error": "adcp.types.domains.compliance.comply_test_controller_response",
    "Forced": "adcp.types.domains.compliance.comply_test_controller_response",
    "GetCreativeFeaturesComplianceCompletion": "adcp.types.domains.compliance.get_creative_features_completion",
    "IdentifierMatchProof": "adcp.types.domains.compliance.comply_test_controller_response",
    "InstallmentDeliveryItem": "adcp.types.domains.compliance.comply_test_controller_request",
    "InstallmentPropertyDeliveryItem": "adcp.types.domains.compliance.comply_test_controller_request",
    "Kind": "adcp.types.domains.compliance.comply_test_controller_request",
    "Method": "adcp.types.domains.compliance.comply_test_controller_response",
    "Metric": "adcp.types.domains.compliance.comply_test_controller_request",
    "NotYetMeasurableVendorMetric": "adcp.types.domains.compliance.comply_test_controller_request",
    "NotYetMeasurableVendorMetricsByPackageItem": "adcp.types.domains.compliance.comply_test_controller_request",
    "Operation": "adcp.types.domains.compliance.comply_test_controller_request",
    "Params": "adcp.types.domains.compliance.comply_test_controller_request",
    "PropertyDeliveryItem": "adcp.types.domains.compliance.comply_test_controller_request",
    "PurgeKind": "adcp.types.domains.compliance.comply_test_controller_request",
    "Purpose": "adcp.types.domains.compliance.comply_test_controller_response",
    "ReachWindow": "adcp.types.domains.compliance.comply_test_controller_request",
    "RecordedCalls": "adcp.types.domains.compliance.comply_test_controller_response",
    "RecordedCalls1": "adcp.types.domains.compliance.comply_test_controller_response",
    "RecordedCalls2": "adcp.types.domains.compliance.comply_test_controller_response",
    "ReportedSpend": "adcp.types.domains.compliance.comply_test_controller_request",
    "TargetHealth": "adcp.types.domains.compliance.comply_test_controller_request",
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
