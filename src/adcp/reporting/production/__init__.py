"""Production reporting composition with explicit, frozen provider contracts.

Mount one support's handler, start its owned lifecycle, then activate accounts
after migration and drain. The memory implementation is for conformance and
does not claim durability. The PostgreSQL production store is
``adcp_reporting_storage.production.pg``; both implement
:class:`~adcp.reporting.production.store.ReportingProductionStore`.
"""

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
from adcp.reporting.production.store import (
    ReportingProductionProjection,
    ReportingProductionStore,
)

__all__ = [
    "ConfigurationAdmission",
    "ConfigurationTask",
    "InMemoryReportingProductionOutbox",
    "InMemoryReportingProductionStore",
    "ReportingConfigurationAdmission",
    "ReportingProductionConfigurationTask",
    "ReportingProductionDestination",
    "ReportingProductionDestinationBinding",
    "ReportingProductionHandler",
    "ReportingProductionMethod",
    "ReportingProductionOffering",
    "ReportingProductionProjection",
    "ReportingProductionSigning",
    "ReportingProductionSource",
    "ReportingProductionSourceBinding",
    "ReportingProductionSourceRegistry",
    "ReportingProductionStore",
    "ReportingProductionSupport",
    "production_notification_workers",
]
