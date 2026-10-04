"""Types declared by the AdCP ``core/rights_constraint`` schema.

One public module per schema, so a type name its own domain declares
more than once is still unambiguous:

    from adcp.types.domains.core.rights_constraint import <Type>

Auto-generated from the generated_poc module tree. DO NOT EDIT MANUALLY.
Generation date: 2026-10-03 13:36:32 UTC
"""

# ruff: noqa: E501, I001
from __future__ import annotations

from adcp.types.generated_poc.core.rights_constraint import (
    ApprovalStatus,
    AttestationRef,
    Country,
    Disclosure,
    ExcludedCountry,
    GrantStatus,
    Issuer,
    Issuer8,
    Issuer9,
    Restriction,
    RightsAgent,
    RightsConstraint,
    Subject,
    Subject20,
    Subject29,
)

# Explicit exports
__all__ = [
    "ApprovalStatus",
    "AttestationRef",
    "Country",
    "Disclosure",
    "ExcludedCountry",
    "GrantStatus",
    "Issuer",
    "Issuer8",
    "Issuer9",
    "Restriction",
    "RightsAgent",
    "RightsConstraint",
    "Subject",
    "Subject20",
    "Subject29",
]
