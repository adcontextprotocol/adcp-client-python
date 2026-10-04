"""Types declared by the AdCP ``account/sync_governance_request`` schema.

One public module per schema, so a type name its own domain declares
more than once is still unambiguous:

    from adcp.types.domains.account.sync_governance_request import <Type>

Auto-generated from the generated_poc module tree. DO NOT EDIT MANUALLY.
Generation date: 2026-10-03 13:36:32 UTC
"""

# ruff: noqa: E501, I001
from __future__ import annotations

from adcp.types.generated_poc.account.sync_governance_request import (
    Account,
    Authentication,
    GovernanceAgent,
    SyncGovernanceRequest,
)

# Explicit exports
__all__ = ["Account", "Authentication", "GovernanceAgent", "SyncGovernanceRequest"]
