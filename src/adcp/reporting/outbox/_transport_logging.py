"""Keep httpx/httpcore's URL/header diagnostics out of reporting delivery logs.

The shared filter is installed while any protected context is active. A locked
reference count keeps overlapping tasks/threads from removing it prematurely;
its decision is context-local so concurrent application HTTP traffic keeps its
normal logging behavior. The last exit restores the original filters. No
handlers, levels, or global record factory are changed.
The SDK-owned sender uses HTTP/1 and the connection logger; HTTP/2 is included
defensively and the conformance test audits the installed httpcore logger set.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from threading import Lock

_PROTECTED = ContextVar("adcp_reporting_protected_transport", default=False)
_LOGGER_NAMES = (
    "httpx",
    "httpcore.connection",
    "httpcore.http11",
    "httpcore.http2",
    "httpcore.proxy",
    "httpcore.socks",
)


class _ReportingTransportFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        return not _PROTECTED.get()


_FILTER = _ReportingTransportFilter()
_FILTER_LOCK = Lock()
_ACTIVE_CONTEXTS = 0


@contextmanager
def protected_transport_logs() -> Iterator[None]:
    global _ACTIVE_CONTEXTS
    with _FILTER_LOCK:
        if _ACTIVE_CONTEXTS == 0:
            for name in _LOGGER_NAMES:
                logging.getLogger(name).addFilter(_FILTER)
        _ACTIVE_CONTEXTS += 1
    token = _PROTECTED.set(True)
    try:
        yield
    finally:
        _PROTECTED.reset(token)
        with _FILTER_LOCK:
            _ACTIVE_CONTEXTS -= 1
            if _ACTIVE_CONTEXTS == 0:
                for name in _LOGGER_NAMES:
                    logging.getLogger(name).removeFilter(_FILTER)
