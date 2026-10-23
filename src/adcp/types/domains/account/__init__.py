"""Types the AdCP ``account`` schemas declare.

Importing from the domain says which variant you mean, where the flat
``adcp.types`` namespace can only bind one class per name:

    from adcp.types.domains.account import <Type>

A type this domain declares in more than one schema is not here: import
    it from its own schema's module, ``adcp.types.domains.account.<schema>``.
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
    from adcp.types.domains.account.get_account_financials_request import (
        GetAccountFinancialsRequest,
    )
    from adcp.types.domains.account.get_account_financials_response import (
        Balance,
        Credit,
        GetAccountFinancialsResponse,
        GetAccountFinancialsResponse1,
        GetAccountFinancialsResponse2,
        Invoice,
        LastTopUp,
        Spend,
    )
    from adcp.types.domains.account.list_account_changes_request import (
        ListAccountChangesRequest,
        StartingPosition,
    )
    from adcp.types.domains.account.list_account_changes_response import (
        Kind,
        ListAccountChangesResponse,
        ListAccountChangesResponseBranches,
        ListAccountChangesResponseCompleted,
        ListAccountChangesResponseFailed,
        SourceCoverageItem,
        Status22,
    )
    from adcp.types.domains.account.list_accounts_request import ListAccountsRequest
    from adcp.types.domains.account.list_accounts_response import ListAccountsResponse
    from adcp.types.domains.account.report_usage_request import ReportUsageRequest, UsageItem
    from adcp.types.domains.account.report_usage_response import ReportUsageResponse
    from adcp.types.domains.account.sync_accounts_request import (
        Accounts,
        Accounts1,
        SyncAccountsRequest,
    )
    from adcp.types.domains.account.sync_accounts_response import (
        CreditLimit,
        Setup,
        SyncAccountsResponse,
        SyncAccountsResponse1,
        SyncAccountsResponse2,
    )
    from adcp.types.domains.account.sync_governance_request import (
        Authentication,
        SyncGovernanceRequest,
    )
    from adcp.types.domains.account.sync_governance_response import (
        SyncGovernanceResponse,
        SyncGovernanceResponse1,
        SyncGovernanceResponse2,
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
    "account.get_account_financials_request": ("GetAccountFinancialsRequest",),
    "account.get_account_financials_response": (
        "Balance",
        "Credit",
        "GetAccountFinancialsResponse",
        "GetAccountFinancialsResponse1",
        "GetAccountFinancialsResponse2",
        "Invoice",
        "LastTopUp",
        "Spend",
    ),
    "account.list_account_changes_request": (
        "ListAccountChangesRequest",
        "StartingPosition",
    ),
    "account.list_account_changes_response": (
        "Kind",
        "ListAccountChangesResponse",
        "ListAccountChangesResponseBranches",
        "ListAccountChangesResponseCompleted",
        "ListAccountChangesResponseFailed",
        "SourceCoverageItem",
        "Status22",
    ),
    "account.list_accounts_request": ("ListAccountsRequest",),
    "account.list_accounts_response": ("ListAccountsResponse",),
    "account.report_usage_request": (
        "ReportUsageRequest",
        "UsageItem",
    ),
    "account.report_usage_response": ("ReportUsageResponse",),
    "account.sync_accounts_request": (
        "Accounts",
        "Accounts1",
        "SyncAccountsRequest",
    ),
    "account.sync_accounts_response": (
        "CreditLimit",
        "Setup",
        "SyncAccountsResponse",
        "SyncAccountsResponse1",
        "SyncAccountsResponse2",
    ),
    "account.sync_governance_request": (
        "Authentication",
        "SyncGovernanceRequest",
    ),
    "account.sync_governance_response": (
        "SyncGovernanceResponse",
        "SyncGovernanceResponse1",
        "SyncGovernanceResponse2",
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
    "Accounts",
    "Accounts1",
    "Authentication",
    "Balance",
    "Credit",
    "CreditLimit",
    "GetAccountFinancialsRequest",
    "GetAccountFinancialsResponse",
    "GetAccountFinancialsResponse1",
    "GetAccountFinancialsResponse2",
    "Invoice",
    "Kind",
    "LastTopUp",
    "ListAccountChangesRequest",
    "ListAccountChangesResponse",
    "ListAccountChangesResponseBranches",
    "ListAccountChangesResponseCompleted",
    "ListAccountChangesResponseFailed",
    "ListAccountsRequest",
    "ListAccountsResponse",
    "ReportUsageRequest",
    "ReportUsageResponse",
    "Setup",
    "SourceCoverageItem",
    "Spend",
    "StartingPosition",
    "Status22",
    "SyncAccountsRequest",
    "SyncAccountsResponse",
    "SyncAccountsResponse1",
    "SyncAccountsResponse2",
    "SyncGovernanceRequest",
    "SyncGovernanceResponse",
    "SyncGovernanceResponse1",
    "SyncGovernanceResponse2",
    "UsageItem",
]
