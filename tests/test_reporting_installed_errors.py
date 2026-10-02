"""Installed reporting preserves buyer recovery semantics at service admission."""

from unittest.mock import AsyncMock

import pytest

from adcp.decisioning import AdcpError
from adcp.exceptions import ADCPTaskError
from adcp.reporting import ReliableReportingConfigurationError, ReliableReportingService
from adcp.reporting.ledger import LedgerConflictError
from adcp.reporting.service_lifecycle import (
    ReliableReportingState,
    ReliableReportingUnavailableError,
)
from adcp.server.base import ADCPHandler
from adcp.server.helpers import STANDARD_ERROR_CODES
from tests.test_reliable_reporting_decisioning import _SalesPlatform
from tests.test_reliable_reporting_service import _account_context


@pytest.mark.parametrize("decisioning", [False, True])
@pytest.mark.parametrize(
    "method,service_method",
    [
        ("get_reporting_status", "get_reporting_status"),
        ("sync_reporting_status", "sync_reporting_status"),
        ("sync_reporting_receipts", "sync_reporting_receipts"),
        ("_get_reporting_revision_content", "get_revision_content"),
    ],
)
@pytest.mark.parametrize(
    "failure,code",
    [
        (ReliableReportingUnavailableError(ReliableReportingState.NEW), "SERVICE_UNAVAILABLE"),
        (ReliableReportingConfigurationError("internal provider detail"), "INTERNAL_ERROR"),
        (
            ReliableReportingConfigurationError("missing caller", kind="auth_required"),
            "AUTH_REQUIRED",
        ),
        (
            ReliableReportingConfigurationError("invalid scope", kind="invalid_request"),
            "INVALID_REQUEST",
        ),
        (LedgerConflictError("RATE_LIMITED", "slow down"), "RATE_LIMITED"),
        (LedgerConflictError("UNAUTHORIZED", "denied"), "UNAUTHORIZED"),
        (LedgerConflictError("INVALID_REQUEST", "bad input"), "INVALID_REQUEST"),
        (LedgerConflictError("UNKNOWN_LEDGER_CODE", "domain detail"), "UNKNOWN_LEDGER_CODE"),
    ],
)
async def test_all_installed_core_routes_translate_by_actual_code(
    decisioning, method, service_method, failure, code
):
    service = ReliableReportingService.memory(
        account_context=_account_context,
        consumer_status_enabled=True,
        receipt_handler=AsyncMock(),
    )
    setattr(service, service_method, AsyncMock(side_effect=failure))
    platform = service.install(_SalesPlatform() if decisioning else ADCPHandler())
    with pytest.raises(AdcpError if decisioning else ADCPTaskError) as caught:
        await getattr(platform, method)({})
    error = caught.value
    if decisioning:
        assert error.code == code
        recovery = error.recovery
    else:
        assert error.error_codes == [code]
        recovery = error.error_info[0].recovery
    assert recovery == STANDARD_ERROR_CODES.get(code, {}).get("recovery", "terminal")
    if code == "INTERNAL_ERROR":
        assert "internal provider detail" not in str(error)
    await service.close()


async def test_disabled_optional_routes_and_aggregate_delivery_delegate():
    class Application(ADCPHandler):
        async def sync_reporting_status(self, params, context=None):
            return {"fallback": "status"}

        async def sync_reporting_receipts(self, params, context=None):
            return {"fallback": "receipts"}

        async def get_media_buy_delivery(self, params, context=None):
            return {"fallback": "delivery"}

    service = ReliableReportingService.memory(account_context=_account_context)
    mounted = service.install(Application())
    assert await mounted.sync_reporting_status({}) == {"fallback": "status"}
    assert await mounted.sync_reporting_receipts({}) == {"fallback": "receipts"}
    assert await mounted.get_media_buy_delivery({}) == {"fallback": "delivery"}
    await service.close()


@pytest.mark.parametrize(
    "code,recovery",
    [
        ("CURSOR_REVISION_MISMATCH", "correctable"),
        ("INVALID_PAGE_SIZE", "correctable"),
        ("CONFIGURATION_GENERATION_IMMUTABLE", "correctable"),
        ("HISTORY_UNAVAILABLE", "terminal"),
        ("REVISION_CONTENT_MISMATCH", "terminal"),
        ("REPORTING_STATUS_UNAVAILABLE", "transient"),
        ("RECEIPT_STORAGE_UNAVAILABLE", "transient"),
    ],
)
async def test_reporting_extension_codes_retain_their_meaning(code, recovery):
    service = ReliableReportingService.memory(account_context=_account_context)
    service.get_reporting_status = AsyncMock(side_effect=LedgerConflictError(code, "detail"))
    handler = service.install(_SalesPlatform())
    with pytest.raises(AdcpError) as caught:
        await handler.get_reporting_status({})
    assert caught.value.code == code
    assert caught.value.recovery == recovery
    await service.close()
