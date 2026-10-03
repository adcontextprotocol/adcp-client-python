"""Types declared by the AdCP ``core/canonical_audience_evidence`` schema.

One public module per schema, so a type name its own domain declares
more than once is still unambiguous:

    from adcp.types.domains.core.canonical_audience_evidence import <Type>

Auto-generated from the generated_poc module tree. DO NOT EDIT MANUALLY.
Generation date: 2026-10-03 13:36:32 UTC
"""

# ruff: noqa: E501, I001
from __future__ import annotations

from adcp.types.generated_poc.core.canonical_audience_evidence import (
    AttestationDigest,
    Baseline,
    CanonicalAudienceEvidence,
    EvidenceType,
    Relationship,
    Unit,
)

# Explicit exports
__all__ = [
    "AttestationDigest",
    "Baseline",
    "CanonicalAudienceEvidence",
    "EvidenceType",
    "Relationship",
    "Unit",
]
