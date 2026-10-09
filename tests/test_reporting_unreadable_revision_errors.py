"""Buyer-facing errors for exact revision reads (adcontextprotocol/adcp#8085 and #8099).

An unknown, unauthorized or retired revision answers the standard, nondisclosing
``REFERENCE_NOT_FOUND``. A retained revision whose rows are missing or fail their binding
answers ``SERVICE_UNAVAILABLE``: never not-found, and never rows that fail the binding.
"""

from __future__ import annotations

import pytest

from adcp.exceptions import ADCPTaskError
from adcp.reporting._errors import installed_reporting_call, reporting_error
from adcp.reporting.ledger.row_storage_errors import ReportingRowStoreError
from adcp.reporting.ledger.store import LedgerConflictError


async def failing(error: Exception) -> None:
    raise error


@pytest.mark.parametrize(
    "code", ["ROWS_UNAVAILABLE", "ROWS_INTEGRITY_FAILED", "PROVIDER_UNAVAILABLE"]
)
async def test_unreadable_rows_are_service_unavailable_and_secret_free(code: str) -> None:
    error = ReportingRowStoreError(code, "gs://secret-bucket/object token=SECRET")  # type: ignore[arg-type]
    with pytest.raises(ADCPTaskError) as raised:
        await installed_reporting_call("get_media_buy_delivery", lambda: failing(error))
    wire = raised.value.errors[0]
    assert wire.code == "SERVICE_UNAVAILABLE"
    assert "SECRET" not in wire.message and "secret" not in wire.message
    assert wire.recovery.value == "transient"


@pytest.mark.parametrize("code", ["REVISION_NOT_FOUND", "REFERENCE_NOT_FOUND"])
async def test_unknown_and_retired_revisions_are_the_standard_reference_error(code: str) -> None:
    with pytest.raises(ADCPTaskError) as raised:
        await installed_reporting_call(
            "get_media_buy_delivery",
            lambda: failing(LedgerConflictError(code, "no such revision for this account")),
        )
    assert raised.value.errors[0].code == "REFERENCE_NOT_FOUND"


def test_the_standard_reference_error_keeps_its_normative_recovery() -> None:
    assert reporting_error("REVISION_NOT_FOUND", "x").code == "REFERENCE_NOT_FOUND"
