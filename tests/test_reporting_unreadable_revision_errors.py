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


# -- the handler's own buyer-facing mapping -------------------------------------------------


def _handler_harness(store: object) -> tuple[object, object, dict[str, object]]:
    """The exact-revision branch of ``get_media_buy_delivery`` over a stub store.

    ``__wrapped__`` skips the service-lifecycle admission, which is not under test.
    """
    from types import SimpleNamespace

    from adcp.reporting.production.handler import ReportingProductionHandler

    caller = SimpleNamespace(account_id="acct_a", consumer_id="consumer_a")

    class Stub:
        production = SimpleNamespace(store=store)

        async def _authorize(self, request: object, context: object) -> object:
            return caller

        async def get_reporting_status(self, request: object, context: object) -> object:
            return {"revision": {"period": {"start": "2026-01-01", "end": "2026-02-01"}}}

    request = {"account": {"account_id": "acct_a"}, "reporting_revision_id": "rev_1"}
    return ReportingProductionHandler.get_media_buy_delivery.__wrapped__, Stub(), request  # type: ignore[attr-defined]


def _revision(*, readable: bool) -> object:
    from types import SimpleNamespace

    return SimpleNamespace(
        readable=readable,
        revision_content_sha256="0" * 64,
        row_count=1,
        managed_control_totals=None,
        control_totals=(),
    )


class _Store:
    def __init__(self, revision: object, read_error: Exception | None = None) -> None:
        self.revision = revision
        self.read_error = read_error

    async def get_revision(self, **_: object) -> object:
        return self.revision

    async def read_revision_rows(self, **_: object) -> object:
        raise self.read_error  # type: ignore[misc]


async def _call(store: object) -> ADCPTaskError:
    method, stub, request = _handler_harness(store)
    with pytest.raises(ADCPTaskError) as raised:
        await method(stub, request, object())  # type: ignore[operator]
    return raised.value


async def test_the_handler_answers_an_owned_but_unreadable_revision_with_service_unavailable() -> (
    None
):
    error = await _call(_Store(_revision(readable=False)))
    assert error.errors[0].code == "SERVICE_UNAVAILABLE"


@pytest.mark.parametrize("code", ["ROWS_UNAVAILABLE", "ROWS_INTEGRITY_FAILED", "ROWS_EXPIRED"])
async def test_the_handler_never_turns_a_row_store_failure_into_not_found(code: str) -> None:
    failure = ReportingRowStoreError(code, "s3://bucket/key")  # type: ignore[arg-type]
    error = await _call(_Store(_revision(readable=True), read_error=failure))
    assert error.errors[0].code == "SERVICE_UNAVAILABLE"
    assert "bucket" not in error.errors[0].message


async def test_the_handler_answers_an_absent_revision_with_the_standard_reference_error() -> None:
    error = await _call(_Store(None))
    assert error.errors[0].code == "REFERENCE_NOT_FOUND"


async def test_the_handler_maps_unavailable_content_to_service_unavailable() -> None:
    from adcp.reporting.outbox.support import ReportingNotificationError

    failure = ReportingNotificationError("reporting_revision_content_unavailable")
    error = await _call(_Store(_revision(readable=True), read_error=failure))
    assert error.errors[0].code == "SERVICE_UNAVAILABLE"
