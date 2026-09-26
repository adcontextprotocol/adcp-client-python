"""Production reporting composition with explicit, frozen provider contracts.

Mount one support's handler, start its owned lifecycle, then activate accounts
after migration and drain. The memory implementation is for conformance and
does not claim durability. PostgreSQL uses the shared optional driver guard.
"""

from typing import TYPE_CHECKING, Any

from adcp.reporting.production.configuration import (
    ConfigurationAdmission,
    ConfigurationTask,
    ReportingConfigurationAdmission,
    ReportingProductionConfigurationTask,
)
from adcp.reporting.production.contracts import (
    ReportingProductionDestinationBinding,
    ReportingProductionMethod,
    ReportingProductionSource,
    ReportingProductionSourceBinding,
)
from adcp.reporting.production.handler import ReportingProductionHandler
from adcp.reporting.production.memory import (
    InMemoryReportingProductionOutbox,
    InMemoryReportingProductionStore,
)
from adcp.reporting.production.notifications import (
    ReportingProductionSigning,
    production_notification_workers,
)
from adcp.reporting.production.offerings import ReportingProductionOffering
from adcp.reporting.production.service import (
    ReportingProductionDestination,
    ReportingProductionSupport,
)
from adcp.reporting.production.source_registry import ReportingProductionSourceRegistry

if TYPE_CHECKING:
    from adcp.reporting.production.pg import PgReportingProductionOutbox, PgReportingProductionStore

__all__ = [
    "ConfigurationAdmission",
    "ConfigurationTask",
    "InMemoryReportingProductionOutbox",
    "InMemoryReportingProductionStore",
    "PgReportingProductionOutbox",
    "PgReportingProductionStore",
    "ReportingConfigurationAdmission",
    "ReportingProductionConfigurationTask",
    "ReportingProductionDestination",
    "ReportingProductionDestinationBinding",
    "ReportingProductionHandler",
    "ReportingProductionMethod",
    "ReportingProductionOffering",
    "ReportingProductionSigning",
    "ReportingProductionSource",
    "ReportingProductionSourceBinding",
    "ReportingProductionSourceRegistry",
    "ReportingProductionSupport",
    "production_notification_workers",
]


def __getattr__(name: str) -> Any:
    if name in {"PgReportingProductionOutbox", "PgReportingProductionStore"}:
        from adcp.reporting.production import pg

        return getattr(pg, name)
    raise AttributeError(name)
