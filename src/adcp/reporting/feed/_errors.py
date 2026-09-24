"""The same closed, redacted storage boundary on both implementations."""

from __future__ import annotations

from collections.abc import Callable, Coroutine
from functools import wraps
from typing import Any, ParamSpec, TypeVar

from adcp.reporting.feed.errors import ReportingFeedError
from adcp.reporting.ledger.notification_models import ReportingNotificationError
from adcp.reporting.ledger.store import LedgerConflictError
from adcp.reporting.receipts.errors import ReportingReceiptError

_P = ParamSpec("_P")
_R = TypeVar("_R")


def storage_errors(
    fn: Callable[_P, Coroutine[Any, Any, _R]],
) -> Callable[_P, Coroutine[Any, Any, _R]]:
    @wraps(fn)
    async def wrapped(*args: _P.args, **kwargs: _P.kwargs) -> _R:
        try:
            return await fn(*args, **kwargs)
        except ReportingFeedError:
            raise
        except (LedgerConflictError, ReportingNotificationError, ReportingReceiptError):
            error = ReportingFeedError("REPORTING_FEED_HISTORY_CORRUPT")
        except Exception:
            error = ReportingFeedError("REPORTING_FEED_STORAGE_UNAVAILABLE")
        # Outside the exception scope: provider bodies and identifiers must not
        # survive in an implicit exception chain, repr, or transport error.
        raise error

    return wrapped
