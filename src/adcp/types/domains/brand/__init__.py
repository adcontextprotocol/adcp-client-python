"""Types the AdCP ``brand`` schemas declare.

Importing from the domain says which variant you mean, where the flat
``adcp.types`` namespace can only bind one class per name:

    from adcp.types.domains.brand import <Type>

A type this domain declares in more than one schema is not here: import
    it from its own schema's module, ``adcp.types.domains.brand.<schema>``.
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
    from adcp.types.domains.brand.acquire_rights_request import AcquireRightsRequest, Campaign
    from adcp.types.domains.brand.acquire_rights_response import (
        AcquireRightsResponse,
        AcquireRightsResponse1,
        AcquireRightsResponse2,
        AcquireRightsResponse3,
        AcquireRightsResponse4,
        Disclosure,
    )
    from adcp.types.domains.brand.creative_approval_request import CreativeApprovalRequest
    from adcp.types.domains.brand.creative_approval_response import (
        CreativeApprovalResponse,
        CreativeApprovalResponse1,
        CreativeApprovalResponse2,
        CreativeApprovalResponse3,
        CreativeApprovalResponse4,
    )
    from adcp.types.domains.brand.get_brand_identity_request import Field1, GetBrandIdentityRequest
    from adcp.types.domains.brand.get_brand_identity_response import (
        Asset,
        Colors,
        File,
        FontRole2,
        Fonts,
        GetBrandIdentityResponse,
        GetBrandIdentityResponse1,
        GetBrandIdentityResponse2,
        Tone,
        VoiceSynthesis,
    )
    from adcp.types.domains.brand.get_rights_request import GetRightsRequest
    from adcp.types.domains.brand.get_rights_response import (
        Excluded,
        ExclusivityStatus,
        GetRightsResponse,
        GetRightsResponse1,
        GetRightsResponse2,
        PreviewAsset,
        Right,
    )
    from adcp.types.domains.brand.revocation_notification import RevocationNotification
    from adcp.types.domains.brand.rights_pricing_option import RightsPricingOption
    from adcp.types.domains.brand.rights_terms import Exclusivity, RightsTerms
    from adcp.types.domains.brand.search_brands_request import SearchBrandsRequest
    from adcp.types.domains.brand.search_brands_response import (
        Background,
        ExcludedCountry,
        KellerType,
        Orientation,
        RelationshipTrust,
        SearchBrandResult,
        SearchBrandsResponse,
        Variant,
    )
    from adcp.types.domains.brand.update_rights_request import UpdateRightsRequest
    from adcp.types.domains.brand.update_rights_response import (
        UpdateRightsResponse,
        UpdateRightsResponse1,
        UpdateRightsResponse2,
    )
    from adcp.types.domains.brand.verification_status import VerificationStatus
    from adcp.types.domains.brand.verify_brand_claim_request import VerifyBrandClaimRequest
    from adcp.types.domains.brand.verify_brand_claim_response import (
        VerifyBrandClaimErrorResponse,
        VerifyBrandClaimPayload,
        VerifyBrandClaimResponse,
        VerifyBrandClaimSignedResponse,
        VerifyBrandClaimSignedSuccessPayload,
        VerifyBrandClaimSuccessResponse,
    )
    from adcp.types.domains.brand.verify_brand_claims_request import (
        Claim,
        Claim1,
        Claim2,
        Claim3,
        ClaimEntry,
        ClaimEntry1,
        ClaimEntry2,
        ClaimEntry3,
        ClaimEntry4,
        Property,
        Store,
        VerifyBrandClaimsRequest,
        VerifyBrandClaimsRequestBulk,
    )
    from adcp.types.domains.brand.verify_brand_claims_response import (
        ResultEntry,
        ResultEntry1,
        ResultEntry2,
        VerifyBrandClaimsErrorResponse,
        VerifyBrandClaimsPayload,
        VerifyBrandClaimsResponse,
        VerifyBrandClaimsResponseBulk,
        VerifyBrandClaimsSignedResponse,
        VerifyBrandClaimsSignedSuccessPayload,
    )

# Explicit exports
__all__ = [
    "AcquireRightsRequest",
    "AcquireRightsResponse",
    "AcquireRightsResponse1",
    "AcquireRightsResponse2",
    "AcquireRightsResponse3",
    "AcquireRightsResponse4",
    "Asset",
    "Background",
    "Campaign",
    "Claim",
    "Claim1",
    "Claim2",
    "Claim3",
    "ClaimEntry",
    "ClaimEntry1",
    "ClaimEntry2",
    "ClaimEntry3",
    "ClaimEntry4",
    "Colors",
    "CreativeApprovalRequest",
    "CreativeApprovalResponse",
    "CreativeApprovalResponse1",
    "CreativeApprovalResponse2",
    "CreativeApprovalResponse3",
    "CreativeApprovalResponse4",
    "Disclosure",
    "Excluded",
    "ExcludedCountry",
    "Exclusivity",
    "ExclusivityStatus",
    "Field1",
    "File",
    "FontRole2",
    "Fonts",
    "GetBrandIdentityRequest",
    "GetBrandIdentityResponse",
    "GetBrandIdentityResponse1",
    "GetBrandIdentityResponse2",
    "GetRightsRequest",
    "GetRightsResponse",
    "GetRightsResponse1",
    "GetRightsResponse2",
    "KellerType",
    "Orientation",
    "PreviewAsset",
    "Property",
    "RelationshipTrust",
    "ResultEntry",
    "ResultEntry1",
    "ResultEntry2",
    "RevocationNotification",
    "Right",
    "RightsPricingOption",
    "RightsTerms",
    "SearchBrandResult",
    "SearchBrandsRequest",
    "SearchBrandsResponse",
    "Store",
    "Tone",
    "UpdateRightsRequest",
    "UpdateRightsResponse",
    "UpdateRightsResponse1",
    "UpdateRightsResponse2",
    "Variant",
    "VerificationStatus",
    "VerifyBrandClaimErrorResponse",
    "VerifyBrandClaimPayload",
    "VerifyBrandClaimRequest",
    "VerifyBrandClaimResponse",
    "VerifyBrandClaimSignedResponse",
    "VerifyBrandClaimSignedSuccessPayload",
    "VerifyBrandClaimSuccessResponse",
    "VerifyBrandClaimsErrorResponse",
    "VerifyBrandClaimsPayload",
    "VerifyBrandClaimsRequest",
    "VerifyBrandClaimsRequestBulk",
    "VerifyBrandClaimsResponse",
    "VerifyBrandClaimsResponseBulk",
    "VerifyBrandClaimsSignedResponse",
    "VerifyBrandClaimsSignedSuccessPayload",
    "VoiceSynthesis",
]


#: The schema module that declares each name, for the lazy binding below.
_DECLARED_IN = {
    "AcquireRightsRequest": "adcp.types.domains.brand.acquire_rights_request",
    "AcquireRightsResponse": "adcp.types.domains.brand.acquire_rights_response",
    "AcquireRightsResponse1": "adcp.types.domains.brand.acquire_rights_response",
    "AcquireRightsResponse2": "adcp.types.domains.brand.acquire_rights_response",
    "AcquireRightsResponse3": "adcp.types.domains.brand.acquire_rights_response",
    "AcquireRightsResponse4": "adcp.types.domains.brand.acquire_rights_response",
    "Asset": "adcp.types.domains.brand.get_brand_identity_response",
    "Background": "adcp.types.domains.brand.search_brands_response",
    "Campaign": "adcp.types.domains.brand.acquire_rights_request",
    "Claim": "adcp.types.domains.brand.verify_brand_claims_request",
    "Claim1": "adcp.types.domains.brand.verify_brand_claims_request",
    "Claim2": "adcp.types.domains.brand.verify_brand_claims_request",
    "Claim3": "adcp.types.domains.brand.verify_brand_claims_request",
    "ClaimEntry": "adcp.types.domains.brand.verify_brand_claims_request",
    "ClaimEntry1": "adcp.types.domains.brand.verify_brand_claims_request",
    "ClaimEntry2": "adcp.types.domains.brand.verify_brand_claims_request",
    "ClaimEntry3": "adcp.types.domains.brand.verify_brand_claims_request",
    "ClaimEntry4": "adcp.types.domains.brand.verify_brand_claims_request",
    "Colors": "adcp.types.domains.brand.get_brand_identity_response",
    "CreativeApprovalRequest": "adcp.types.domains.brand.creative_approval_request",
    "CreativeApprovalResponse": "adcp.types.domains.brand.creative_approval_response",
    "CreativeApprovalResponse1": "adcp.types.domains.brand.creative_approval_response",
    "CreativeApprovalResponse2": "adcp.types.domains.brand.creative_approval_response",
    "CreativeApprovalResponse3": "adcp.types.domains.brand.creative_approval_response",
    "CreativeApprovalResponse4": "adcp.types.domains.brand.creative_approval_response",
    "Disclosure": "adcp.types.domains.brand.acquire_rights_response",
    "Excluded": "adcp.types.domains.brand.get_rights_response",
    "ExcludedCountry": "adcp.types.domains.brand.search_brands_response",
    "Exclusivity": "adcp.types.domains.brand.rights_terms",
    "ExclusivityStatus": "adcp.types.domains.brand.get_rights_response",
    "Field1": "adcp.types.domains.brand.get_brand_identity_request",
    "File": "adcp.types.domains.brand.get_brand_identity_response",
    "FontRole2": "adcp.types.domains.brand.get_brand_identity_response",
    "Fonts": "adcp.types.domains.brand.get_brand_identity_response",
    "GetBrandIdentityRequest": "adcp.types.domains.brand.get_brand_identity_request",
    "GetBrandIdentityResponse": "adcp.types.domains.brand.get_brand_identity_response",
    "GetBrandIdentityResponse1": "adcp.types.domains.brand.get_brand_identity_response",
    "GetBrandIdentityResponse2": "adcp.types.domains.brand.get_brand_identity_response",
    "GetRightsRequest": "adcp.types.domains.brand.get_rights_request",
    "GetRightsResponse": "adcp.types.domains.brand.get_rights_response",
    "GetRightsResponse1": "adcp.types.domains.brand.get_rights_response",
    "GetRightsResponse2": "adcp.types.domains.brand.get_rights_response",
    "KellerType": "adcp.types.domains.brand.search_brands_response",
    "Orientation": "adcp.types.domains.brand.search_brands_response",
    "PreviewAsset": "adcp.types.domains.brand.get_rights_response",
    "Property": "adcp.types.domains.brand.verify_brand_claims_request",
    "RelationshipTrust": "adcp.types.domains.brand.search_brands_response",
    "ResultEntry": "adcp.types.domains.brand.verify_brand_claims_response",
    "ResultEntry1": "adcp.types.domains.brand.verify_brand_claims_response",
    "ResultEntry2": "adcp.types.domains.brand.verify_brand_claims_response",
    "RevocationNotification": "adcp.types.domains.brand.revocation_notification",
    "Right": "adcp.types.domains.brand.get_rights_response",
    "RightsPricingOption": "adcp.types.domains.brand.rights_pricing_option",
    "RightsTerms": "adcp.types.domains.brand.rights_terms",
    "SearchBrandResult": "adcp.types.domains.brand.search_brands_response",
    "SearchBrandsRequest": "adcp.types.domains.brand.search_brands_request",
    "SearchBrandsResponse": "adcp.types.domains.brand.search_brands_response",
    "Store": "adcp.types.domains.brand.verify_brand_claims_request",
    "Tone": "adcp.types.domains.brand.get_brand_identity_response",
    "UpdateRightsRequest": "adcp.types.domains.brand.update_rights_request",
    "UpdateRightsResponse": "adcp.types.domains.brand.update_rights_response",
    "UpdateRightsResponse1": "adcp.types.domains.brand.update_rights_response",
    "UpdateRightsResponse2": "adcp.types.domains.brand.update_rights_response",
    "Variant": "adcp.types.domains.brand.search_brands_response",
    "VerificationStatus": "adcp.types.domains.brand.verification_status",
    "VerifyBrandClaimErrorResponse": "adcp.types.domains.brand.verify_brand_claim_response",
    "VerifyBrandClaimPayload": "adcp.types.domains.brand.verify_brand_claim_response",
    "VerifyBrandClaimRequest": "adcp.types.domains.brand.verify_brand_claim_request",
    "VerifyBrandClaimResponse": "adcp.types.domains.brand.verify_brand_claim_response",
    "VerifyBrandClaimSignedResponse": "adcp.types.domains.brand.verify_brand_claim_response",
    "VerifyBrandClaimSignedSuccessPayload": "adcp.types.domains.brand.verify_brand_claim_response",
    "VerifyBrandClaimSuccessResponse": "adcp.types.domains.brand.verify_brand_claim_response",
    "VerifyBrandClaimsErrorResponse": "adcp.types.domains.brand.verify_brand_claims_response",
    "VerifyBrandClaimsPayload": "adcp.types.domains.brand.verify_brand_claims_response",
    "VerifyBrandClaimsRequest": "adcp.types.domains.brand.verify_brand_claims_request",
    "VerifyBrandClaimsRequestBulk": "adcp.types.domains.brand.verify_brand_claims_request",
    "VerifyBrandClaimsResponse": "adcp.types.domains.brand.verify_brand_claims_response",
    "VerifyBrandClaimsResponseBulk": "adcp.types.domains.brand.verify_brand_claims_response",
    "VerifyBrandClaimsSignedResponse": "adcp.types.domains.brand.verify_brand_claims_response",
    "VerifyBrandClaimsSignedSuccessPayload": "adcp.types.domains.brand.verify_brand_claims_response",
    "VoiceSynthesis": "adcp.types.domains.brand.get_brand_identity_response",
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
