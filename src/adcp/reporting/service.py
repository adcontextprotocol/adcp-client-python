"""Adapter-first orchestration for AdCP Reliable Reporting.

The low-level reporting modules deliberately separate source acquisition,
ledger durability, status projection, and transport handlers. This module is
the adopter-facing composition layer: register small source adapters, resolve
trusted account context once per configuration generation, then let one
service own workers, exact reads, status handlers, and lifecycle.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import re
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel

from adcp.reporting._errors import (
    ReliableReportingConfigurationError,
    installed_reporting_call,
)
from adcp.reporting._source_authorization import source_turn
from adcp.reporting.inline_source import (
    InlineFetchResult,
    InlineReportingSource,
    InMemorySealStore,
    InMemoryStagingStore,
    ReportingSealStore,
    ReportingStagingStore,
)
from adcp.reporting.ledger import (
    ConsumerStatusIngest,
    InMemoryReportingLedgerStore,
    ProducerOfferings,
    ReportingConfiguration,
    ReportingConfigurationGenerationKey,
    ReportingDeliveryEscalation,
    ReportingLedgerStore,
    ReportingProducer,
    ReportingStatusCaller,
    ReportingStatusHandler,
    WorkerTurn,
)
from adcp.reporting.ledger.producer import _advertised_reporting_delivery
from adcp.reporting.ledger.store import LedgerConflictError, decode_cursor
from adcp.reporting.service_lifecycle import (
    ReliableReportingServiceError,
    ReliableReportingShutdownTimeoutError,
    ReliableReportingState,
    ReliableReportingUnavailableError,
    ReportingServiceResource,
    _ServiceLifecycle,
)
from adcp.reporting.source import (
    AuthoritativeOfferingV1,
    ProvisionalSnapshotOfferingV1,
    ReportingSourceCapabilitiesV1,
    ReportingSourceExecutor,
    ReportingSourceSliceRequestV1,
)
from adcp.types import ReportingDeliveryOffering

if TYPE_CHECKING:
    from adcp.reporting.production.contracts import ReportingProductionSourceBinding
    from adcp.reporting.production.service import ReportingProductionSupport
    from adcp.reporting.service_production import ReportingProductionOptions
    from adcp.reporting.service_subscriptions import (
        ReportingSubscriptionServiceOptions,
        SubscriptionPlanningFailure,
    )

__all__ = [
    "AdapterRegistration",
    "ReliableReportingConfigurationError",
    "ReliableReportingService",
    "ReliableReportingServiceError",
    "ReliableReportingShutdownTimeoutError",
    "ReliableReportingState",
    "ReliableReportingTurn",
    "ReliableReportingUnavailableError",
    "ReportingAccountContext",
    "ReportingAdapter",
    "ReportingAdapterRegistry",
    "ReportingBackgroundWorker",
    "ReportingCallerResolver",
    "ReportingContextResolver",
    "ReportingReceiptHandler",
    "ReportingServiceResource",
    "ReportingWorkerErrorHandler",
]

_ROUTE_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")
logger = logging.getLogger(__name__)


@runtime_checkable
class ReportingAdapter(Protocol):
    """The minimum source adapter: declaration plus one normalized slice read.

    ``fetch_slice`` contains no MCP, A2A, or AdCP task-handler concerns. It may
    be synchronous (the SDK moves it to a worker thread) or asynchronous.
    """

    @property
    def capabilities(self) -> ReportingSourceCapabilitiesV1: ...

    def fetch_slice(
        self, request: ReportingSourceSliceRequestV1
    ) -> InlineFetchResult | Sequence[Mapping[str, Any]] | None | Awaitable[Any]: ...


@runtime_checkable
class ReportingBackgroundWorker(Protocol):
    """Optional managed-delivery or notification worker extension."""

    async def run_once(self) -> Any: ...


@runtime_checkable
class ReportingReceiptHandler(Protocol):
    """Optional reconciled-billing receipt task extension."""

    async def handle(
        self, request: dict[str, Any], *, account_id: str, consumer_id: str
    ) -> dict[str, Any]: ...


ReportingContextResolver = Callable[
    [ReportingConfiguration], "ReportingAccountContext | Awaitable[ReportingAccountContext]"
]
ReportingCallerResolver = Callable[
    [Any, Any | None], "ReportingStatusCaller | Awaitable[ReportingStatusCaller]"
]
ReportingWorkerErrorHandler = Callable[[str, BaseException], Any | Awaitable[Any]]
ProducerFactory = Callable[..., ReportingProducer]


def _freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType({str(key): _freeze(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
    return value


def _thaw(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _thaw(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw(item) for item in value]
    return value


def _wire(value: Any) -> dict[str, Any]:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json", exclude_none=True)
    if isinstance(value, Mapping):
        return {str(key): _thaw(item) for key, item in value.items()}
    raise TypeError(f"expected a request model or mapping, got {type(value).__name__}")


async def _resolve(value: Any) -> Any:
    return await value if inspect.isawaitable(value) else value


@dataclass(frozen=True)
class ReportingAccountContext:
    """Trusted source facts frozen for one configuration generation."""

    account_id: str
    adapter: str
    currency: str
    source_scope: Mapping[str, Any]
    account_timezone: str = "UTC"
    snapshot_offering_id: str | None = None
    official_offering_id: str | None = None
    requested_metrics: tuple[str, ...] = ("impressions", "spend")
    requested_dimensions: tuple[str, ...] = ()
    capability_offering: Mapping[str, Any] = field(default_factory=dict)
    publication_namespace: str = "reporting-source:default"
    slice_timeout: timedelta = timedelta(minutes=10)

    def __post_init__(self) -> None:
        object.__setattr__(self, "requested_metrics", tuple(self.requested_metrics))
        object.__setattr__(self, "requested_dimensions", tuple(self.requested_dimensions))
        if not self.account_id:
            raise ReliableReportingConfigurationError("account_id is required")
        if not _ROUTE_RE.fullmatch(self.adapter):
            raise ReliableReportingConfigurationError(
                "adapter must be a stable identifier containing only letters, digits, ._:-"
            )
        if not re.fullmatch(r"[A-Z]{3}", self.currency):
            raise ReliableReportingConfigurationError("currency must be an ISO 4217 alpha-3 code")
        try:
            ZoneInfo(self.account_timezone)
        except ZoneInfoNotFoundError as error:
            raise ReliableReportingConfigurationError(
                f"unknown account_timezone {self.account_timezone!r}"
            ) from error
        if self.snapshot_offering_id is None and self.official_offering_id is None:
            raise ReliableReportingConfigurationError(
                "at least one snapshot or official source offering is required"
            )
        if not self.requested_metrics:
            raise ReliableReportingConfigurationError("requested_metrics must not be empty")
        if self.slice_timeout <= timedelta(0):
            raise ReliableReportingConfigurationError("slice_timeout must be greater than zero")
        object.__setattr__(self, "source_scope", _freeze(self.source_scope))
        object.__setattr__(self, "capability_offering", _freeze(self.capability_offering))

    def producer_offerings(self) -> ProducerOfferings:
        return ProducerOfferings(
            snapshot_offering_id=self.snapshot_offering_id,
            official_offering_id=self.official_offering_id,
            publication_namespace=self.publication_namespace,
            requested_metrics=self.requested_metrics,
            requested_dimensions=self.requested_dimensions,
            currency=self.currency,
            source_scope=_thaw(self.source_scope),
            slice_timeout=self.slice_timeout,
        )


@dataclass(frozen=True)
class AdapterRegistration:
    name: str
    executor: ReportingSourceExecutor
    object_reader: ReportingStagingStore | None
    adapter: ReportingAdapter | None = None
    capability_offerings: tuple[Mapping[str, Any], ...] = ()


class _AuthorizedInlineSource(InlineReportingSource):
    """Forward the adopter's existing production authority without caching it."""

    def __init__(
        self,
        *,
        configuration_binding: Callable[
            [ReportingConfiguration], ReportingProductionSourceBinding | None
        ],
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self._configuration_binding = configuration_binding

    def configuration_binding(
        self, configuration: ReportingConfiguration
    ) -> ReportingProductionSourceBinding | None:
        return self._configuration_binding(configuration)


class ReportingAdapterRegistry:
    """Named source adapters available to one service process."""

    def __init__(
        self,
        *,
        clock: Callable[[], datetime],
        staging: ReportingStagingStore | None = None,
        seals: ReportingSealStore | None = None,
    ) -> None:
        self._clock = clock
        self._staging = staging
        self._seals = seals
        self._registrations: dict[str, AdapterRegistration] = {}
        self._frozen = False

    def register(
        self,
        name: str,
        adapter: ReportingAdapter,
        *,
        staging: ReportingStagingStore | None = None,
        seals: ReportingSealStore | None = None,
        constituent_of: (
            Callable[[Mapping[str, Any], ReportingSourceSliceRequestV1], str | None] | None
        ) = None,
        capability_offerings: Sequence[ReportingDeliveryOffering | Mapping[str, Any]] = (),
    ) -> AdapterRegistration:
        """Wrap a small adapter using this service's default source storage.

        Managed adapters also implement the existing production source method
        ``configuration_binding(configuration)``. It is forwarded on every
        authorization check, without an SDK cache: revocation takes effect at
        the next dispatch or publish. Adopters own callback caching and latency.
        Explicit stores are borrowed and must have their schemas prepared.
        ``constituent_of`` supports row contracts with custom constituent keys;
        otherwise rows use media_buy_id, package_id, product_id or constituent_id.
        ``capability_offerings`` declares the public buyer contract before any
        account configures reporting. Source cadence alone cannot imply its SLA.
        """
        if not isinstance(adapter, ReportingAdapter):
            raise TypeError("adapter must expose capabilities and fetch_slice(request)")
        if staging is None:
            staging = self._staging if self._staging is not None else InMemoryStagingStore()
        if seals is None:
            seals = self._seals if self._seals is not None else InMemorySealStore()
        authority = getattr(adapter, "configuration_binding", None)
        source_type = _AuthorizedInlineSource if callable(authority) else InlineReportingSource
        executor = source_type(
            capabilities=adapter.capabilities,
            fetch=adapter.fetch_slice,
            staging=staging,
            seals=seals,
            constituent_of=constituent_of,
            clock=self._clock,
            **({"configuration_binding": authority} if callable(authority) else {}),
        )
        return self.register_executor(
            name,
            executor,
            object_reader=staging,
            adapter=adapter,
            capability_offerings=capability_offerings,
        )

    def register_executor(
        self,
        name: str,
        executor: ReportingSourceExecutor,
        *,
        object_reader: ReportingStagingStore | None,
        adapter: ReportingAdapter | None = None,
        capability_offerings: Sequence[ReportingDeliveryOffering | Mapping[str, Any]] = (),
    ) -> AdapterRegistration:
        """Advanced registration for custom manifest/staging implementations."""
        if self._frozen:
            raise ReliableReportingConfigurationError(
                "source registration is frozen after service initialization"
            )
        if not _ROUTE_RE.fullmatch(name):
            raise ReliableReportingConfigurationError(f"invalid adapter route {name!r}")
        if name in self._registrations:
            raise ReliableReportingConfigurationError(
                f"adapter route {name!r} is already registered"
            )
        registration = AdapterRegistration(
            name=name,
            executor=executor,
            object_reader=object_reader,
            adapter=adapter,
            capability_offerings=tuple(
                _freeze(
                    ReportingDeliveryOffering.model_validate(_wire(item)).model_dump(
                        mode="json", exclude_none=True
                    )
                )
                for item in capability_offerings
            ),
        )
        declared = {
            str(item["offering_id"]): item
            for registered in self._registrations.values()
            for item in registered.capability_offerings
        }
        for public in registration.capability_offerings:
            offering_id = str(public["offering_id"])
            if offering_id in declared and declared[offering_id] != public:
                raise ReliableReportingConfigurationError(
                    f"capability offering {offering_id!r} has conflicting declarations"
                )
            declared[offering_id] = public
            profile = public["reporting_profile"]
            for finality in public["supported_finality"]:
                matching = [
                    source
                    for source in executor.capabilities.offerings
                    if (
                        "snapshot"
                        if isinstance(source, ProvisionalSnapshotOfferingV1)
                        else "official"
                    )
                    == finality
                    and source.contract.report_definition_id == public["report_definition_id"]
                    and source.contract.report_definition_uri == public["report_definition_uri"]
                    and source.contract.report_definition_sha256
                    == public["report_definition_sha256"]
                    and source.contract.reporting_profile == profile["id"]
                    and source.contract.schema_version == profile["version"]
                    and source.contract.schema_uri == profile["schema_uri"]
                    and source.contract.schema_sha256 == profile["schema_sha256"]
                ]
                if not matching:
                    raise ReliableReportingConfigurationError(
                        f"capability offering {public['offering_id']!r} has no matching "
                        f"{finality} source contract on adapter {name!r}"
                    )
        self._registrations[name] = registration
        return registration

    def get(self, name: str) -> AdapterRegistration:
        try:
            return self._registrations[name]
        except KeyError as error:
            raise ReliableReportingConfigurationError(
                f"configuration names unregistered reporting adapter {name!r}"
            ) from error

    def freeze(self) -> None:
        self._frozen = True

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(sorted(self._registrations))


@dataclass(frozen=True)
class _Binding:
    configuration: ReportingConfiguration
    context: ReportingAccountContext
    producer: ReportingProducer


class _AccountContextResolutionError(Exception):
    """Distinguish resolver failures from invalid static configuration."""

    def __init__(self, error: Exception, *, permanent: bool = False) -> None:
        super().__init__("account context resolution failed")
        self.error = error
        self.permanent = permanent or isinstance(error, ReliableReportingConfigurationError)


@dataclass
class ReliableReportingTurn:
    """Result of one service turn across all frozen configuration routes."""

    configurations: dict[ReportingConfigurationGenerationKey, WorkerTurn] = field(
        default_factory=dict
    )
    configuration_errors: dict[ReportingConfigurationGenerationKey, BaseException] = field(
        default_factory=dict
    )
    extension_results: list[Any] = field(default_factory=list)
    extension_errors: dict[str, BaseException] = field(default_factory=dict)
    #: Reporting-subscription obligations written this turn.
    subscription_obligations_written: int = 0
    #: (principal subject, account_id, subscription_id) -> why it was not planned.
    subscription_errors: dict[tuple[str, str | None, str | None], BaseException] = field(
        default_factory=dict
    )

    @property
    def did_work(self) -> bool:
        return (
            any(turn.did_work for turn in self.configurations.values())
            or bool(self.extension_results)
            or bool(self.configuration_errors)
            or bool(self.extension_errors)
            or bool(self.subscription_obligations_written)
            or bool(self.subscription_errors)
        )


class ReliableReportingService:
    """High-level owner of adapters, ledger handlers, workers, and lifecycle.

    Admitted calls own a task so transport cancellation cannot interrupt their
    cleanup. Invoke them outside caller-owned ledger transactions; use the
    low-level store API when composing an ambient transaction batch. Injected
    resources are borrowed unless explicitly transferred via ``owned_resources``.
    """

    def __init__(
        self,
        *,
        store: ReportingLedgerStore,
        account_context: ReportingContextResolver,
        caller_resolver: ReportingCallerResolver | None = None,
        consumer_status_enabled: bool = False,
        escalation: ReportingDeliveryEscalation | None = None,
        clock: Callable[[], datetime] | None = None,
        worker_interval: timedelta | None = None,
        producer_factory: ProducerFactory = ReportingProducer,
        read_jitter_window: timedelta | None = None,
        materialization_worker: ReportingBackgroundWorker | None = None,
        notification_worker: ReportingBackgroundWorker | None = None,
        notification_attempt_store: Any | None = None,
        receipt_handler: ReportingReceiptHandler | None = None,
        reconciled_billing: bool = False,
        worker_error_handler: ReportingWorkerErrorHandler | None = None,
        owned_resources: Sequence[ReportingServiceResource] = (),
        production: ReportingProductionOptions | None = None,
        automated_recovery_window: timedelta = timedelta(hours=6),
        status_retention_days: int = 400,
        staging: ReportingStagingStore | None = None,
        seals: ReportingSealStore | None = None,
        initialize_source_storage: Callable[[], Awaitable[None]] | None = None,
        reporting_subscriptions: ReportingSubscriptionServiceOptions | None = None,
    ) -> None:
        """``staging``/``seals`` are the default source stores adapters register with;
        ``initialize_source_storage`` runs once at startup, after ``store.create_schema()``,
        so a durable storage distribution can install its own schema.
        ``reporting_subscriptions`` serves AdCP 3.3 principal-level reporting
        subscriptions (see :mod:`adcp.reporting.service_subscriptions`); without
        it the service is unchanged."""
        if production is not None and (
            caller_resolver is not None
            or producer_factory is not ReportingProducer
            or read_jitter_window is not None
            or worker_interval is not None
            or materialization_worker is not None
            or notification_worker is not None
            or notification_attempt_store is not None
            or receipt_handler is not None
            or reconciled_billing
            or worker_error_handler is not None
        ):
            raise ReliableReportingConfigurationError(
                "production options compose their own workers, admission and receipt handlers"
            )
        if (
            production is not None
            and reporting_subscriptions is not None
            and reporting_subscriptions.bind_managed_generation is None
        ):
            raise ReliableReportingConfigurationError(
                "production serves reporting subscriptions through managed delivery; "
                "supply reporting_subscriptions.bind_managed_generation"
            )
        effective_clock = clock or (lambda: datetime.now(timezone.utc))
        self.store = store
        self._production: ReportingProductionSupport | None = None
        self._production_options = production
        self._initialize_source_storage = initialize_source_storage
        self.sources = ReportingAdapterRegistry(clock=effective_clock, staging=staging, seals=seals)
        self._context_resolver = account_context
        self._caller_resolver = caller_resolver or self._default_caller
        self._consumer_status_enabled = consumer_status_enabled
        self._escalation = escalation or ReportingDeliveryEscalation()
        self._clock = effective_clock
        if automated_recovery_window < timedelta(0) or status_retention_days < 1:
            raise ReliableReportingConfigurationError("invalid reporting recovery/retention policy")
        self._automated_recovery_window = automated_recovery_window
        self._status_retention_days = status_retention_days
        self._worker_interval = worker_interval
        self._producer_factory = producer_factory
        # Preserve the existing custom-factory keyword contract unless the
        # caller explicitly opts into forwarding this additional setting.
        self._pass_read_jitter_window = (
            producer_factory is ReportingProducer or read_jitter_window is not None
        )
        self._read_jitter_window = (
            timedelta(minutes=5) if read_jitter_window is None else read_jitter_window
        )
        if self._read_jitter_window < timedelta(0):
            raise ReliableReportingConfigurationError("read jitter window must be nonnegative")
        self._materialization_worker = materialization_worker
        self._notification_worker = notification_worker
        self._notification_attempt_store = notification_attempt_store
        self._receipt_handler = receipt_handler
        self._reconciled_billing = reconciled_billing
        self._worker_error_handler = worker_error_handler
        self._bindings: dict[ReportingConfigurationGenerationKey, _Binding] = {}
        self._pending_configurations: dict[
            ReportingConfigurationGenerationKey, ReportingConfiguration
        ] = {}
        self._initialized = False
        self._configuration_lock = asyncio.Lock()
        self._initialization_errors: dict[ReportingConfigurationGenerationKey, Exception] = {}
        self._pending_recovery: dict[
            ReportingConfigurationGenerationKey, ReportingConfiguration
        ] = {}
        self._turn_lock = asyncio.Lock()
        self._subscriptions = reporting_subscriptions
        self._subscription_planner: Any | None = None
        if reporting_subscriptions is not None and production is None:
            from adcp.reporting.subscriptions.ledger import SubscriptionObligationPlanner

            try:
                self._subscription_planner = SubscriptionObligationPlanner(
                    store,
                    offerings=reporting_subscriptions.engine.offerings,
                    currency=self._subscription_currency,
                    definitions=reporting_subscriptions.definitions,
                    automated_recovery_window=automated_recovery_window,
                    status_retention_days=status_retention_days,
                    install=self._install_subscription_generation,
                )
            except TypeError as error:
                raise ReliableReportingConfigurationError(str(error)) from None
        self._lifecycle = _ServiceLifecycle(
            self._initialize,
            resources=(
                (*owned_resources, ReportingServiceResource(close=self._close_production))
                if production is not None
                else owned_resources
            ),
            configuration_error=ReliableReportingConfigurationError,
        )

    @classmethod
    def from_production(
        cls,
        production: ReportingProductionSupport,
        *,
        owned_resources: Sequence[ReportingServiceResource] = (),
    ) -> ReliableReportingService:
        """Own a preassembled B2 graph with durable fixed-profile admission.

        Mount the exact handler returned by ``install(application)`` before
        startup. This explicit bridge requires the real production components
        and a source registry; it is not an adapter-first component factory.
        Production tasks use their existing indexed leases. Pools and providers
        stay borrowed unless explicitly transferred in ``owned_resources``.
        """
        from adcp.reporting.production.service import ReportingProductionSupport

        if (
            type(production) is not ReportingProductionSupport
            or production.source_registry is None
            or production._task is not None
            or production._closed
            or production._service_lifecycle is not None
        ):
            raise ReliableReportingConfigurationError(
                "requires an unstarted registered production graph"
            )

        def unavailable(_: ReportingConfiguration) -> ReportingAccountContext:
            raise ReliableReportingConfigurationError(
                "production requires typed sync_accounts admission"
            )

        service = cls(
            store=production.store,
            account_context=unavailable,
            owned_resources=(*owned_resources, ReportingServiceResource(close=production.aclose)),
        )
        service._production = production
        production._service_lifecycle = service._lifecycle
        return service

    @classmethod
    def memory(
        cls,
        *,
        account_context: ReportingContextResolver,
        caller_resolver: ReportingCallerResolver | None = None,
        clock: Callable[[], datetime] | None = None,
        production: ReportingProductionOptions | None = None,
        **kwargs: Any,
    ) -> ReliableReportingService:
        """Create a deterministic in-memory service for tests and pilots."""
        from adcp.reporting.production.memory import InMemoryReportingProductionStore

        store = (
            InMemoryReportingProductionStore(clock=clock, notifications=production.notifications)
            if production is not None
            else InMemoryReportingLedgerStore(clock=clock)
        )
        return cls(
            store=store,
            account_context=account_context,
            caller_resolver=caller_resolver,
            clock=clock,
            production=production,
            **kwargs,
        )

    def _compose_production(self) -> None:
        if self._production_options is None or self._production is not None:
            return
        from adcp.reporting.production.store import ReportingProductionStore

        if (
            not isinstance(self.store, ReportingProductionStore)
            or not self.store.is_sdk_production_store()
        ):
            raise ReliableReportingConfigurationError(
                "production options require a memory or postgres production store"
            )
        subscriptions = self._subscriptions
        self._production = self._production_options._compose(
            store=self.store,
            sources=self.sources,
            account_context=self._context_resolver,
            clock=self._clock,
            escalation=self._escalation,
            consumer_status_enabled=self._consumer_status_enabled,
            authorize_subscription_delivery=(
                subscriptions.engine.authorizes_delivery if subscriptions is not None else None
            ),
        )
        self._production._service_lifecycle = self._lifecycle
        if subscriptions is not None:
            self._compose_production_subscriptions()
        self.sources.freeze()

    def _compose_production_subscriptions(self) -> None:
        """Serve reporting subscriptions through production managed delivery."""
        from adcp.reporting.service_subscriptions import ProductionSubscriptionHooks
        from adcp.reporting.subscriptions.ledger import SubscriptionObligationPlanner

        support, subscriptions = self._production, self._subscriptions
        assert support is not None and subscriptions is not None
        if not all(
            callable(getattr(self.store, name, None))
            for name in (
                "admit_subscription_generation",
                "subscription_generation_admitted",
                "commit_subscription_obligation",
                "production_owed_sets",
            )
        ):
            raise ReliableReportingConfigurationError(
                "this production store cannot hold reporting-subscription owed sets"
            )
        offerings = {item.offering_id: item for item in support.offerings}
        try:
            self._subscription_planner = SubscriptionObligationPlanner(
                self.store,
                offerings=subscriptions.engine.offerings,
                # The installed verification key is the definition each
                # managed generation must report against.
                definitions={
                    offering_id: item.verification_key.definition
                    for offering_id, item in offerings.items()
                },
                currency=lambda configuration: self._production_offering(
                    configuration
                ).producer._offerings.currency,
                automated_recovery_window=support.automated_recovery_window,
                status_retention_days=support.status_retention_days,
                install=self._install_production_generation,
                commit=self._commit_production_obligation,
            )
        except TypeError as error:
            raise ReliableReportingConfigurationError(str(error)) from None
        support.subscription_cycle = self._production_subscription_cycle
        support.subscription_hooks = ProductionSubscriptionHooks(subscriptions, self.store)

    def _production_offering(self, configuration: ReportingConfiguration) -> Any:
        assert self._production is not None and configuration.subscription is not None
        wanted = configuration.subscription.offering_id
        matches = [o for o in self._production.offerings if o.offering_id == wanted]
        if len(matches) != 1:
            raise ReliableReportingConfigurationError(
                f"offering {wanted!r} is not a production offering"
            )
        return matches[0]

    async def _install_production_generation(
        self, configuration: ReportingConfiguration, version: Mapping[str, Any]
    ) -> None:
        """Bind, admit and activate a managed subscription generation (idempotent)."""
        from adcp.reporting.service_subscriptions import check_managed_binding

        assert self._production is not None and self._subscriptions is not None
        binder = self._subscriptions.bind_managed_generation
        assert binder is not None and configuration.subscription is not None
        if await self.store.subscription_generation_admitted(  # type: ignore[attr-defined]
            configuration
        ):
            return  # bound once, before its first obligation
        binding = await _resolve(binder(configuration, version))
        check_managed_binding(binding, configuration, version)
        await self.store.admit_subscription_generation(  # type: ignore[attr-defined]
            configuration, binding, offering_id=configuration.subscription.offering_id
        )
        await self._production.activate(account_id=configuration.account_id)

    async def _commit_production_obligation(
        self, configuration: ReportingConfiguration, obligation: Any
    ) -> Any:
        """Commit an owed obligation with the constituents of exactly its media buys.

        The host's current source binding is the authorized pool; every media
        buy the obligation owes must be in it, or the subscription fails to
        plan this turn and nothing is written.
        """
        from adcp.reporting.production.contracts import (
            ReportingProductionSource,
            ReportingProductionSourceBinding,
        )
        from adcp.reporting.source import MediaBuyConstituentV1

        offering = self._production_offering(configuration)
        source = offering.producer._source
        if not isinstance(source, ReportingProductionSource):
            raise ReliableReportingConfigurationError("production source is unavailable")
        binding = source.configuration_binding(configuration)
        if type(binding) is not ReportingProductionSourceBinding:
            raise LedgerConflictError(
                "SOURCE_UNAUTHORIZED", "the source does not authorize this generation"
            )
        binding.check(configuration, source.capabilities, offering.source_offering_id)
        pool = dict(binding.media_buy_products)
        missing = sorted(set(obligation.media_buy_ids) - set(pool))
        if missing:
            raise LedgerConflictError(
                "SOURCE_UNAUTHORIZED",
                "the source does not authorize every media buy the subscription owes",
            )
        constituents = tuple(
            MediaBuyConstituentV1(
                constituent_id=media_buy_id,
                media_buy_id=media_buy_id,
                product_id=pool[media_buy_id],
            )
            for media_buy_id in sorted(obligation.media_buy_ids)
        )
        return await self.store.commit_subscription_obligation(  # type: ignore[attr-defined]
            configuration, obligation, constituents
        )

    async def _production_subscription_cycle(self) -> None:
        from adcp.reporting.service_subscriptions import plan_subscription_cycle

        assert self._production is not None and self._subscriptions is not None
        production = self._production
        _, failures = await plan_subscription_cycle(
            self._subscriptions,
            self._subscription_planner,
            now=self._clock(),
            stopping=lambda: production._stop.is_set(),
        )
        for failure in failures:
            await self._report_planning_failure(failure)

    async def _close_production(self) -> None:
        if self._production is not None:
            await self._production.aclose()

    async def configure(self, configuration: ReportingConfiguration) -> None:
        """Resolve trusted account facts once and freeze this generation's route."""
        if self._production is not None or self._production_options is not None:
            raise ReliableReportingConfigurationError(
                "production requires typed sync_accounts admission"
            )

        async def configure() -> None:
            async with self._configuration_lock:
                await self._configure(configuration)

        await self._lifecycle.call(configure, before_start=True)

    async def _configure(
        self,
        configuration: ReportingConfiguration,
        *,
        persist: bool = True,
        recovering: bool = False,
        retrying: bool = False,
    ) -> None:
        key = configuration.generation_key
        existing = self._bindings.get(key)
        if existing is not None:
            if existing.configuration != configuration:
                raise ReliableReportingConfigurationError(
                    f"configuration {key.delivery_config_id}@{key.delivery_config_version} "
                    f"for account {key.account_id!r} is already frozen with other facts"
                )
            return
        try:
            context = await _resolve(self._context_resolver(configuration))
        except Exception as error:
            if recovering:
                raise _AccountContextResolutionError(error) from error
            raise
        try:
            if not isinstance(context, ReportingAccountContext):
                raise TypeError("account_context must resolve ReportingAccountContext")
            if context.account_id != configuration.account_id:
                raise ReliableReportingConfigurationError(
                    "resolved account context does not match the configuration account"
                )
            # An owed set never derives periods from its schedule: each
            # obligation carries its own period and source timezone.
            if (
                not configuration.owed_set
                and context.account_timezone != configuration.account_timezone
            ):
                raise ReliableReportingConfigurationError(
                    "resolved account timezone does not match the configuration timezone"
                )
            registration = self.sources.get(context.adapter)
        except Exception as error:
            if retrying:
                raise _AccountContextResolutionError(error, permanent=True) from error
            raise
        # Check registered source facts separately from the returned account
        # context. Initial setup failures abort startup; queued retries record
        # configuration errors without interrupting healthy bindings.
        if registration.executor.capabilities.scope != "effective_account":
            raise ReliableReportingConfigurationError(
                "registered source capabilities must be scoped to an effective account"
            )
        try:
            self._validate_offerings(configuration, context, registration)
            offerings = context.producer_offerings()
            if not isinstance(offerings, ProducerOfferings):
                raise TypeError("producer_offerings must return ProducerOfferings")
        except Exception as error:
            if retrying:
                raise _AccountContextResolutionError(error, permanent=True) from error
            raise
        producer = self._producer_factory(
            source=registration.executor,
            offerings=offerings,
            store=self.store,
            object_reader=registration.object_reader,
            escalation=self._escalation,
            worker_id=f"reporting-service:{context.adapter}",
            clock=self._clock,
            **(
                {"read_jitter_window": self._read_jitter_window}
                if self._pass_read_jitter_window
                else {}
            ),
        )
        binding = _Binding(configuration=configuration, context=context, producer=producer)
        if persist:
            if self._initialized:
                await self.store.put_configuration(configuration)
            else:
                self._pending_configurations[key] = configuration
        self._bindings[key] = binding
        self._initialization_errors.pop(key, None)
        self._pending_recovery.pop(key, None)

    @property
    def initialization_errors(self) -> Mapping[ReportingConfigurationGenerationKey, Exception]:
        """Read-only view of unresolved stored configuration bindings.

        Worker turns retry temporary resolution and construction failures and
        include all unresolved errors in ``configuration_errors``. Configuration
        errors and invalid contexts returned during recovery retries require an
        explicit ``configure`` call after the underlying facts are fixed.
        """
        return MappingProxyType(self._initialization_errors)

    async def _install_subscription_generation(
        self, configuration: ReportingConfiguration, version: Mapping[str, Any]
    ) -> None:
        """Install one subscription generation through the trusted configure path."""
        async with self._configuration_lock:
            await self._configure(configuration)

    def _subscription_currency(self, configuration: ReportingConfiguration) -> str:
        return self._bindings[configuration.generation_key].context.currency

    async def _plan_subscriptions(self, turn: ReliableReportingTurn, *, now: datetime) -> None:
        from adcp.reporting.service_subscriptions import plan_subscription_cycle

        assert self._subscriptions is not None
        written, failures = await plan_subscription_cycle(
            self._subscriptions,
            self._subscription_planner,
            now=now,
            stopping=lambda: self._lifecycle.stopping,
        )
        turn.subscription_obligations_written += written
        for failure in failures:
            turn.subscription_errors[failure.key] = failure.error
            await self._report_planning_failure(failure)

    async def _report_planning_failure(self, failure: SubscriptionPlanningFailure) -> None:
        assert self._subscriptions is not None
        handler = self._subscriptions.planning_error_handler
        if handler is None:
            await self._report_worker_error(
                "subscription:"
                f"{failure.principal.subject}:{failure.account_id or ''}:"
                f"{failure.subscription_id or ''}",
                failure.error,
            )
            return
        try:
            await _resolve(handler(failure))
        except Exception:
            logger.error("Reliable Reporting subscription planning error handler failed")

    async def _recover_configuration(
        self, configuration: ReportingConfiguration, *, retrying: bool = False
    ) -> None:
        try:
            await self._configure(configuration, persist=False, recovering=True, retrying=retrying)
        except Exception as failure:
            if isinstance(failure, _AccountContextResolutionError):
                error = failure.error
                permanent = failure.permanent
            elif retrying:
                # Initial static setup still fails startup. A queued account
                # retry owns its entire binding attempt, including construction,
                # so it cannot interrupt already-bound healthy accounts.
                error = failure
                permanent = isinstance(error, ReliableReportingConfigurationError)
            else:
                raise
            key = configuration.generation_key
            self._initialization_errors[key] = error
            if permanent:
                self._pending_recovery.pop(key, None)
            else:
                self._pending_recovery[key] = configuration
            # Provider errors can contain credentials or account identifiers.
            # Detailed failures remain available through the read-only property.
            logger.warning("Reliable Reporting stored account context could not be resolved")

    def _validate_offerings(
        self,
        configuration: ReportingConfiguration,
        context: ReportingAccountContext,
        registration: AdapterRegistration,
    ) -> None:
        capabilities = registration.executor.capabilities
        if capabilities.source_scope != _thaw(context.source_scope):
            raise ReliableReportingConfigurationError(
                "resolved source_scope does not match the registered adapter capabilities"
            )
        selected: list[ProvisionalSnapshotOfferingV1 | AuthoritativeOfferingV1] = []
        if context.snapshot_offering_id is not None:
            try:
                snapshot = capabilities.offering(context.snapshot_offering_id)
            except KeyError as error:
                raise ReliableReportingConfigurationError(
                    f"snapshot offering {context.snapshot_offering_id!r} is not registered"
                ) from error
            if not isinstance(snapshot, ProvisionalSnapshotOfferingV1):
                raise ReliableReportingConfigurationError(
                    "snapshot_offering_id must name a provisional snapshot offering"
                )
            selected.append(snapshot)
        if context.official_offering_id is not None:
            try:
                official = capabilities.offering(context.official_offering_id)
            except KeyError as error:
                raise ReliableReportingConfigurationError(
                    f"official offering {context.official_offering_id!r} is not registered"
                ) from error
            if not isinstance(official, AuthoritativeOfferingV1):
                raise ReliableReportingConfigurationError(
                    "official_offering_id must name an authoritative offering"
                )
            selected.append(official)
        for offering in selected:
            if (
                offering.contract.report_definition_id != configuration.report_definition_id
                or offering.contract.reporting_profile != configuration.reporting_profile
            ):
                raise ReliableReportingConfigurationError(
                    "source offering contract does not match the reporting configuration"
                )
        required = (
            context.official_offering_id
            if configuration.required_finality == "official"
            else context.snapshot_offering_id
        )
        if required is None:
            raise ReliableReportingConfigurationError(
                f"configuration requires {configuration.required_finality} but its account "
                "context declares no matching source offering"
            )
        declared_id = context.capability_offering.get("offering_id")
        if not declared_id:
            raise ReliableReportingConfigurationError(
                "capability_offering must contain the advertised offering_id"
            )
        if context.capability_offering.get("feed_purpose") != configuration.feed_purpose:
            raise ReliableReportingConfigurationError(
                "advertised capability offering feed_purpose does not match the configuration"
            )
        if (
            context.capability_offering.get("report_definition_id")
            != configuration.report_definition_id
        ):
            raise ReliableReportingConfigurationError(
                "advertised capability offering report definition does not match the configuration"
            )
        supported_finality = context.capability_offering.get("supported_finality", ())
        if configuration.required_finality not in supported_finality:
            raise ReliableReportingConfigurationError(
                "advertised capability offering does not support the required finality"
            )
        if registration.capability_offerings:
            public = ReportingDeliveryOffering.model_validate(
                _thaw(context.capability_offering)
            ).model_dump(mode="json", exclude_none=True)
            if not any(_thaw(item) == public for item in registration.capability_offerings):
                raise ReliableReportingConfigurationError(
                    "account capability offering must match a registered public offering"
                )

    def validate(self) -> None:
        """Fail fast on tier combinations the configured components cannot honor."""
        self._compose_production()
        if self._production is not None and self._production_options is None and self.sources.names:
            raise ReliableReportingConfigurationError(
                "production adapters must belong to its frozen source registry"
            )
        if self._worker_interval is not None and self._worker_interval <= timedelta(0):
            raise ReliableReportingConfigurationError("worker_interval must be greater than zero")
        if self._notification_worker is not None and self._notification_attempt_store is None:
            raise ReliableReportingConfigurationError(
                "a notification worker requires durable notification attempt storage"
            )
        if self._subscriptions is not None:
            self._validate_subscriptions()
        if self._receipt_handler is not None and self._materialization_worker is None:
            raise ReliableReportingConfigurationError(
                "receipt handling requires managed delivery/materialization"
            )
        if self._reconciled_billing and (
            self._receipt_handler is None or self._materialization_worker is None
        ):
            raise ReliableReportingConfigurationError(
                "reconciled billing requires managed delivery and a receipt handler"
            )
        if self._production is None:
            for name in self.sources.names:
                for offering in self.sources.get(name).capability_offerings:
                    if offering.get("method") and self._materialization_worker is None:
                        raise ReliableReportingConfigurationError(
                            "managed capability offerings require a materialization worker"
                        )
                    if (
                        offering["reconciliation_mode"] == "consumer_receipt"
                        and not self._reconciled_billing
                    ):
                        raise ReliableReportingConfigurationError(
                            "consumer_receipt offerings require reconciled billing"
                        )

    def _validate_subscriptions(self) -> None:
        assert self._subscriptions is not None
        engine = self._subscriptions.engine
        if engine.status_retention_days != self._status_retention_days:
            raise ReliableReportingConfigurationError(
                "the reporting subscription engine must use the status_retention_days this "
                "service advertises"
            )
        mapped = {item.offering_id for item in engine.offerings}
        if self._production_options is not None:
            for item in self._production_options.offerings:
                wire = _wire(item.offering)
                if wire["offering_id"] not in mapped:
                    raise ReliableReportingConfigurationError(
                        f"offering {wire['offering_id']!r} has no subscription dimension "
                        "mapping; register it with the reporting subscription engine"
                    )
                if wire["reconciliation_mode"] != "delivery_only":
                    raise ReliableReportingConfigurationError(
                        "reporting subscriptions do not yet serve receipt-reconciled offerings"
                    )
        for name in self.sources.names:
            for offering in self.sources.get(name).capability_offerings:
                # adcp#8585: every advertised offering publishes standard_dimensions,
                # so buyers resolve a selector from the wire as the engine does.
                if offering["offering_id"] not in mapped:
                    raise ReliableReportingConfigurationError(
                        f"offering {offering['offering_id']!r} has no subscription dimension "
                        "mapping; register it with the reporting subscription engine"
                    )
                if offering.get("method") or offering["reconciliation_mode"] != "delivery_only":
                    raise ReliableReportingConfigurationError(
                        "reporting subscriptions do not yet serve managed or receipt-reconciled "
                        "offerings"
                    )

    async def initialize(self) -> None:
        """Prepare resources once; start/run_worker opens reporting admission."""
        await self._lifecycle.initialize()

    async def _initialize(self) -> None:
        self.validate()
        async with self._configuration_lock:
            await self.store.create_schema()
            if self._initialize_source_storage is not None:
                await self._initialize_source_storage()
            for configuration in self._pending_configurations.values():
                await self.store.put_configuration(configuration)
            self._pending_configurations.clear()
            if self._production is None and self.sources.names:
                # Older custom stores can retain explicit configure() startup.
                # Built-in stores recover all generations in one scan, without
                # rewriting their activation, retirement or producer progress.
                enumerate_configurations = getattr(self.store, "list_all_configurations", None)
                if callable(enumerate_configurations):
                    for configuration in await enumerate_configurations():
                        await self._recover_configuration(configuration)
            self.sources.freeze()
            if self._production is not None:
                await self._production.start()
            self._initialized = True

    async def start(self) -> None:
        await self.initialize()
        await self._lifecycle.activate(
            self._monitor_production
            if self._production is not None
            else (self._worker_loop if self._worker_interval is not None else None)
        )

    async def _monitor_production(self) -> None:
        assert self._production is not None
        while not self._lifecycle.stopping:
            self._production._assert_components()
            stop = asyncio.create_task(self._lifecycle.wait_for_stop(60))
            workers = [
                task
                for task in (self._production._task, self._production._notification_task)
                if task is not None
            ]
            try:
                await asyncio.wait([stop, *workers], return_when=asyncio.FIRST_COMPLETED)
            finally:
                stop.cancel()
                await asyncio.gather(stop, return_exceptions=True)

    @property
    def state(self) -> ReliableReportingState:
        """Current process lifecycle; STOPPING retains all unsettled ownership."""
        return self._lifecycle.state

    @property
    def ready(self) -> bool:
        """Whether this process admits work (not a durable-tier graph proof)."""
        return self._lifecycle.ready

    @property
    def failure(self) -> ReliableReportingServiceError | None:
        """Sanitized first terminal lifecycle failure, if any."""
        return self._lifecycle.failure

    async def close(self, *, timeout: float | None = None) -> None:
        """Reject new work, drain admitted operations, then close owned resources.

        A timeout or cancelled waiter leaves the shared shutdown running and the
        service STOPPING until settlement. Injected components are borrowed;
        transfer lifetime explicitly with ``owned_resources`` to close them here.
        """
        await self._lifecycle.close(timeout=timeout)

    async def wait(self) -> None:
        """Wait for shutdown and raise a typed failure if supervision failed."""
        await self._lifecycle.wait()

    async def __aenter__(self) -> ReliableReportingService:
        await self.start()
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.close()

    async def _worker_loop(self) -> None:
        assert self._worker_interval is not None
        while not self._lifecycle.stopping:
            try:
                await self.run_worker()
            except Exception as error:
                # Configuration and extension failures are isolated inside a
                # turn. Only a failure of the scheduler itself stops service.
                if not self._lifecycle.stopping:
                    await self._lifecycle.fail("service")
                    await self._report_worker_error("service", error)
                return
            await self._lifecycle.wait_for_stop(self._worker_interval.total_seconds())

    async def _report_worker_error(self, component: str, error: BaseException) -> None:
        # Preserve the adopter callback's detailed identity and original error,
        # without writing account identifiers or provider bodies to SDK logs.
        logger.error("Reliable Reporting worker component %s failed", component.split(":", 1)[0])
        if self._worker_error_handler is None:
            return
        try:
            await _resolve(self._worker_error_handler(component, error))
        except Exception:
            logger.error("Reliable Reporting worker error handler failed")

    async def run_worker(self, *, now: datetime | None = None) -> ReliableReportingTurn:
        """Route one turn across every frozen configuration generation."""
        if self._production is not None or self._production_options is not None:
            raise ReliableReportingConfigurationError("production workers are owned by start/close")
        await self.initialize()
        await self._lifecycle.activate()
        return await self._lifecycle.call(lambda: self._run_worker(now=now))

    async def _run_worker(self, *, now: datetime | None) -> ReliableReportingTurn:
        async with self._turn_lock:
            # A turn admitted before STOPPING may have waited behind another
            # turn. It must not start fresh work after that turn has drained.
            self._lifecycle.require_ready()
            turn = ReliableReportingTurn()
            async with self._configuration_lock:
                for configuration in tuple(self._pending_recovery.values()):
                    if self._lifecycle.stopping:
                        break
                    await self._recover_configuration(configuration, retrying=True)
                turn.configuration_errors.update(self._initialization_errors)
            if self._subscriptions is not None and not self._lifecycle.stopping:
                # Plan first, so newly frozen obligations run in this turn.
                await self._plan_subscriptions(turn, now=now or self._clock())
            with source_turn():
                for key, binding in sorted(
                    self._bindings.items(),
                    key=lambda item: (
                        item[0].account_id,
                        item[0].delivery_config_id,
                        item[0].delivery_config_version,
                        item[0].consumer_id,
                        item[0].offering_id or "",
                    ),
                ):
                    if self._lifecycle.stopping:
                        break
                    try:
                        turn.configurations[key] = await binding.producer.run_configuration(
                            binding.configuration, now=now
                        )
                    except Exception as error:
                        turn.configuration_errors[key] = error
                        await self._report_worker_error(
                            "configuration:"
                            f"{key.account_id}:{key.delivery_config_id}@"
                            f"{key.delivery_config_version}",
                            error,
                        )
            extensions = (
                ("materialization", self._materialization_worker),
                ("notification", self._notification_worker),
            )
            for name, extension in extensions:
                if self._lifecycle.stopping:
                    break
                if extension is not None:
                    try:
                        result = await extension.run_once()
                    except Exception as error:
                        turn.extension_errors[name] = error
                        await self._report_worker_error(name, error)
                        continue
                    if result is not None:
                        turn.extension_results.append(result)
            return turn

    async def caller_for(self, request: Any, context: Any | None) -> ReportingStatusCaller:
        caller = await _resolve(self._caller_resolver(request, context))
        if not isinstance(caller, ReportingStatusCaller):
            raise TypeError("caller_resolver must resolve ReportingStatusCaller")
        return caller

    @staticmethod
    def _default_caller(request: Any, context: Any | None) -> ReportingStatusCaller:
        del request
        # RequestContext is duck-typed here: importing decisioning.context at
        # module scope would cycle through the decisioning reporting helpers.
        # Its caller_identity is a per-account cache key, not a buyer identity.
        if hasattr(context, "account_id"):
            # AccountAwareToolContext.account is an opaque adopter object;
            # resolve_account_into_context supplies its stable id separately.
            account_id = getattr(context, "account_id", None)
            consumer_id = getattr(context, "caller_identity", None)
        else:
            from adcp.server.auth import current_principal

            account_id = getattr(getattr(context, "account", None), "id", None)
            consumer_id = current_principal.get()
            if consumer_id is None:
                # Signed requests carry their verified identity on AuthInfo,
                # without populating the bearer middleware's ContextVar.
                auth_principal = getattr(context, "auth_principal", None)
                auth_info = getattr(context, "auth_info", None)
                if auth_principal == getattr(auth_info, "principal", None):
                    consumer_id = auth_principal
        if not account_id or not consumer_id:
            raise ReliableReportingConfigurationError(
                "default caller resolution requires AccountAwareToolContext.account_id and "
                "authenticated caller_identity, or RequestContext.account.id and an "
                "authenticated current_principal or verified auth_principal; "
                "provide caller_resolver for another trust model",
                kind="auth_required" if not consumer_id else "misconfiguration",
            )
        return ReportingStatusCaller(account_id=account_id, consumer_id=consumer_id)

    async def get_reporting_status(
        self, request: Any, context: Any | None = None
    ) -> dict[str, Any]:
        if self._production is not None:
            return _wire(await self._production.handler.get_reporting_status(request, context))
        return await self._lifecycle.call(lambda: self._get_reporting_status(request, context))

    async def _require_grant(
        self, request: Any, context: Any | None, caller: ReportingStatusCaller, task: str
    ) -> None:
        if self._subscriptions is None:
            return
        from adcp.reporting.service_subscriptions import require_subscription_grant

        await require_subscription_grant(
            self._subscriptions,
            self.store,
            request=request,
            context=context,
            account_id=caller.account_id,
            consumer_id=caller.consumer_id,
            task=task,
        )

    async def _get_reporting_status(self, request: Any, context: Any | None) -> dict[str, Any]:
        caller = await self.caller_for(request, context)
        await self._require_grant(request, context, caller, "get_reporting_status")
        response = await ReportingStatusHandler(
            self.store,
            consumer_status_enabled=self._consumer_status_enabled,
            escalation=self._escalation,
        ).handle(_wire(request), caller=caller)
        if self._subscriptions is None:
            return response
        from adcp.reporting.service_subscriptions import with_subscription_coverage

        return await with_subscription_coverage(
            response,
            self._subscriptions,
            request=request,
            context=context,
            account_id=caller.account_id,
            consumer_id=caller.consumer_id,
        )

    async def sync_reporting_status(
        self, request: Any, context: Any | None = None
    ) -> dict[str, Any]:
        if self._production is not None:
            return _wire(await self._production.handler.sync_reporting_status(request, context))
        return await self._lifecycle.call(lambda: self._sync_reporting_status(request, context))

    async def _sync_reporting_status(self, request: Any, context: Any | None) -> dict[str, Any]:
        if not self._consumer_status_enabled:
            raise ReliableReportingConfigurationError("consumer status ingest is disabled")
        caller = await self.caller_for(request, context)
        await self._require_grant(request, context, caller, "sync_reporting_status")
        return await ConsumerStatusIngest(self.store, enabled=True, clock=self._clock).handle(
            _wire(request),
            account_id=caller.account_id,
            consumer_id=caller.consumer_id,
        )

    async def sync_reporting_receipts(
        self, request: Any, context: Any | None = None
    ) -> dict[str, Any]:
        if self._production is not None:
            return _wire(await self._production.handler.sync_reporting_receipts(request, context))
        return await self._lifecycle.call(lambda: self._sync_reporting_receipts(request, context))

    async def _sync_reporting_receipts(self, request: Any, context: Any | None) -> dict[str, Any]:
        if self._receipt_handler is None:
            raise ReliableReportingConfigurationError("reporting receipt handling is disabled")
        caller = await self.caller_for(request, context)
        await self._require_grant(request, context, caller, "sync_reporting_receipts")
        return await self._receipt_handler.handle(
            _wire(request), account_id=caller.account_id, consumer_id=caller.consumer_id
        )

    async def get_revision_content(
        self, request: Any, context: Any | None = None
    ) -> dict[str, Any]:
        if self._production is not None:
            return _wire(await self._production.handler.get_media_buy_delivery(request, context))
        return await self._lifecycle.call(lambda: self._get_revision_content(request, context))

    async def _get_revision_content(self, request: Any, context: Any | None) -> dict[str, Any]:
        payload = _wire(request)
        revision_id = payload.get("reporting_revision_id")
        if not revision_id:
            raise LedgerConflictError(
                "MISSING_REVISION_ID", "an exact revision read requires reporting_revision_id"
            )
        caller = await self.caller_for(request, context)
        await self._require_grant(request, context, caller, "get_media_buy_delivery")
        cursor = (payload.get("pagination") or {}).get("cursor")
        limit = int((payload.get("pagination") or {}).get("limit") or 500)
        if limit < 1 or limit > 1000:
            raise LedgerConflictError(
                "INVALID_PAGE_SIZE", "pagination.limit must be between 1 and 1000"
            )
        if cursor:
            selected = decode_cursor(cursor).get("revision")
            if selected != revision_id:
                raise LedgerConflictError(
                    "CURSOR_REVISION_MISMATCH",
                    "this cursor belongs to a different reporting revision",
                )
        revision_view = await ReportingStatusHandler(self.store).handle(
            {"view": "revision", "reporting_revision_id": revision_id},
            caller=caller,
        )
        revision = revision_view["revision"]
        page = await self.store.read_revision_rows(
            account_id=caller.account_id,
            reporting_revision_id=revision_id,
            cursor=cursor,
            limit=limit,
        )
        period = revision["period"]
        response: dict[str, Any] = {
            "status": "completed",
            "reporting_period": {"start": period["start"], "end": period["end"]},
            "media_buy_deliveries": [],
            "reporting_revision_binding": {
                "reporting_revision_id": revision_id,
                "row_count": revision["row_count"],
                "control_totals": revision["control_totals"],
                "content_sha256": revision["revision_content_sha256"],
            },
            "reporting_revision": revision,
            "reporting_rows": [dict(row) for row in page.rows],
            "pagination": {
                "total_count": page.total_count,
                "has_more": page.has_more,
                **({"cursor": page.cursor} if page.cursor else {}),
            },
        }
        if "context" in payload:
            response["context"] = payload["context"]
        return response

    def capability_block(self, *, adcp_version: str | None = None) -> dict[str, Any]:
        """Project only components and offerings this service actually installed.

        ``adcp_version`` is the release the response is served at; it decides
        which reporting-subscription fields appear when subscriptions are served.
        """
        block = self._capability_block()
        if self._subscriptions is None or not block:
            return block
        from adcp.reporting.service_subscriptions import subscription_capability_block

        try:
            return subscription_capability_block(
                block, self._subscriptions, adcp_version=adcp_version
            )
        except (KeyError, ValueError) as error:
            raise ReliableReportingConfigurationError(str(error)) from None

    def _capability_block(self) -> dict[str, Any]:
        if self._production is not None:
            return dict(self._production._declared_capabilities()["reporting_delivery"])
        if self._production_options is not None:
            return self._production_options._capability_block(
                store=self.store,
                sources=self.sources,
                escalation=self._escalation,
                consumer_status_enabled=self._consumer_status_enabled,
            )
        offerings: dict[str, dict[str, Any]] = {}
        declarations = [
            item
            for name in self.sources.names
            for item in self.sources.get(name).capability_offerings
        ]
        # Preserve pre-registration callers that only supplied their offering
        # with each account context. New declarations are ready at first boot.
        declarations.extend(
            binding.context.capability_offering for binding in self._bindings.values()
        )
        for declaration in declarations:
            offering = ReportingDeliveryOffering.model_validate(_thaw(declaration)).model_dump(
                mode="json", exclude_none=True
            )
            offering_id = str(offering["offering_id"])
            existing = offerings.get(offering_id)
            if existing is not None and existing != offering:
                raise ReliableReportingConfigurationError(
                    f"capability offering {offering_id!r} has conflicting declarations"
                )
            offerings[offering_id] = offering
        if not offerings:
            return {}
        configurations = [binding.configuration for binding in self._bindings.values()]
        payload = _advertised_reporting_delivery(
            escalation=self._escalation,
            consumer_status_task=self._consumer_status_enabled,
            offerings=[offerings[key] for key in sorted(offerings)],
            automated_recovery_window=max(
                (item.automated_recovery_window for item in configurations),
                default=self._automated_recovery_window,
            ),
            status_retention_days=min(
                (item.status_retention_days for item in configurations),
                default=self._status_retention_days,
            ),
        )
        # The service owns these installed-component declarations; they are not
        # caller-provided producer extensions.
        payload["managed_delivery"] = self._materialization_worker is not None
        payload["reconciled_billing"] = self._reconciled_billing
        if self._reconciled_billing:
            payload["receipt_task"] = "sync_reporting_receipts"
        return payload

    def inject_capabilities(
        self, response: Any, *, adcp_version: str | None = None
    ) -> dict[str, Any]:
        """Merge the truthful reporting block into a base capability response."""
        payload = _wire(response)
        block = self.capability_block(adcp_version=adcp_version)
        if not block:
            return payload
        media_buy = dict(payload.get("media_buy") or {})
        media_buy["reporting_delivery"] = block
        payload["media_buy"] = media_buy
        protocols = [str(item) for item in payload.get("supported_protocols") or []]
        if "media_buy" not in protocols:
            protocols.append("media_buy")
        payload["supported_protocols"] = protocols
        experimental = [str(item) for item in payload.get("experimental_features") or []]
        if "media_buy.reporting_delivery" not in experimental:
            experimental.append("media_buy.reporting_delivery")
        payload["experimental_features"] = experimental
        if self._subscriptions is not None:
            from adcp.reporting.service_subscriptions import advertise_subscriptions

            try:
                payload = advertise_subscriptions(
                    payload, self._subscriptions, adcp_version=adcp_version
                )
            except ValueError as error:
                raise ReliableReportingConfigurationError(str(error)) from None
        return payload

    def install(self, platform: Any) -> Any:
        """Install reporting on an ADCPHandler or a DecisioningPlatform.

        Core decisioning installations retain the platform's account resolution,
        dispatcher and boot-time idempotency checks. Pass the returned platform
        to ``adcp.decisioning.serve`` as usual. Managed production graphs still
        bind an ADCPHandler and return their exact production handler; use
        ``create_adcp_server_from_platform`` first for that composition.
        """
        self._compose_production()
        if self._production is not None:
            self._production.handler.bind_application(platform)
            return self._production.handler
        from adcp.server import ADCPHandler
        from adcp.server.mcp_tools import get_tools_for_handler

        decisioning = not isinstance(platform, ADCPHandler)
        if decisioning:
            from adcp.decisioning.platform import DecisioningPlatform

            if not isinstance(platform, DecisioningPlatform):
                raise ReliableReportingConfigurationError(
                    "install() requires an ADCPHandler or DecisioningPlatform instance"
                )
        if getattr(platform, "_reliable_reporting_service", None) is self:
            return platform
        if getattr(platform, "_reliable_reporting_service", None) is not None:
            raise ReliableReportingConfigurationError(
                "this platform already has another ReliableReportingService installed"
            )
        original = type(platform)
        existing_tools = (
            set()
            if decisioning
            else {item["name"] for item in get_tools_for_handler(platform, _include_schemas=False)}
        )
        tools = set(existing_tools)
        tools.update(self.reporting_tools)

        service = self

        class ReportingInstallMixin:
            async def get_reporting_status(self, params: Any, context: Any | None = None) -> Any:
                return await installed_reporting_call(
                    "get_reporting_status",
                    lambda: service.get_reporting_status(params, context),
                    decisioning=decisioning,
                )

            async def sync_reporting_status(self, params: Any, context: Any | None = None) -> Any:
                if not service._consumer_status_enabled:
                    return await _resolve(
                        getattr(super(), "sync_reporting_status")(params, context)
                    )
                return await installed_reporting_call(
                    "sync_reporting_status",
                    lambda: service.sync_reporting_status(params, context),
                    decisioning=decisioning,
                )

            async def sync_reporting_receipts(self, params: Any, context: Any | None = None) -> Any:
                if service._receipt_handler is None:
                    return await _resolve(
                        getattr(super(), "sync_reporting_receipts")(params, context)
                    )
                return await installed_reporting_call(
                    "sync_reporting_receipts",
                    lambda: service.sync_reporting_receipts(params, context),
                    decisioning=decisioning,
                )

            async def _get_reporting_revision_content(
                self, params: Any, context: Any | None = None
            ) -> Any:
                return await installed_reporting_call(
                    "get_media_buy_delivery",
                    lambda: service.get_revision_content(params, context),
                    decisioning=decisioning,
                )

        attrs: dict[str, Any] = {"__module__": original.__module__}
        if not decisioning:

            async def get_adcp_capabilities(
                instance: Any, params: Any, context: Any | None = None
            ) -> Any:
                result = await _resolve(original.get_adcp_capabilities(instance, params, context))
                return service.inject_capabilities(
                    result, adcp_version=getattr(context, "resolved_adcp_version", None)
                )

            async def get_media_buy_delivery(
                instance: Any, params: Any, context: Any | None = None
            ) -> Any:
                if not _wire(params).get("reporting_revision_id"):
                    return await _resolve(
                        original.get_media_buy_delivery(instance, params, context)
                    )
                return await installed_reporting_call(
                    "get_media_buy_delivery",
                    lambda: service.get_revision_content(params, context),
                    decisioning=decisioning,
                )

            attrs.update(
                advertised_tools=tools,
                get_adcp_capabilities=get_adcp_capabilities,
                get_media_buy_delivery=get_media_buy_delivery,
            )
        reporting_methods = {
            "get_adcp_capabilities",
            "get_reporting_status",
            "sync_reporting_status",
            "sync_reporting_receipts",
            "get_media_buy_delivery",
        }
        for name in existing_tools:
            if name in reporting_methods:
                continue
            method = getattr(original, name, None)
            if method is None:
                continue

            def passthrough(
                method_name: str, original_method: Callable[..., Any]
            ) -> Callable[..., Any]:
                async def delegated(instance: Any, *args: Any, **kwargs: Any) -> Any:
                    return await _resolve(original_method(instance, *args, **kwargs))

                delegated.__name__ = method_name
                delegated.__qualname__ = f"ReliableReporting{original.__name__}.{method_name}"
                return delegated

            attrs[name] = passthrough(name, method)

        installed_name = f"ReliableReporting{original.__name__}_{id(self):x}"
        installed = type(
            installed_name,
            (ReportingInstallMixin, original),
            attrs,
        )
        try:
            platform.__class__ = installed
        except TypeError:
            # CPython rejects some otherwise ordinary multiple-inheritance
            # layout changes. The documented API returns the installed
            # platform, so preserve the instance state in a replacement.
            state = getattr(platform, "__dict__", None)
            if state is None:
                raise ReliableReportingConfigurationError(
                    "install() requires an ordinary mutable platform instance; slot-only/native "
                    "instances should wire the service handler methods explicitly"
                ) from None
            replacement: Any = object.__new__(installed)
            replacement.__dict__.update(state)
            platform = replacement
        setattr(platform, "_reliable_reporting_service", self)
        return platform

    @property
    def reporting_tools(self) -> frozenset[str]:
        """Core task implementations mounted by ``install`` (independent of readiness)."""
        tasks = {"get_reporting_status", "get_media_buy_delivery"}
        if self._consumer_status_enabled:
            tasks.add("sync_reporting_status")
        if self._receipt_handler is not None:
            tasks.add("sync_reporting_receipts")
        return frozenset(tasks)
