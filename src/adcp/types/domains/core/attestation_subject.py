"""Types declared by the AdCP ``core/attestation_subject`` schema.

One public module per schema, so a type name its own domain declares
more than once is still unambiguous:

    from adcp.types.domains.core.attestation_subject import <Type>

Auto-generated from the generated_poc module tree. DO NOT EDIT MANUALLY.
Generation date: 2026-10-03 13:36:32 UTC
"""

# ruff: noqa: E501, I001
from __future__ import annotations

from adcp.types.generated_poc.core.attestation_subject import (
    AttestationSubject,
    AttestationSubject1,
    AttestationSubject2,
    AttestationSubject3,
)

# Explicit exports
__all__ = [
    "AttestationSubject",
    "AttestationSubject1",
    "AttestationSubject2",
    "AttestationSubject3",
]
