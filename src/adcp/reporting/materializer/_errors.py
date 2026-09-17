"""Do not let a driver, hook or cancellation message become a diagnostic."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Coroutine
from functools import wraps
from typing import Any, ParamSpec, TypeVar

from adcp.reporting.ledger.store import LedgerConflictError
from adcp.reporting.materializer.contracts import ReportingWriterError, ReportingWriterFailure

P = ParamSpec("P")
T = TypeVar("T")


class ReportingMaterializerUsageError(ValueError):
    """A deterministic caller-argument rejection with a fixed SDK message.

    Raised only by the SDK's own argument validators, before any store or
    destination work. It stays a ``ValueError`` so existing adopter handling is
    unchanged, and the guard re-raises it instead of reporting an unknown
    external effect that never happened.
    """


def materializer_errors(method: Callable[P, Awaitable[T]]) -> Callable[P, Coroutine[Any, Any, T]]:
    @wraps(method)
    async def guarded(*args: P.args, **kwargs: P.kwargs) -> T:
        try:
            return await method(*args, **kwargs)
        except (ReportingWriterError, LedgerConflictError, ReportingMaterializerUsageError):
            raise
        except asyncio.CancelledError:
            canceled = True
        except Exception:
            canceled = False
        # Outside the handler: neither a provider's message nor exception chain
        # survives. Commit uncertainty is always resumed with the same identity.
        if canceled:
            raise asyncio.CancelledError
        raise ReportingWriterError(
            ReportingWriterFailure("RESOURCE_UNAVAILABLE", "same_identity", "unknown")
        )

    return guarded
