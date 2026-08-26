"""Regression tests for concrete AccountReference arms on request models."""

from __future__ import annotations

import subprocess
import sys
from typing import get_args

from adcp.types import (
    AccountReferenceById,
    CompatibilityPurchaseCoordinatorInput,
    GetProductsRequest,
    ListCreativesRequest,
    SyncAccountsRequest,
)
from adcp.types.generated_poc.core.account_ref import AccountReference as GeneratedAccountReference
from adcp.types.versioned import make_versioned_base


def _contains_generated_wrapper(annotation: object) -> bool:
    return annotation is GeneratedAccountReference or any(
        _contains_generated_wrapper(arg) for arg in get_args(annotation)
    )


def test_alias_first_import_order_keeps_concrete_request_arms() -> None:
    code = """
from adcp.types.aliases import AccountReferenceById
from adcp.types import GetProductsRequest
request = GetProductsRequest(
    buying_mode="wholesale",
    brief="test",
    account={"account_id": "acc_123"},
)
assert isinstance(request.account, AccountReferenceById)
"""
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)

    assert result.returncode == 0, result.stderr


def test_get_products_account_returns_concrete_union_arm() -> None:
    request = GetProductsRequest(
        buying_mode="wholesale",
        brief="test",
        account={"account_id": "acc_123"},
    )

    assert isinstance(request.account, AccountReferenceById)
    assert not _contains_generated_wrapper(GetProductsRequest.model_fields["account"].annotation)


def test_nested_sync_accounts_account_returns_concrete_union_arm() -> None:
    request = SyncAccountsRequest(
        idempotency_key="sync-accounts-0001",
        accounts=[
            {
                "account": {"account_id": "acc_123"},
            }
        ],
    )

    assert isinstance(request.accounts[0].account, AccountReferenceById)


def test_nested_creative_filter_accounts_return_concrete_union_arms() -> None:
    request = ListCreativesRequest(filters={"accounts": [{"account_id": "acc_123"}]})

    assert isinstance(request.filters.accounts[0], AccountReferenceById)


def test_compatibility_input_account_returns_concrete_union_arm() -> None:
    request = CompatibilityPurchaseCoordinatorInput(
        idempotency_key="12345678-1234-5678-1234-567812345678",
        continuation_token="continuation-0001",
        account={"account_id": "acc_123"},
        selected_product_ids=["product_1"],
        accepted_losses=["feed_version_not_atomic", "pricing_version_not_atomic"],
        legacy_create_request={"idempotency_key": "legacy"},
    )

    assert isinstance(request.account, AccountReferenceById)


def test_versioned_request_account_returns_concrete_union_arm() -> None:
    request_type = make_versioned_base("3.1", "GetProductsRequest")
    request = request_type(
        buying_mode="wholesale",
        brief="test",
        account={"account_id": "acc_123"},
    )

    assert isinstance(request.account, AccountReferenceById)
    assert not _contains_generated_wrapper(request_type.model_fields["account"].annotation)


def test_versioned_nested_account_returns_concrete_union_arm() -> None:
    request_type = make_versioned_base("3.1", "ListCreativesRequest")
    request = request_type(filters={"accounts": [{"account_id": "acc_123"}]})

    assert isinstance(request.filters.accounts[0], AccountReferenceById)
