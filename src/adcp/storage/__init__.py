"""One storage decision for reporting (AdCP 3.3, experimental).

``memory_storage()`` or ``postgres_storage(pool=... | dsn=...)`` returns a
:class:`ReportingStorage` that holds every reporting store: the ledger, staged
source objects and seals, ``sync_principal`` principal records and the
reporting-subscription engine state (grant journal, exceptions, version
snapshots). Pass it to ``ReliableReportingService(storage=...)``.

* **Durability is a property of the storage**, never a flag:
  ``memory_storage()`` is ``process-local`` and ``postgres_storage`` is
  ``durable``.
* **Process-local storage is refused in production** for reporting
  subscriptions, with ``REPORTING_STORAGE_NOT_DURABLE``. It is allowed under
  pytest and when ``ADCP_ENV`` is ``development`` or ``test``.
* **Migrations are versioned files**, applied by ``python -m adcp migrate``;
  durable storage refuses to start on a schema that is behind
  (``REPORTING_SCHEMA_BEHIND``).

``postgres_storage`` ships in the ``adcp-reporting-storage`` distribution and
is re-exported here and from :mod:`adcp.storage.postgres`.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal

__all__ = [
    "DEVELOPMENT_ENVIRONMENTS",
    "ReportingStorage",
    "development_environment",
    "memory_storage",
    "postgres_storage",
    "require_durable",
]

#: ``ADCP_ENV`` values under which process-local storage is allowed.
DEVELOPMENT_ENVIRONMENTS = frozenset({"development", "test"})


@dataclass(frozen=True)
class ReportingStorage:
    """Every reporting store, chosen together.

    Built by :func:`memory_storage` or :func:`postgres_storage`. ``ledger``
    is the reporting ledger store, ``principals`` the principal-record store,
    ``subscriptions`` the reporting-subscription engine-state store, and
    ``staging``/``seals`` the default source stores adapters register with.
    ``check_schema`` runs at startup and refuses a schema that is behind;
    ``close`` releases what the storage opened.
    """

    durability: Literal["process-local", "durable"]
    ledger: Any
    principals: Any
    subscriptions: Any
    staging: Any = None
    seals: Any = None
    check_schema: Callable[[], Awaitable[None]] | None = None
    close: Callable[[], Awaitable[None]] | None = None
    pool: Any = None

    def __post_init__(self) -> None:
        if self.durability not in {"process-local", "durable"}:
            raise ValueError("durability is process-local or durable")

    @property
    def durable(self) -> bool:
        return self.durability == "durable"


def memory_storage(*, clock: Callable[[], datetime] | None = None) -> ReportingStorage:
    """Process-local storage for tests and development; lost on restart."""
    from adcp.reporting.inline_source import InMemorySealStore, InMemoryStagingStore
    from adcp.reporting.ledger import InMemoryReportingLedgerStore
    from adcp.reporting.subscriptions.engine import InMemoryReportingSubscriptionStore
    from adcp.server.principal import InMemoryPrincipalRecordStore

    return ReportingStorage(
        durability="process-local",
        ledger=InMemoryReportingLedgerStore(clock=clock),
        principals=InMemoryPrincipalRecordStore(),
        subscriptions=InMemoryReportingSubscriptionStore(),
        staging=InMemoryStagingStore(),
        seals=InMemorySealStore(),
    )


def postgres_storage(
    *,
    pool: Any = None,
    dsn: str | None = None,
    clock: Callable[[], datetime] | None = None,
    max_size: int = 10,
) -> ReportingStorage:
    """Durable storage over PostgreSQL, from the ``adcp-reporting-storage`` distribution.

    Resolved through the distribution's ``adcp.storage`` entry point, so
    ``adcp`` never imports it by name.
    """
    build = _plugin("adcp.storage", "postgres")
    storage = build(pool=pool, dsn=dsn, clock=clock, max_size=max_size)
    if not isinstance(storage, ReportingStorage):
        raise TypeError("the adcp.storage postgres entry point must return ReportingStorage")
    return storage


def _plugin(group: str, name: str) -> Any:
    """Load an entry point a companion distribution registers."""
    from importlib.metadata import entry_points

    for entry in entry_points(group=group):
        if entry.name == name:
            return entry.load()
    raise ImportError(
        f"{group}:{name} ships in adcp-reporting-storage: pip install 'adcp-reporting-storage[pg]'"
    )


def development_environment() -> bool:
    """Whether process-local storage is allowed: pytest, or ``ADCP_ENV`` development/test."""
    if os.environ.get("ADCP_ENV", "").strip().lower() in DEVELOPMENT_ENVIRONMENTS:
        return True
    return "pytest" in sys.modules or "PYTEST_CURRENT_TEST" in os.environ


def require_durable(durable: bool, *, stores: str) -> None:
    """Refuse process-local reporting-subscription storage outside development."""
    if durable or development_environment():
        return
    from adcp.reporting._errors import ReliableReportingConfigurationError

    raise ReliableReportingConfigurationError(
        f"reporting subscriptions keep obligations, grants and frozen coverage in {stores}, "
        "which do not survive a restart. Pass storage=postgres_storage(dsn=...) to "
        "ReliableReportingService, or set ADCP_ENV=development to run in memory.",
        code="REPORTING_STORAGE_NOT_DURABLE",
        config_field="storage",
    )
