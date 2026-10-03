"""One buyer error policy for installed Core and managed reporting routes."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Literal, TypeVar

from adcp.exceptions import ADCPTaskError
from adcp.reporting.ledger.store import LedgerConflictError
from adcp.reporting.service_lifecycle import ReliableReportingUnavailableError
from adcp.server.helpers import STANDARD_ERROR_CODES, adcp_error
from adcp.types import Error

_T = TypeVar("_T")

# Reporting extensions have local meanings. Normative SDK codes always take
# precedence; unlisted extension/invariant failures retain terminal recovery.
_REPORTING_CORRECTABLE = frozenset(
    {
        "CURSOR_REVISION_MISMATCH",
        "CURSOR_SNAPSHOT_MISMATCH",
        "INVALID_CHECKPOINT",
        "INVALID_CURSOR",
        "INVALID_PAGE_SIZE",
        "INVALID_PERIOD",
        "INVALID_STATUS_PERIOD",
        "INVALID_STATUS_TIME",
        "INVALID_VIEW",
        "MISSING_REVISION_ID",
        "EMPTY_BATCH",
        "UNKNOWN_CONFIGURATION_GENERATION",
        "CONFIGURATION_GENERATION_IMMUTABLE",
        "CONFIGURATION_GENERATION_MISMATCH",
        "REPORT_DEFINITION_MISMATCH",
        "OBLIGATION_IDENTITY_MISMATCH",
        "SELLER_SNAPSHOT_EVIDENCE_INCOMPLETE",
        "STATUS_NOT_DUE",
        "STATUS_SUPERSEDES_REQUIRED",
        "STATUS_SUPERSEDES_STALE",
        "REVISION_NOT_CURRENTLY_REQUIRED",
        "REPORTING_FEED_VERSION_MISMATCH",
    }
)
_REPORTING_TRANSIENT = frozenset(
    {
        "REPORTING_CONFIGURATION_UNAVAILABLE",
        "REPORTING_STATUS_UNAVAILABLE",
        "REPORTING_CONTENT_UNAVAILABLE",
        "REPORTING_FEED_STORAGE_UNAVAILABLE",
        "RECEIPT_STORAGE_UNAVAILABLE",
    }
)


class ReliableReportingConfigurationError(ValueError):
    """A composition failure, missing caller, or invalid buyer configuration.

    Composition failures default to terminal INTERNAL_ERROR. Only explicit
    request-time failures may ask the buyer to correct authentication/input.
    """

    def __init__(
        self,
        message: str,
        *,
        kind: Literal["misconfiguration", "auth_required", "invalid_request"] = "misconfiguration",
    ) -> None:
        super().__init__(message)
        self.kind = kind


def reporting_error(code: str, message: str) -> Error:
    """Preserve the domain code and use the SDK's normative recovery defaults."""
    recovery = None
    if code not in STANDARD_ERROR_CODES:
        if code in _REPORTING_CORRECTABLE:
            recovery = "correctable"
        elif code in _REPORTING_TRANSIENT:
            recovery = "transient"
    return Error.model_validate(adcp_error(code, message, recovery=recovery)["errors"][0])


async def installed_reporting_call(
    task: str, call: Callable[[], Awaitable[_T]], *, decisioning: bool = False
) -> _T:
    """Translate at admission, including lifecycle refusals before handler entry."""
    try:
        return await call()
    except ReliableReportingUnavailableError as error:
        wire = reporting_error(
            "SERVICE_UNAVAILABLE", "Reporting service is temporarily unavailable"
        )
        cause: Exception = error
    except LedgerConflictError as error:
        wire = reporting_error(error.code, str(error))
        cause = error
    except ReliableReportingConfigurationError as error:
        code, message = {
            "misconfiguration": ("INTERNAL_ERROR", "Reporting service configuration failed"),
            "auth_required": ("AUTH_REQUIRED", "Authenticated reporting caller is required"),
            "invalid_request": ("INVALID_REQUEST", str(error)),
        }[error.kind]
        wire = reporting_error(code, message)
        cause = error
    except ADCPTaskError as error:
        # Existing production routes already redact ACL/storage errors. Apply
        # the same code policy without dropping their structured metadata.
        errors = [
            (
                Error.model_validate(
                    {
                        **item.model_dump(exclude_none=True),
                        "recovery": reporting_error(item.code, item.message).recovery,
                    }
                )
                if isinstance(item, Error)
                else item
            )
            for item in error.errors
        ]
        if not decisioning:
            raise ADCPTaskError(operation=task, errors=errors) from error
        wire = errors[0]
        cause = error
    if decisioning:
        from adcp.decisioning import AdcpError

        assert wire.recovery is not None
        raise AdcpError(wire.code, message=wire.message, recovery=wire.recovery.value) from cause
    raise ADCPTaskError(operation=task, errors=[wire]) from cause
