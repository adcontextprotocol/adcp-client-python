"""Reporting subscriptions in :class:`~adcp.reporting.service.ReliableReportingService`.

Most sellers opt in with ``ReliableReportingService(subscriptions=SubscriptionOptions(...))``,
the managed path (:mod:`adcp.reporting.subscriptions.managed`), which builds
everything below from the platform the service is installed on.
``ReliableReportingService(reporting_subscriptions=ReportingSubscriptionServiceOptions(...))``
is the advanced composition, where the host builds the engine and links the
principal runtime itself. Without either, capabilities, responses and worker
turns are exactly what they were.

With it, the service:

* **plans** every listed principal's frozen owed sets at the start of each
  worker turn, through :class:`~adcp.reporting.subscriptions.SubscriptionObligationPlanner`,
  and then executes them with the account generations. Each (version,
  offering) generation is installed through the same trusted path as
  :meth:`~adcp.reporting.service.ReliableReportingService.configure`: the
  ``account_context`` resolver receives the generation (its ``subscription``
  is set) and returns its source route, currency and advertised offering, and
  the service validates them as for an account configuration. The horizon is
  ``min(engine through, turn clock)``. One subscription's failure never stops
  another's or an account configuration's; it is recorded on the turn and
  reported to ``planning_error_handler`` (or the worker error handler).
* **advertises** ``configuration_tasks``, the ``subscriptions`` block,
  ``standard_dimensions`` on every offering, the
  ``media_buy.reporting_subscriptions`` feature and the principal section, at
  the release each caller is served. Callers served below 3.3 see the
  pre-3.3 block.
* **adds** ``subscription_coverage[]`` to completed ``summary`` and
  ``periods`` views of ``get_reporting_status``.

Ownership: a subscription generation belongs to its principal, whose
``subject`` is the generation's ``consumer_id``. Ledger reads and status
ingest are scoped by the resolved caller's ``consumer_id``, so the caller
resolver must yield the principal's subject; ``resolve_principal`` is checked
against it.
"""

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any

from adcp._principal_ref import PrincipalRef
from adcp.reporting._errors import ReliableReportingConfigurationError
from adcp.reporting.ledger.models import ReportingConfiguration, ReportingDefinitionBinding
from adcp.reporting.subscriptions.managed import SubscriptionOptions
from adcp.reporting.subscriptions.wire import serves_subscriptions

if TYPE_CHECKING:
    from adcp.reporting.ledger.delivery_models import (
        ReportingDeliveryPrincipal,
        ReportingDestinationBinding,
    )
    from adcp.reporting.subscriptions.engine import ReportingSubscriptionEngine
    from adcp.server.principal import PrincipalIdentity

#: Every principal whose subscription obligations the service writes.
PrincipalLister = Callable[
    [],
    "Sequence[PrincipalIdentity | PrincipalRef] | Awaitable[Sequence[PrincipalIdentity | PrincipalRef]]",  # noqa: E501
]
#: The authenticated principal of a request; never derived from request content.
PrincipalResolver = Callable[
    [Any, Any],
    "PrincipalIdentity | PrincipalRef | Awaitable[PrincipalIdentity | PrincipalRef]",
]


@dataclass(frozen=True)
class SubscriptionPlanningFailure:
    """One subscription (or one principal's listing or reconcile) a turn could not plan."""

    principal: PrincipalIdentity | PrincipalRef
    error: Exception
    account_id: str | None = None
    subscription_id: str | None = None

    @property
    def key(self) -> tuple[str, str | None, str | None]:
        return (self.principal.subject, self.account_id, self.subscription_id)


PlanningErrorHandler = Callable[[SubscriptionPlanningFailure], "Awaitable[None] | None"]
#: Binds a reporting subscription's generation to its managed destination
#: (production): called with the generation and its version's configuration,
#: before the generation's first obligation is written. It resolves the
#: version's ``destination.destination_id`` to the owner's current destination
#: generation, authorizes it for the account and returns the binding, with the
#: version's ``path_template``. A failure writes nothing; the next turn retries.
ManagedGenerationBinder = Callable[
    [ReportingConfiguration, Mapping[str, Any]],
    "Awaitable[ReportingDestinationBinding] | ReportingDestinationBinding",
]


@dataclass(frozen=True)
class ReportingSubscriptionServiceOptions:
    """Serve principal-level reporting subscriptions from the reliable-reporting service.

    ``engine`` is the :class:`~adcp.reporting.subscriptions.ReportingSubscriptionEngine`
    linked to the seller's ``PrincipalService``; the host keeps it reconciled
    (``reconcile`` on ``principal.changed`` and authorization changes).
    ``list_principals`` names every principal whose obligations each turn
    writes. ``resolve_principal`` is the same authenticated mapping the
    principal runtime uses. ``definitions`` binds offering ids to their
    content-addressed report definitions; a seller advertising
    ``reporting.core`` supplies them, as it does on account configurations.
    """

    engine: ReportingSubscriptionEngine
    list_principals: PrincipalLister
    resolve_principal: PrincipalResolver
    planning_error_handler: PlanningErrorHandler | None = None
    definitions: Mapping[str, ReportingDefinitionBinding] | None = None
    bind_managed_generation: ManagedGenerationBinder | None = None

    def __post_init__(self) -> None:
        for name in ("list_principals", "resolve_principal"):
            if not callable(getattr(self, name)):
                raise TypeError(f"reporting_subscriptions requires {name}")
        if not hasattr(self.engine, "frozen_owed_sets") or not hasattr(
            self.engine, "subscription_coverage"
        ):
            raise TypeError("reporting_subscriptions requires a reporting subscription engine")


async def _resolve(value: Any) -> Any:
    return await value if inspect.isawaitable(value) else value


async def plan_subscription_cycle(
    options: ReportingSubscriptionServiceOptions,
    planner: Any,
    *,
    now: datetime,
    stopping: Callable[[], bool],
) -> tuple[int, list[SubscriptionPlanningFailure]]:
    """Plan every listed principal's frozen owed sets, isolating each subscription."""
    principals = await _resolve(options.list_principals())
    if isinstance(principals, (str, bytes)) or not isinstance(principals, Sequence):
        raise TypeError("list_principals must return a sequence of PrincipalIdentity")
    written = 0
    failures: list[SubscriptionPlanningFailure] = []
    for principal in principals:
        if stopping():
            break
        try:
            owed_sets = await options.engine.frozen_owed_sets(principal, now=now)
        except Exception as error:  # noqa: BLE001 - isolate one principal
            failures.append(SubscriptionPlanningFailure(principal, error))
            continue
        for owed in owed_sets:
            if stopping():
                break
            try:
                result = await planner.plan(owed, through=now)
            except Exception as error:  # noqa: BLE001 - isolate one subscription
                failures.append(
                    SubscriptionPlanningFailure(
                        principal,
                        error,
                        account_id=owed.account_id,
                        subscription_id=owed.subscription_id,
                    )
                )
                continue
            written += len(result.obligations_written)
    return written, failures


def subscription_capability_block(
    block: Mapping[str, Any],
    options: ReportingSubscriptionServiceOptions,
    *,
    adcp_version: str | None,
) -> dict[str, Any]:
    """``reporting_delivery`` with the subscription registration fields, as served."""
    from adcp.reporting.subscriptions.capabilities import (
        subscription_capabilities,
        with_subscription_dimensions,
    )

    payload = dict(block)
    if not payload or not serves_subscriptions(adcp_version):
        return payload
    mappings = {item.offering_id: item for item in options.engine.offerings}
    payload["offerings"] = [
        with_subscription_dimensions(item, mappings[str(item["offering_id"])])
        for item in payload.get("offerings") or ()
    ]
    return subscription_capabilities(payload, options.engine.support)


def advertise_subscriptions(
    response: Mapping[str, Any],
    options: ReportingSubscriptionServiceOptions,
    *,
    adcp_version: str | None,
) -> dict[str, Any]:
    """Add the feature id and the principal section, when subscriptions are served."""
    from adcp.reporting.subscriptions.capabilities import advertise_reporting_subscriptions

    if not serves_subscriptions(adcp_version):
        return dict(response)
    return advertise_reporting_subscriptions(response, options.engine.support)


async def require_subscription_grant(
    options: ReportingSubscriptionServiceOptions,
    store: Any,
    *,
    request: Any,
    context: Any,
    account_id: str,
    consumer_id: str,
    task: str,
) -> None:
    """Refuse ``task`` unless the principal's current grant allows it.

    Applies to a caller holding subscription generations on the account. Access
    means access: there is no window after a loss of authority, so a revoked
    account is unknown to every task, receipts and statuses included. A seller
    that wants a buyer to finish reconciliation keeps a reduced grant listing
    the reconciliation tasks instead of revoking.
    """
    from adcp.reporting.ledger.delivery_models import ReportingDeliveryPrincipal
    from adcp.reporting.ledger.store import LedgerConflictError

    configurations = await store.list_configurations(
        caller=ReportingDeliveryPrincipal(account_id, consumer_id)
    )
    if not any(item.owed_set for item in configurations):
        return
    principal = await _resolve(options.resolve_principal(request, context))
    if getattr(principal, "subject", None) != consumer_id:
        raise ReliableReportingConfigurationError(
            "resolve_principal and the caller resolver disagree on the requesting principal"
        )
    if not await options.engine.grants_task(principal, account_id, task):
        raise LedgerConflictError(
            "ACCOUNT_NOT_FOUND", "the account is not available to this caller"
        )


async def with_subscription_coverage(
    response: dict[str, Any],
    options: ReportingSubscriptionServiceOptions,
    *,
    request: Any,
    context: Any,
    account_id: str,
    consumer_id: str,
) -> dict[str, Any]:
    """Add ``subscription_coverage[]`` to a completed summary or periods view."""
    if response.get("status") != "completed" or response.get("view") not in {
        "summary",
        "periods",
    }:
        return response
    adcp_version = getattr(context, "resolved_adcp_version", None)
    if not serves_subscriptions(adcp_version):
        return response
    principal = await _resolve(options.resolve_principal(request, context))
    if getattr(principal, "subject", None) != consumer_id:
        raise ReliableReportingConfigurationError(
            "resolve_principal and the caller resolver disagree on the requesting principal"
        )
    coverage = await options.engine.subscription_coverage(principal, account_id)
    if not coverage:
        return response
    return {
        **response,
        "subscription_coverage": [
            item.model_dump(mode="json", exclude_none=True) if hasattr(item, "model_dump") else item
            for item in coverage
        ],
    }


class ProductionSubscriptionHooks:
    """Per-task grant checks and coverage for the mounted production handler."""

    def __init__(self, options: ReportingSubscriptionServiceOptions, store: Any) -> None:
        self._options, self._store = options, store

    async def require(
        self, caller: ReportingDeliveryPrincipal, request: Any, context: Any, task: str
    ) -> None:
        await require_subscription_grant(
            self._options,
            self._store,
            request=request,
            context=context,
            account_id=caller.account_id,
            consumer_id=caller.consumer_id,
            task=task,
        )

    async def coverage(
        self,
        response: dict[str, Any],
        caller: ReportingDeliveryPrincipal,
        request: Any,
        context: Any,
    ) -> dict[str, Any]:
        return await with_subscription_coverage(
            response,
            self._options,
            request=request,
            context=context,
            account_id=caller.account_id,
            consumer_id=caller.consumer_id,
        )


def check_managed_binding(
    binding: Any, configuration: ReportingConfiguration, version: Mapping[str, Any]
) -> None:
    """The binding a host returns must be this generation's, with the version's path."""
    from adcp.reporting.ledger.delivery_models import ReportingDestinationBinding

    destination = version.get("destination") or {}
    if (
        type(binding) is not ReportingDestinationBinding
        or binding.generation_key != configuration.generation_key
        or binding.consumer_id != configuration.consumer_id
        or binding.feed_purpose != configuration.feed_purpose
        or binding.path_template != destination.get("path_template")
    ):
        raise ReliableReportingConfigurationError(
            "bind_managed_generation must return this generation's binding with the "
            "version's destination path_template"
        )


__all__ = [
    "PlanningErrorHandler",
    "PrincipalLister",
    "PrincipalResolver",
    "ManagedGenerationBinder",
    "ProductionSubscriptionHooks",
    "ReportingSubscriptionServiceOptions",
    "SubscriptionOptions",
    "SubscriptionPlanningFailure",
    "serves_subscriptions",
]
