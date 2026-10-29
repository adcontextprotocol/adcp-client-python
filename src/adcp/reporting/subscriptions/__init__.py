"""AdCP 3.3 principal-level reporting subscriptions (experimental).

``media_buy.reporting_subscriptions`` lets a caller register reporting once on
its principal through ``sync_principal`` instead of once per account through
``sync_accounts``. This package holds the parts both sides share:

* :func:`expected_subscription_periods` -- the pure coverage rule. A buyer runs
  it from its own records to learn exactly which periods it is owed; the
  seller runtime runs it over the same facts to decide which obligations it
  owes.
* :func:`resolve_subscription_offerings` -- deterministic selector resolution
  (``exact`` / ``finest`` / ``all``, ties to the lowest ``offering_id``).
* :func:`subscription_capabilities` -- the capability fields a seller that
  serves subscriptions must advertise.
"""

from __future__ import annotations

from adcp.reporting.subscriptions.capabilities import (
    REPORTING_SUBSCRIPTIONS_FEATURE,
    REPORTING_SUBSCRIPTIONS_SECTION,
    ReportingSubscriptionSupport,
    advertise_reporting_subscriptions,
    reporting_configuration_tasks,
    subscription_capabilities,
    with_subscription_dimensions,
)
from adcp.reporting.subscriptions.coverage import (
    AccountTimezoneChange,
    ExpectedSubscriptionPeriod,
    SubscriptionAuthorization,
    SubscriptionBuy,
    SubscriptionBuyFlight,
    SubscriptionBuyPackage,
    SubscriptionException,
    SubscriptionVersionWindow,
    expected_subscription_periods,
    subscription_covers_account,
    subscription_exceptions,
    subscription_version_windows,
)
from adcp.reporting.subscriptions.resolution import (
    STANDARD_REPORTING_DIMENSIONS,
    SubscriptionDestination,
    SubscriptionMatch,
    SubscriptionOffering,
    SubscriptionResolution,
    SubscriptionSelector,
    resolve_subscription_offerings,
)
from adcp.reporting.subscriptions.wire import (
    project_offering,
    project_reporting_delivery,
    project_subscription_coverage,
    project_subscription_state,
    serves_settled_subscription_fields,
)

__all__ = [
    "REPORTING_SUBSCRIPTIONS_FEATURE",
    "REPORTING_SUBSCRIPTIONS_SECTION",
    "STANDARD_REPORTING_DIMENSIONS",
    "AccountTimezoneChange",
    "ExpectedSubscriptionPeriod",
    "ReportingSubscriptionSupport",
    "SubscriptionAuthorization",
    "SubscriptionBuy",
    "SubscriptionBuyFlight",
    "SubscriptionBuyPackage",
    "SubscriptionDestination",
    "SubscriptionException",
    "SubscriptionMatch",
    "SubscriptionOffering",
    "SubscriptionResolution",
    "SubscriptionSelector",
    "SubscriptionVersionWindow",
    "advertise_reporting_subscriptions",
    "expected_subscription_periods",
    "reporting_configuration_tasks",
    "resolve_subscription_offerings",
    "subscription_capabilities",
    "subscription_covers_account",
    "subscription_exceptions",
    "subscription_version_windows",
    "with_subscription_dimensions",
    "project_offering",
    "project_reporting_delivery",
    "project_subscription_coverage",
    "project_subscription_state",
    "serves_settled_subscription_fields",
]
