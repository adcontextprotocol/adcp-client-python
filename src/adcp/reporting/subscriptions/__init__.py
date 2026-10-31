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
* :func:`apply_reporting_subscriptions` -- validation and versioning of the
  ``sync_principal`` section, used by ``PrincipalService`` when it is given
  ``reporting_subscriptions=`` options.
* :class:`ReportingSubscriptionTracker` and :func:`sync_reporting_subscriptions`
  -- the buyer's client side: register, read back, refresh on
  ``principal.changed``, and compute expected periods from its own records.
* :class:`ReportingSubscriptionEngine` -- the seller runtime: host hooks,
  lost authority, exceptions, ``subscription_coverage[]`` and owed periods.
"""

from __future__ import annotations

from adcp.reporting.subscriptions._clock import COVERAGE_INPUT_DETAILS, CoverageInputError
from adcp.reporting.subscriptions.capabilities import (
    REPORTING_SUBSCRIPTIONS_FEATURE,
    REPORTING_SUBSCRIPTIONS_SECTION,
    ReportingSubscriptionSupport,
    advertise_reporting_subscriptions,
    reporting_configuration_tasks,
    subscription_capabilities,
    with_subscription_dimensions,
)
from adcp.reporting.subscriptions.consumer import (
    REPORTING_SUBSCRIPTION_CHANGED,
    REPORTING_SUBSCRIPTION_ERROR_CODES,
    ReportingSubscriptionError,
    ReportingSubscriptionReadback,
    ReportingSubscriptionTracker,
    is_subscription_change,
    load_subscription_coverage,
    read_reporting_subscriptions,
    sync_reporting_subscriptions,
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
from adcp.reporting.subscriptions.engine import (
    AccountDeliveryConfig,
    AuthorizedAccount,
    AuthorizedAccountPage,
    InMemoryReportingSubscriptionStore,
    ReportingSubscriptionDeliveryHost,
    ReportingSubscriptionEngine,
    ReportingSubscriptionHost,
    ReportingSubscriptionPrecedenceHost,
    ReportingSubscriptionStateConflictError,
    ReportingSubscriptionStateStore,
    StoredReportingSubscriptionState,
    decode_principal_state,
    encode_principal_state,
)
from adcp.reporting.subscriptions.facade import (
    PreviewResult,
    ReportingClient,
    SubscribeResult,
    guard_sync_principal,
)
from adcp.reporting.subscriptions.ledger import (
    FrozenOwedSet,
    SubscriptionObligationPlanner,
    SubscriptionPlanResult,
    subscription_obligation_id,
)
from adcp.reporting.subscriptions.registry import (
    ReportingSubscriptionValidationError,
    apply_reporting_subscriptions,
    refresh_destination_states,
    render_path_template,
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
from adcp.reporting.subscriptions.vocabulary import (
    COVERAGE_REASON_CODES,
    COVERAGE_RULE_ORDER,
    COVERAGE_VOCABULARY_VERSION,
)
from adcp.reporting.subscriptions.wire import (
    project_offering,
    project_reporting_delivery,
    project_subscription_coverage,
    project_subscription_state,
    serves_settled_subscription_fields,
)

__all__ = [
    "COVERAGE_REASON_CODES",
    "COVERAGE_RULE_ORDER",
    "COVERAGE_VOCABULARY_VERSION",
    "COVERAGE_INPUT_DETAILS",
    "CoverageInputError",
    "PreviewResult",
    "ReportingClient",
    "SubscribeResult",
    "guard_sync_principal",
    "FrozenOwedSet",
    "SubscriptionObligationPlanner",
    "SubscriptionPlanResult",
    "subscription_obligation_id",
    "REPORTING_SUBSCRIPTION_CHANGED",
    "ReportingSubscriptionReadback",
    "ReportingSubscriptionTracker",
    "is_subscription_change",
    "load_subscription_coverage",
    "read_reporting_subscriptions",
    "sync_reporting_subscriptions",
    "AccountDeliveryConfig",
    "AuthorizedAccount",
    "AuthorizedAccountPage",
    "InMemoryReportingSubscriptionStore",
    "ReportingSubscriptionDeliveryHost",
    "ReportingSubscriptionEngine",
    "REPORTING_SUBSCRIPTION_ERROR_CODES",
    "ReportingSubscriptionError",
    "ReportingSubscriptionValidationError",
    "ReportingSubscriptionHost",
    "ReportingSubscriptionPrecedenceHost",
    "ReportingSubscriptionStateConflictError",
    "ReportingSubscriptionStateStore",
    "StoredReportingSubscriptionState",
    "decode_principal_state",
    "encode_principal_state",
    "apply_reporting_subscriptions",
    "refresh_destination_states",
    "render_path_template",
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


def __getattr__(name: str) -> object:
    if name == "ReportingSubscriptionClientError":
        from adcp.reporting.subscriptions import consumer

        return getattr(consumer, name)  # warns: renamed ReportingSubscriptionError
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
