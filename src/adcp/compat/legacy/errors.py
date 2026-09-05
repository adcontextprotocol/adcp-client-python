"""Error policy for transport-facing legacy adapter failures."""

from __future__ import annotations

import logging
from typing import Literal

from adcp.exceptions import ADCPTaskError
from adcp.types import Error

logger = logging.getLogger(__name__)

_REQUEST_FAILURE_MESSAGE = "The legacy AdCP request could not be translated."
_RESPONSE_FAILURE_MESSAGE = "The response could not be translated to the legacy AdCP format."


class LegacyAdapterValidationError(ValueError):
    """Buyer-correctable request error whose message is safe for the wire.

    Legacy request adapters may raise this exception when the failure is caused
    by caller input and the exception message was deliberately written for the
    buyer. Arbitrary exceptions, including ordinary ``ValueError`` instances,
    are treated as private implementation failures.
    """


def legacy_adapter_task_error(
    *,
    operation: str,
    wire_version: str | None,
    phase: Literal["request", "response"],
    exception: Exception,
) -> ADCPTaskError:
    """Map an adapter exception to the shared, sanitized protocol error.

    Only the explicit request-validation exception is public. All unexpected
    failures are logged with their traceback and replaced by fixed wire text.
    Response normalization is seller-side work, so it never exposes exception
    text as a buyer-correctable error.
    """

    if phase == "request" and isinstance(exception, LegacyAdapterValidationError):
        error = Error(code="INVALID_REQUEST", message=str(exception))
    else:
        logger.error(
            "Legacy %s adapter failed for %r at AdCP %s",
            phase,
            operation,
            wire_version,
            exc_info=(type(exception), exception, exception.__traceback__),
        )
        error = Error(
            code="INVALID_REQUEST" if phase == "request" else "INTERNAL_ERROR",
            message=(_REQUEST_FAILURE_MESSAGE if phase == "request" else _RESPONSE_FAILURE_MESSAGE),
        )

    return ADCPTaskError(operation=operation, errors=[error])


__all__ = ["LegacyAdapterValidationError"]
