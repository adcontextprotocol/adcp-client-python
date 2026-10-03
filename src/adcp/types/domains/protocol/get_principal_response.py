"""Types declared by the AdCP ``protocol/get_principal_response`` schema.

One public module per schema, so a type name its own domain declares
more than once is still unambiguous:

    from adcp.types.domains.protocol.get_principal_response import <Type>

Auto-generated from the generated_poc module tree. DO NOT EDIT MANUALLY.
Generation date: 2026-10-03 13:36:32 UTC
"""

# ruff: noqa: E501, I001
from __future__ import annotations

from adcp.types.generated_poc.protocol.get_principal_response import (
    GetPrincipalResponse,
    PrincipalCurrentResult,
    PrincipalReadFailedResult,
    PrincipalRecognizedResult,
    PrincipalUnconfiguredResult,
    Result,
    Result6,
    Result7,
    Result9,
)

# Explicit exports
__all__ = [
    "GetPrincipalResponse",
    "PrincipalCurrentResult",
    "PrincipalReadFailedResult",
    "PrincipalRecognizedResult",
    "PrincipalUnconfiguredResult",
    "Result",
    "Result6",
    "Result7",
    "Result9",
]
