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

if TYPE_CHECKING:
    from adcp.reporting.production.service import ReportingProductionSupport

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


class ReliableReportingConfigurationError(ValueError):
    """The requested service composition cannot make truthful guarantees."""


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


class ReportingAdapterRegistry:
    """Named source adapters available to one service process."""

    def __init__(self, *, clock: Callable[[], datetime]) -> None:
        self._clock = clock
        self._registrations: dict[str, AdapterRegistration] = {}
        self._frozen = False

    def register(
        self,
        name: str,
        adapter: ReportingAdapter,
        *,
        staging: ReportingStagingStore | None = None,
        seals: ReportingSealStore | None = None,
    ) -> AdapterRegistration:
        """Register a small adapter and wrap it in the manifest executor."""
        if not isinstance(adapter, ReportingAdapter):
            raise TypeError("adapter must expose capabilities and fetch_slice(request)")
        staging = staging or InMemoryStagingStore()
        executor = InlineReportingSource(
            capabilities=adapter.capabilities,
            fetch=adapter.fetch_slice,
            staging=staging,
            seals=seals or InMemorySealStore(),
            clock=self._clock,
        )
        return self.register_executor(
            name,
            executor,
            object_reader=staging,
            adapter=adapter,
        )

    def register_executor(
        self,
        name: str,
        executor: ReportingSourceExecutor,
        *,
        object_reader: ReportingStagingStore | None,
        adapter: ReportingAdapter | None = None,
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

    @property
    def did_work(self) -> bool:
        return (
            any(turn.did_work for turn in self.configurations.values())
            or bool(self.extension_results)
            or bool(self.configuration_errors)
            or bool(self.extension_errors)
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
        materialization_worker: ReportingBackgroundWorker | None = None,
        notification_worker: ReportingBackgroundWorker | None = None,
        notification_attempt_store: Any | None = None,
        receipt_handler: ReportingReceiptHandler | None = None,
        reconciled_billing: bool = False,
        worker_error_handler: ReportingWorkerErrorHandler | None = None,
        owned_resources: Sequence[ReportingServiceResource] = (),
    ) -> None:
        effective_clock = clock or (lambda: datetime.now(timezone.utc))
        self.store = store
        self._production: ReportingProductionSupport | None = None
        self.sources = ReportingAdapterRegistry(clock=effective_clock)
        self._context_resolver = account_context
        self._caller_resolver = caller_resolver or self._default_caller
        self._consumer_status_enabled = consumer_status_enabled
        self._escalation = escalation or ReportingDeliveryEscalation()
        self._clock = effective_clock
        self._worker_interval = worker_interval
        self._producer_factory = producer_factory
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
        self._turn_lock = asyncio.Lock()
        self._lifecycle = _ServiceLifecycle(
            self._initialize,
            resources=owned_resources,
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
        **kwargs: Any,
    ) -> ReliableReportingService:
        """Create a deterministic in-memory service for tests and pilots."""
        return cls(
            store=InMemoryReportingLedgerStore(clock=clock),
            account_context=account_context,
            caller_resolver=caller_resolver,
            clock=clock,
            **kwargs,
        )

    @classmethod
    def postgres(
        cls,
        *,
        pool: Any,
        account_context: ReportingContextResolver,
        caller_resolver: ReportingCallerResolver | None = None,
        clock: Callable[[], datetime] | None = None,
        **kwargs: Any,
    ) -> ReliableReportingService:
        """Create a production ledger service over a caller-owned async pool."""
        from adcp.reporting.ledger.pg import PgReportingLedgerStore

        return cls(
            store=PgReportingLedgerStore(pool=pool, clock=clock),
            account_context=account_context,
            caller_resolver=caller_resolver,
            clock=clock,
            **kwargs,
        )

    async def configure(self, configuration: ReportingConfiguration) -> None:
        """Resolve trusted account facts once and freeze this generation's route."""
        if self._production is not None:
            raise ReliableReportingConfigurationError(
                "production requires typed sync_accounts admission"
            )

        async def configure() -> None:
            async with self._configuration_lock:
                await self._configure(configuration)

        await self._lifecycle.call(configure, before_start=True)

    async def _configure(self, configuration: ReportingConfiguration) -> None:
        key = configuration.generation_key
        existing = self._bindings.get(key)
        if existing is not None:
            if existing.configuration != configuration:
                raise ReliableReportingConfigurationError(
                    f"configuration {key.delivery_config_id}@{key.delivery_config_version} "
                    f"for account {key.account_id!r} is already frozen with other facts"
                )
            return
        context = await _resolve(self._context_resolver(configuration))
        if not isinstance(context, ReportingAccountContext):
            raise TypeError("account_context must resolve ReportingAccountContext")
        if context.account_id != configuration.account_id:
            raise ReliableReportingConfigurationError(
                "resolved account context does not match the configuration account"
            )
        if context.account_timezone != configuration.account_timezone:
            raise ReliableReportingConfigurationError(
                "resolved account timezone does not match the configuration timezone"
            )
        registration = self.sources.get(context.adapter)
        self._validate_offerings(configuration, context, registration)
        producer = self._producer_factory(
            source=registration.executor,
            offerings=context.producer_offerings(),
            store=self.store,
            object_reader=registration.object_reader,
            escalation=self._escalation,
            worker_id=f"reporting-service:{context.adapter}",
            clock=self._clock,
        )
        binding = _Binding(configuration=configuration, context=context, producer=producer)
        if self._initialized:
            await self.store.put_configuration(configuration)
        else:
            self._pending_configurations[key] = configuration
        self._bindings[key] = binding

    def _validate_offerings(
        self,
        configuration: ReportingConfiguration,
        context: ReportingAccountContext,
        registration: AdapterRegistration,
    ) -> None:
        capabilities = registration.executor.capabilities
        if capabilities.scope != "effective_account":
            raise ReliableReportingConfigurationError(
                "registered source capabilities must be scoped to an effective account"
            )
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

    def validate(self) -> None:
        """Fail fast on tier combinations the configured components cannot honor."""
        if self._production is not None and self.sources.names:
            raise ReliableReportingConfigurationError(
                "production adapters must belong to its frozen source registry"
            )
        if self._worker_interval is not None and self._worker_interval <= timedelta(0):
            raise ReliableReportingConfigurationError("worker_interval must be greater than zero")
        if self._notification_worker is not None and self._notification_attempt_store is None:
            raise ReliableReportingConfigurationError(
                "a notification worker requires durable notification attempt storage"
            )
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

    async def initialize(self) -> None:
        """Prepare resources once; start/run_worker opens reporting admission."""
        await self._lifecycle.initialize()

    async def _initialize(self) -> None:
        self.validate()
        async with self._configuration_lock:
            await self.store.create_schema()
            for configuration in self._pending_configurations.values():
                await self.store.put_configuration(configuration)
            self._pending_configurations.clear()
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
        if self._production is not None:
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
            with source_turn():
                for key, binding in sorted(
                    self._bindings.items(),
                    key=lambda item: (
                        item[0].account_id,
                        item[0].delivery_config_id,
                        item[0].delivery_config_version,
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
        account_id = getattr(context, "account_id", None)
        consumer_id = getattr(context, "caller_identity", None)
        if not account_id or not consumer_id:
            raise ReliableReportingConfigurationError(
                "default caller resolution requires AccountAwareToolContext.account_id and "
                "authenticated caller_identity; provide caller_resolver for another trust model"
            )
        return ReportingStatusCaller(account_id=account_id, consumer_id=consumer_id)

    async def get_reporting_status(
        self, request: Any, context: Any | None = None
    ) -> dict[str, Any]:
        if self._production is not None:
            return _wire(await self._production.handler.get_reporting_status(request, context))
        return await self._lifecycle.call(lambda: self._get_reporting_status(request, context))

    async def _get_reporting_status(self, request: Any, context: Any | None) -> dict[str, Any]:
        caller = await self.caller_for(request, context)
        return await ReportingStatusHandler(
            self.store,
            consumer_status_enabled=self._consumer_status_enabled,
            escalation=self._escalation,
        ).handle(_wire(request), caller=caller)

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

    def capability_block(self) -> dict[str, Any]:
        """Project only components and offerings this service actually installed."""
        if not self.ready or not self._bindings:
            return {}
        offerings: dict[str, dict[str, Any]] = {}
        for binding in self._bindings.values():
            offering = _thaw(binding.context.capability_offering)
            offering_id = str(offering["offering_id"])
            existing = offerings.get(offering_id)
            if existing is not None and existing != offering:
                raise ReliableReportingConfigurationError(
                    f"capability offering {offering_id!r} has conflicting declarations"
                )
            offerings[offering_id] = offering
        configurations = [binding.configuration for binding in self._bindings.values()]
        first = next(iter(self._bindings.values())).producer
        payload = first.advertised_reporting_delivery(
            consumer_status_task=self._consumer_status_enabled,
            offerings=[offerings[key] for key in sorted(offerings)],
            automated_recovery_window=max(
                item.automated_recovery_window for item in configurations
            ),
            status_retention_days=min(item.status_retention_days for item in configurations),
        )
        # The service owns these installed-component declarations; they are not
        # caller-provided producer extensions.
        payload["managed_delivery"] = self._materialization_worker is not None
        payload["reconciled_billing"] = self._reconciled_billing
        if self._reconciled_billing:
            payload["receipt_task"] = "sync_reporting_receipts"
        return payload

    def inject_capabilities(self, response: Any) -> dict[str, Any]:
        """Merge the truthful reporting block into a base capability response."""
        payload = _wire(response)
        block = self.capability_block()
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
        return payload

    def install(self, platform: Any) -> Any:
        """Install ready-to-use reporting handlers on an existing platform instance."""
        if self._production is not None:
            self._production.handler.bind_application(platform)
            return self._production.handler
        from adcp.server import ADCPHandler
        from adcp.server.mcp_tools import get_tools_for_handler

        if not isinstance(platform, ADCPHandler):
            raise ReliableReportingConfigurationError(
                "install() requires an ADCPHandler instance; wire service methods explicitly "
                "for other frameworks"
            )
        if getattr(platform, "_reliable_reporting_service", None) is self:
            return platform
        if getattr(platform, "_reliable_reporting_service", None) is not None:
            raise ReliableReportingConfigurationError(
                "this platform already has another ReliableReportingService installed"
            )
        original = type(platform)
        existing_tools = {
            item["name"] for item in get_tools_for_handler(platform, _include_schemas=False)
        }
        tools = set(existing_tools)
        tools.update({"get_reporting_status", "get_media_buy_delivery"})
        if self._consumer_status_enabled:
            tools.add("sync_reporting_status")
        if self._receipt_handler is not None:
            tools.add("sync_reporting_receipts")

        service = self

        class ReportingInstallMixin:
            async def get_adcp_capabilities(self, params: Any, context: Any | None = None) -> Any:
                result = await _resolve(getattr(super(), "get_adcp_capabilities")(params, context))
                return service.inject_capabilities(result)

            async def get_reporting_status(self, params: Any, context: Any | None = None) -> Any:
                return await service.get_reporting_status(params, context)

            async def sync_reporting_status(self, params: Any, context: Any | None = None) -> Any:
                if not service._consumer_status_enabled:
                    return await _resolve(
                        getattr(super(), "sync_reporting_status")(params, context)
                    )
                return await service.sync_reporting_status(params, context)

            async def sync_reporting_receipts(self, params: Any, context: Any | None = None) -> Any:
                if service._receipt_handler is None:
                    return await _resolve(
                        getattr(super(), "sync_reporting_receipts")(params, context)
                    )
                return await service.sync_reporting_receipts(params, context)

            async def get_media_buy_delivery(self, params: Any, context: Any | None = None) -> Any:
                if not _wire(params).get("reporting_revision_id"):
                    return await _resolve(
                        getattr(super(), "get_media_buy_delivery")(params, context)
                    )
                return await service.get_revision_content(params, context)

        attrs: dict[str, Any] = {
            "__module__": original.__module__,
            "advertised_tools": tools,
        }
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
