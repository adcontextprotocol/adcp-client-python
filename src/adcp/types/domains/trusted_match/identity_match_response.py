"""Types declared by the AdCP ``trusted_match/identity_match_response`` schema.

One public module per schema, so a type name its own domain declares
more than once is still unambiguous:

    from adcp.types.domains.trusted_match.identity_match_response import <Type>

Auto-generated from the generated_poc module tree. DO NOT EDIT MANUALLY.
Generation date: 2026-10-03 13:36:32 UTC
"""

# ruff: noqa: E501, I001
from __future__ import annotations

from adcp.types.generated_poc.trusted_match.identity_match_response import (
    IdentityMatchResponse,
    IdentityMatchResponseRouterPublisher,
    TmpxMacro,
    TmpxProviders,
)

# Explicit exports
__all__ = [
    "IdentityMatchResponse",
    "IdentityMatchResponseRouterPublisher",
    "TmpxMacro",
    "TmpxProviders",
]
