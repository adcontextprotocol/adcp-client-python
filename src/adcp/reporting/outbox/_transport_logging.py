"""Keep httpx/httpcore's URL/header diagnostics out of reporting delivery logs.

Filters are context-local: concurrent application HTTP traffic keeps its normal
logging configuration. No handlers, levels, or global record factory are changed.
The SDK-owned sender uses HTTP/1 and the connection logger; HTTP/2 is included
defensively and the conformance test audits the installed httpcore logger set.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar

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


@contextmanager
def protected_transport_logs() -> Iterator[None]:
    for name in _LOGGER_NAMES:
        logging.getLogger(name).addFilter(_FILTER)
    token = _PROTECTED.set(True)
    try:
        yield
    finally:
        _PROTECTED.reset(token)
