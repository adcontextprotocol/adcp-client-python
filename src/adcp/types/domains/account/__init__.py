"""Types the AdCP ``account`` schemas declare.

Importing from the domain says which variant you mean, where the flat
``adcp.types`` namespace can only bind one class per name:

    from adcp.types.domains.account import <Type>

A type this domain declares in more than one schema is not here: import
    it from its own schema's module, ``adcp.types.domains.account.<schema>``.
Nothing here is renamed.

This root is lazy (PEP 562): importing it, or a schema module under it,
does not import every schema in the domain. A name is bound from its
declaring module on first access and cached here. That is what keeps a
schema module's ``from ...<domain> import <sibling>`` from re-entering a
half-initialized module through this package (#1784).

Auto-generated from the generated domain tree. DO NOT EDIT MANUALLY.
Generation date: 2026-11-01 23:44:03 UTC
"""

# ruff: noqa: E501, I001
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
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
        SourceCoverageItem,
        Status54,
    )
    from adcp.types.domains.account.list_accounts_request import ListAccountsRequest, RequestId
    from adcp.types.domains.account.list_accounts_response import ListAccountsResponse
    from adcp.types.domains.account.report_usage_request import ReportUsageRequest, UsageItem
    from adcp.types.domains.account.report_usage_response import ReportUsageResponse
    from adcp.types.domains.account.request_account_request import RequestAccountRequest
    from adcp.types.domains.account.request_account_response import (
        RequestAccountResponse,
        RequestAccountResponse1,
        RequestAccountResponse2,
    )
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
    "ListAccountsRequest",
    "ListAccountsResponse",
    "ReportUsageRequest",
    "ReportUsageResponse",
    "RequestAccountRequest",
    "RequestAccountResponse",
    "RequestAccountResponse1",
    "RequestAccountResponse2",
    "RequestId",
    "Setup",
    "SourceCoverageItem",
    "Spend",
    "StartingPosition",
    "Status54",
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


#: The schema module that declares each name, for the lazy binding below.
_DECLARED_IN = {
    "Accounts": "adcp.types.domains.account.sync_accounts_request",
    "Accounts1": "adcp.types.domains.account.sync_accounts_request",
    "Authentication": "adcp.types.domains.account.sync_governance_request",
    "Balance": "adcp.types.domains.account.get_account_financials_response",
    "Credit": "adcp.types.domains.account.get_account_financials_response",
    "CreditLimit": "adcp.types.domains.account.sync_accounts_response",
    "GetAccountFinancialsRequest": "adcp.types.domains.account.get_account_financials_request",
    "GetAccountFinancialsResponse": "adcp.types.domains.account.get_account_financials_response",
    "GetAccountFinancialsResponse1": "adcp.types.domains.account.get_account_financials_response",
    "GetAccountFinancialsResponse2": "adcp.types.domains.account.get_account_financials_response",
    "Invoice": "adcp.types.domains.account.get_account_financials_response",
    "Kind": "adcp.types.domains.account.list_account_changes_response",
    "LastTopUp": "adcp.types.domains.account.get_account_financials_response",
    "ListAccountChangesRequest": "adcp.types.domains.account.list_account_changes_request",
    "ListAccountChangesResponse": "adcp.types.domains.account.list_account_changes_response",
    "ListAccountsRequest": "adcp.types.domains.account.list_accounts_request",
    "ListAccountsResponse": "adcp.types.domains.account.list_accounts_response",
    "ReportUsageRequest": "adcp.types.domains.account.report_usage_request",
    "ReportUsageResponse": "adcp.types.domains.account.report_usage_response",
    "RequestAccountRequest": "adcp.types.domains.account.request_account_request",
    "RequestAccountResponse": "adcp.types.domains.account.request_account_response",
    "RequestAccountResponse1": "adcp.types.domains.account.request_account_response",
    "RequestAccountResponse2": "adcp.types.domains.account.request_account_response",
    "RequestId": "adcp.types.domains.account.list_accounts_request",
    "Setup": "adcp.types.domains.account.sync_accounts_response",
    "SourceCoverageItem": "adcp.types.domains.account.list_account_changes_response",
    "Spend": "adcp.types.domains.account.get_account_financials_response",
    "StartingPosition": "adcp.types.domains.account.list_account_changes_request",
    "Status54": "adcp.types.domains.account.list_account_changes_response",
    "SyncAccountsRequest": "adcp.types.domains.account.sync_accounts_request",
    "SyncAccountsResponse": "adcp.types.domains.account.sync_accounts_response",
    "SyncAccountsResponse1": "adcp.types.domains.account.sync_accounts_response",
    "SyncAccountsResponse2": "adcp.types.domains.account.sync_accounts_response",
    "SyncGovernanceRequest": "adcp.types.domains.account.sync_governance_request",
    "SyncGovernanceResponse": "adcp.types.domains.account.sync_governance_response",
    "SyncGovernanceResponse1": "adcp.types.domains.account.sync_governance_response",
    "SyncGovernanceResponse2": "adcp.types.domains.account.sync_governance_response",
    "UsageItem": "adcp.types.domains.account.report_usage_request",
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
