"""Types the AdCP ``brand`` schemas declare.

Importing from the domain says which variant you mean, where the flat
``adcp.types`` namespace can only bind one class per name:

    from adcp.types.domains.brand import <Type>

A type this domain declares in more than one schema is not here: import
    it from its own schema's module, ``adcp.types.domains.brand.<schema>``.
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
        SearchBrandsError,
        SearchBrandsResponse,
        SearchBrandsResponseBranches,
        SearchBrandsSuccess,
        Variant,
    )
    from adcp.types.domains.brand.update_rights_request import UpdateRightsRequest
    from adcp.types.domains.brand.update_rights_response import (
        UpdateRightsResponse,
        UpdateRightsResponse1,
        UpdateRightsResponse2,
    )
    from adcp.types.domains.brand.verification_status import VerificationStatus
    from adcp.types.domains.brand.verify_brand_claim_request import (
        VerifyBrandClaimRequest,
        VerifyBrandClaimRequestBranches,
        VerifyBrandClaimRequestVerifyParentClaim,
        VerifyBrandClaimRequestVerifyPropertyClaim,
        VerifyBrandClaimRequestVerifySubsidiaryClaim,
        VerifyBrandClaimRequestVerifyTrademarkClaim,
    )
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

#: Which schema module declares each re-exported name.
#:
#: Resolution is PER NAME, a deliberate divergence from
#: ``adcp.types.__init__``, which routes every name through one ``_eager``
#: module. Importing a whole domain graph on first access can re-enter the
#: cycle this laziness exists to break, when that first access happens while
#: a canonical format module is still initializing. Importing only the one
#: declaring module cannot.
_DECLARED_BY = {
    "brand.acquire_rights_request": (
        "AcquireRightsRequest",
        "Campaign",
    ),
    "brand.acquire_rights_response": (
        "AcquireRightsResponse",
        "AcquireRightsResponse1",
        "AcquireRightsResponse2",
        "AcquireRightsResponse3",
        "AcquireRightsResponse4",
        "Disclosure",
    ),
    "brand.creative_approval_request": ("CreativeApprovalRequest",),
    "brand.creative_approval_response": (
        "CreativeApprovalResponse",
        "CreativeApprovalResponse1",
        "CreativeApprovalResponse2",
        "CreativeApprovalResponse3",
        "CreativeApprovalResponse4",
    ),
    "brand.get_brand_identity_request": (
        "Field1",
        "GetBrandIdentityRequest",
    ),
    "brand.get_brand_identity_response": (
        "Asset",
        "Colors",
        "File",
        "FontRole2",
        "Fonts",
        "GetBrandIdentityResponse",
        "GetBrandIdentityResponse1",
        "GetBrandIdentityResponse2",
        "Tone",
        "VoiceSynthesis",
    ),
    "brand.get_rights_request": ("GetRightsRequest",),
    "brand.get_rights_response": (
        "Excluded",
        "ExclusivityStatus",
        "GetRightsResponse",
        "GetRightsResponse1",
        "GetRightsResponse2",
        "PreviewAsset",
        "Right",
    ),
    "brand.revocation_notification": ("RevocationNotification",),
    "brand.rights_pricing_option": ("RightsPricingOption",),
    "brand.rights_terms": (
        "Exclusivity",
        "RightsTerms",
    ),
    "brand.search_brands_request": ("SearchBrandsRequest",),
    "brand.search_brands_response": (
        "Background",
        "ExcludedCountry",
        "KellerType",
        "Orientation",
        "RelationshipTrust",
        "SearchBrandResult",
        "SearchBrandsError",
        "SearchBrandsResponse",
        "SearchBrandsResponseBranches",
        "SearchBrandsSuccess",
        "Variant",
    ),
    "brand.update_rights_request": ("UpdateRightsRequest",),
    "brand.update_rights_response": (
        "UpdateRightsResponse",
        "UpdateRightsResponse1",
        "UpdateRightsResponse2",
    ),
    "brand.verification_status": ("VerificationStatus",),
    "brand.verify_brand_claim_request": (
        "VerifyBrandClaimRequest",
        "VerifyBrandClaimRequestBranches",
        "VerifyBrandClaimRequestVerifyParentClaim",
        "VerifyBrandClaimRequestVerifyPropertyClaim",
        "VerifyBrandClaimRequestVerifySubsidiaryClaim",
        "VerifyBrandClaimRequestVerifyTrademarkClaim",
    ),
    "brand.verify_brand_claim_response": (
        "VerifyBrandClaimErrorResponse",
        "VerifyBrandClaimPayload",
        "VerifyBrandClaimResponse",
        "VerifyBrandClaimSignedResponse",
        "VerifyBrandClaimSignedSuccessPayload",
        "VerifyBrandClaimSuccessResponse",
    ),
    "brand.verify_brand_claims_request": (
        "Claim",
        "Claim1",
        "Claim2",
        "Claim3",
        "ClaimEntry",
        "ClaimEntry1",
        "ClaimEntry2",
        "ClaimEntry3",
        "ClaimEntry4",
        "Property",
        "Store",
        "VerifyBrandClaimsRequest",
        "VerifyBrandClaimsRequestBulk",
    ),
    "brand.verify_brand_claims_response": (
        "ResultEntry",
        "ResultEntry1",
        "ResultEntry2",
        "VerifyBrandClaimsErrorResponse",
        "VerifyBrandClaimsPayload",
        "VerifyBrandClaimsResponse",
        "VerifyBrandClaimsResponseBulk",
        "VerifyBrandClaimsSignedResponse",
        "VerifyBrandClaimsSignedSuccessPayload",
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
    "SearchBrandsError",
    "SearchBrandsRequest",
    "SearchBrandsResponse",
    "SearchBrandsResponseBranches",
    "SearchBrandsSuccess",
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
    "VerifyBrandClaimRequestBranches",
    "VerifyBrandClaimRequestVerifyParentClaim",
    "VerifyBrandClaimRequestVerifyPropertyClaim",
    "VerifyBrandClaimRequestVerifySubsidiaryClaim",
    "VerifyBrandClaimRequestVerifyTrademarkClaim",
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
