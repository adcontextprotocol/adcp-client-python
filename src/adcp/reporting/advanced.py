"""Reporting-subscription building blocks for hand-wired compositions (experimental).

Rung 3 of the reporting-subscriptions design: most sellers enable subscriptions
with ``ReliableReportingService(subscriptions=SubscriptionOptions(...))`` and
never touch these. Use them to bring your own ledger, build the engine and link
the principal runtime yourself
(``ReliableReportingService(reporting_subscriptions=ReportingSubscriptionServiceOptions(...))``),
or compute coverage directly. The TypeScript SDK publishes the same building
blocks from ``@adcp/sdk/reporting/advanced``.

Everything here is experimental and may change between betas. The names are
defined in :mod:`adcp.reporting.subscriptions` and
:mod:`adcp.reporting.service_subscriptions`, which keep exporting them.
"""

from __future__ import annotations

from adcp.reporting.service_subscriptions import (
    ManagedGenerationBinder,
    PlanningErrorHandler,
    PrincipalLister,
    PrincipalResolver,
    ProductionSubscriptionHooks,
    ReportingSubscriptionServiceOptions,
    SubscriptionPlanningFailure,
    assert_reporting_task_granted,
)
from adcp.reporting.subscriptions._clock import COVERAGE_INPUT_DETAILS, CoverageInputError
from adcp.reporting.subscriptions.capabilities import (
    ReportingSubscriptionSupport,
    advertise_reporting_subscriptions,
    reporting_configuration_tasks,
    subscription_capabilities,
    with_subscription_dimensions,
)
from adcp.reporting.subscriptions.coverage import (
    DEFAULT_MAX_ITERATIONS,
    DEFAULT_MAX_PERIODS,
    AccountTimezoneChange,
    ExpectedSubscriptionPeriod,
    OwedSubscriptionPeriod,
    SubscriptionAuthorization,
    SubscriptionBuy,
    SubscriptionBuyFlight,
    SubscriptionBuyPackage,
    SubscriptionException,
    SubscriptionVersionWindow,
    authorization_chain_start,
    authorization_regranted_at,
    expected_subscription_periods,
    owed_subscription_periods,
    subscription_covers_account,
    subscription_exceptions,
    subscription_version_windows,
)
from adcp.reporting.subscriptions.engine import (
    ENGINE_ERROR_CODES,
    AccountConfigsEnder,
    AccountDeliveryConfig,
    AuthorizedAccount,
    AuthorizedAccountPage,
    GrantObservation,
    InMemoryReportingSubscriptionStore,
    ReportingSubscriptionDeliveryHost,
    ReportingSubscriptionEngine,
    ReportingSubscriptionEngineError,
    ReportingSubscriptionHost,
    ReportingSubscriptionPrecedenceHost,
    ReportingSubscriptionStateConflictError,
    ReportingSubscriptionStateCorruptError,
    ReportingSubscriptionStateStore,
    StoredReportingSubscriptionState,
    decode_principal_state,
    encode_principal_state,
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
    resolve_reporting_subscription_package,
    resolve_subscription_offerings,
)

__all__ = [
    "assert_reporting_task_granted",
    # Engine
    "ENGINE_ERROR_CODES",
    "AccountConfigsEnder",
    "AccountDeliveryConfig",
    "AuthorizedAccount",
    "AuthorizedAccountPage",
    "FrozenOwedSet",
    "GrantObservation",
    "ReportingSubscriptionDeliveryHost",
    "ReportingSubscriptionEngine",
    "ReportingSubscriptionEngineError",
    "ReportingSubscriptionHost",
    "ReportingSubscriptionPrecedenceHost",
    "ReportingSubscriptionStateConflictError",
    "ReportingSubscriptionStateCorruptError",
    # Engine stores
    "InMemoryReportingSubscriptionStore",
    "ReportingSubscriptionStateStore",
    "StoredReportingSubscriptionState",
    "decode_principal_state",
    "encode_principal_state",
    # Coverage function
    "DEFAULT_MAX_ITERATIONS",
    "DEFAULT_MAX_PERIODS",
    "AccountTimezoneChange",
    "ExpectedSubscriptionPeriod",
    "OwedSubscriptionPeriod",
    "SubscriptionAuthorization",
    "SubscriptionBuy",
    "SubscriptionBuyFlight",
    "SubscriptionBuyPackage",
    "SubscriptionException",
    "SubscriptionVersionWindow",
    "authorization_chain_start",
    "authorization_regranted_at",
    "expected_subscription_periods",
    "owed_subscription_periods",
    "subscription_covers_account",
    "subscription_exceptions",
    "subscription_version_windows",
    # Calendar and selection
    "COVERAGE_INPUT_DETAILS",
    "CoverageInputError",
    "STANDARD_REPORTING_DIMENSIONS",
    "SubscriptionDestination",
    "SubscriptionMatch",
    "SubscriptionOffering",
    "SubscriptionResolution",
    "SubscriptionSelector",
    "resolve_reporting_subscription_package",
    "resolve_subscription_offerings",
    # Capability builder
    "ReportingSubscriptionSupport",
    "advertise_reporting_subscriptions",
    "reporting_configuration_tasks",
    "subscription_capabilities",
    "with_subscription_dimensions",
    # Principal section validation
    "ReportingSubscriptionValidationError",
    "apply_reporting_subscriptions",
    "refresh_destination_states",
    "render_path_template",
    # Service planner hooks
    "ManagedGenerationBinder",
    "PlanningErrorHandler",
    "PrincipalLister",
    "PrincipalResolver",
    "ProductionSubscriptionHooks",
    "ReportingSubscriptionServiceOptions",
    "SubscriptionObligationPlanner",
    "SubscriptionPlanResult",
    "SubscriptionPlanningFailure",
    "subscription_obligation_id",
]
