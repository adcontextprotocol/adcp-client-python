"""Types declared by the AdCP ``brand/verify_brand_claims_response`` schema.

One public module per schema, so a type name its own domain declares
more than once is still unambiguous:

    from adcp.types.domains.brand.verify_brand_claims_response import <Type>

Auto-generated from the generated_poc module tree. DO NOT EDIT MANUALLY.
Generation date: 2026-10-03 13:36:32 UTC
"""

# ruff: noqa: E501, I001
from __future__ import annotations

from adcp.types.generated_poc.brand.verify_brand_claims_response import (
    ClaimType,
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
    "ClaimType",
    "ResultEntry",
    "ResultEntry1",
    "ResultEntry2",
    "VerifyBrandClaimsErrorResponse",
    "VerifyBrandClaimsPayload",
    "VerifyBrandClaimsResponse",
    "VerifyBrandClaimsResponseBulk",
    "VerifyBrandClaimsSignedResponse",
    "VerifyBrandClaimsSignedSuccessPayload",
]
