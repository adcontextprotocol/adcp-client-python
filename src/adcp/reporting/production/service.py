"""One live SDK composition for admitted reporting work and truthful discovery."""

from __future__ import annotations

import asyncio
import hashlib
import json
import weakref
from collections.abc import Mapping
from contextvars import ContextVar
from dataclasses import dataclass, replace
from datetime import timedelta
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from adcp.reporting._source_authorization import source_turn
from adcp.reporting.ledger.delivery_models import ReportingDestinationBinding
from adcp.reporting.ledger.models import ReportingConfiguration
from adcp.reporting.ledger.notification_models import ReportingNotificationError
from adcp.reporting.materializer.contracts import (
    ReportingDestinationResolver,
    ReportingDestinationWriter,
    ReportingVerificationKey,
    failure,
)
from adcp.reporting.materializer.reference import (
    ReferenceReportingDestinationWriter,
    ReferenceReportingResolver,
)
from adcp.reporting.materializer.service import ReportingMaterializerService
from adcp.reporting.materializer.verification import ReportingDestinationIO
from adcp.reporting.materializer.work import MaterializerContext, verification_key_id
from adcp.reporting.production._diagnostics import _worker_stopped, _WorkerBoundary
from adcp.reporting.production.configuration import (
    ReportingConfigurationAdmission,
    ReportingProductionConfigurationTask,
)
from adcp.reporting.production.contracts import (
    ReportingProductionDestinationBinding,
    ReportingProductionMethod,
    ReportingProductionSourceBinding,
)
from adcp.reporting.production.memory import InMemoryReportingProductionStore
from adcp.reporting.production.offerings import ReportingProductionOffering
from adcp.reporting.production.source_registry import (
    ReportingProductionSourceContext,
    ReportingProductionSourceRegistry,
)
from adcp.reporting.projection.memory import InMemoryReportingStatusProjection
from adcp.reporting.receipts.handler import ReceiptAccountResolver

if TYPE_CHECKING:
    from adcp.decisioning.registry import BuyerAgentRegistry
    from adcp.reporting.ledger.producer import ReportingProducer
    from adcp.reporting.outbox.worker import ReportingNotificationWorker
    from adcp.reporting.production.pg import PgReportingProductionStore
    from adcp.reporting.projection.pg import PgReportingStatusProjection
    from adcp.reporting.service_lifecycle import _ServiceLifecycle
    from adcp.server.base import ADCPHandler


@runtime_checkable
class ReportingProductionDestination(
    ReportingDestinationWriter, ReportingDestinationResolver, Protocol
):
    """An actual provider's bounded retention and revocation commitments.

    These promises accompany the writer's exact capabilities. Every write and
    independent readback still opens a separately authorized SDK session. A
    service cannot make the development writer eligible by decorating it.
    """

    @property
    def resource_retention_days(self) -> int: ...

    @property
    def authorization_revocation_seconds(self) -> int: ...

    @property
    def delivery_methods(self) -> tuple[ReportingProductionMethod, ...]: ...

    def configuration_binding(
        self, binding: ReportingDestinationBinding
    ) -> ReportingProductionDestinationBinding | None: ...


def _method_ready(
    writer: ReportingDestinationWriter, offering: ReportingProductionOffering
) -> bool:
    return (
        isinstance(writer, ReportingProductionDestination)
        and offering.verification_key.capability in writer.capabilities
        and any(
            type(method) is ReportingProductionMethod
            and method.capability == offering.verification_key.capability
            and method.wire() == offering.wire()["method"]
            for method in writer.delivery_methods
        )
    )


def production_owner(
    store: InMemoryReportingProductionStore | PgReportingProductionStore,
) -> ReportingProductionSupport:
    reference = store._production_support
    owner = reference() if reference is not None else None
    if owner is None or owner.store is not store:
        raise ReportingNotificationError("reporting_production_component_unready")
    owner._assert_components()
    return owner


@dataclass(frozen=True)
class _MountedProduction:
    mount: weakref.ReferenceType[Any]
    dispatcher: weakref.ReferenceType[Any]
    transport: str
    entries: dict[str, tuple[int, int]]

    def current(self) -> dict[str, tuple[int, int]]:
        dispatcher = self.dispatcher()
        if self.mount() is None or dispatcher is None:
            return {}
        try:
            if self.transport == "mcp":
                return {
                    name: (id(tool), id(tool.fn))
                    for name, tool in dispatcher._tool_manager._tools.items()
                }
            if self.transport == "a2a":
                return {name: (id(fn), id(fn)) for name, fn in dispatcher._tool_callers.items()}
        except (AttributeError, TypeError):
            return {}
        return {}


def register_production_mount(
    handler: ADCPHandler[Any], mount: object, *, transport: str, dispatcher: object
) -> None:
    """Called only after the SDK has installed the transport's real dispatch map."""
    from adcp.reporting.production.handler import ReportingProductionHandler

    if type(handler) is ReportingProductionHandler:
        proof = _MountedProduction(weakref.ref(mount), weakref.ref(dispatcher), transport, {})
        proof.entries.update(proof.current())
        handler.production._mounts.append(proof)


class ReportingProductionSupport:
    """Compose, mount, start, then activate accounts after draining old workers.

    The owned worker performs indexed producer/materializer/projector turns.
    Stopping it withdraws production claims and prevents fresh admission. An
    optional HTTP notification worker is independent of polling readiness;
    enabled logical enqueue is always part of the materializer transaction.

    Migrate with ``store.create_schema()`` before construction/start. For DDL,
    drain and close this support, migrate, and construct fresh support. Schema
    proofs do not detect arbitrary serving-time DDL or search-path changes.
    """

    def __init__(
        self,
        materializer: ReportingMaterializerService,
        projection: InMemoryReportingStatusProjection | PgReportingStatusProjection,
        *,
        offerings: tuple[ReportingProductionOffering, ...],
        configuration_task: ReportingProductionConfigurationTask,
        resolve_account: ReceiptAccountResolver,
        buyer_agents: BuyerAgentRegistry | None = None,
        automated_recovery_window: timedelta = timedelta(hours=6),
        status_retention_days: int = 400,
        notification_workers: tuple[ReportingNotificationWorker, ...] = (),
        poll_seconds: float = 0.25,
        adcp_version: str | None = None,
        source_registry: ReportingProductionSourceRegistry | None = None,
    ) -> None:
        from adcp.reporting.outbox.worker import ReportingNotificationWorker
        from adcp.reporting.production.handler import ReportingProductionHandler
        from adcp.reporting.production.pg import PgReportingProductionStore
        from adcp.reporting.projection.pg import PgReportingStatusProjection

        if (
            type(materializer) is not ReportingMaterializerService
            or type(materializer.store)
            not in {InMemoryReportingProductionStore, PgReportingProductionStore}
            or type(projection)
            not in {InMemoryReportingStatusProjection, PgReportingStatusProjection}
            or projection.ledger is not materializer.store
            or type(configuration_task) is not ReportingProductionConfigurationTask
            or type(offerings) is not tuple
            or not offerings
            or any(type(o) is not ReportingProductionOffering for o in offerings)
            or len({o.offering_id for o in offerings}) != len(offerings)
            or type(status_retention_days) is not int
            or not 1 <= status_retention_days <= 36500
            or automated_recovery_window <= timedelta(0)
            or not 0.01 <= poll_seconds <= 60
            or type(notification_workers) is not tuple
            or any(type(w) is not ReportingNotificationWorker for w in notification_workers)
        ):
            raise ValueError(
                "production support requires exact SDK components and bounded promises"
            )
        assert isinstance(
            materializer.store, (InMemoryReportingProductionStore, PgReportingProductionStore)
        )
        self.store = materializer.store
        previous = self.store._production_support
        if previous is not None and previous() is not None:
            raise ReportingNotificationError("reporting_production_owner_conflict")
        self.materializer, self.projection = materializer, projection
        self.offerings, self.configuration_task = offerings, configuration_task
        if source_registry is not None:
            if type(source_registry) is not ReportingProductionSourceRegistry:
                raise ValueError("production requires an exact source registry")
            source_registry.freeze(offerings)
        self.source_registry = source_registry
        self._service_lifecycle: _ServiceLifecycle | None = None
        self.automated_recovery_window, self.status_retention_days = (
            automated_recovery_window,
            status_retention_days,
        )
        self.notification_workers, self.poll_seconds = notification_workers, poll_seconds
        from adcp.reporting.production.notifications import worker_identity

        self._notification_identity = tuple(worker_identity(w) for w in notification_workers)
        self.handler = ReportingProductionHandler(
            self,
            resolve_account=resolve_account,
            buyer_agents=buyer_agents,
            adcp_version=adcp_version,
        )
        self._mounts: list[_MountedProduction] = []
        self._task: asyncio.Task[None] | None = None
        self._notification_task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()
        self._started = asyncio.Event()
        self._notification_started = asyncio.Event()
        self._closed = False
        self._failed = False
        self._producer_turn: ContextVar[ReportingProducer | None] = ContextVar(
            "reporting_production_source_turn", default=None
        )
        self._schema_task: asyncio.Task[bool] | None = None
        self._schema_epoch = 0
        self._schema_positive: tuple[int, int] | None = None
        self._protocol_version = self.handler.get_adcp_version()
        self._components = self._component_ids()
        self._methods = self._handler_methods()
        self.store._production_support = weakref.ref(self)
        self._assert_components(running=False, mounted=False)

    @property
    def keys(self) -> tuple[ReportingVerificationKey, ...]:
        return tuple(dict.fromkeys(o.verification_key for o in self.offerings))

    @property
    def notifications_enabled(self) -> bool:
        return bool(self.projection.policy["notifications_enabled"])

    def _component_ids(self) -> tuple[int, ...]:
        return tuple(
            id(v)
            for v in (
                self.store,
                getattr(self.store, "_pool", None),
                self.materializer,
                self.materializer.io,
                self.materializer.io.registry,
                self.materializer.io.resolver,
                self.materializer.writer,
                self.projection,
                self.projection.outbox,
                self.configuration_task,
                self.source_registry,
                *self.offerings,
                *self.notification_workers,
            )
        )

    def _handler_methods(self) -> tuple[object, ...]:
        return tuple(
            getattr(getattr(self.handler, name), "__func__", None)
            for name in (
                "get_reporting_status",
                "get_media_buy_delivery",
                "sync_reporting_receipts",
                "sync_reporting_status",
                "sync_accounts",
                "get_adcp_capabilities",
                "get_adcp_version",
            )
        )

    def _mounted(self, *, receipts: bool = False) -> bool:
        required = {
            "get_adcp_capabilities",
            "get_reporting_status",
            "get_media_buy_delivery",
            "sync_accounts",
        }
        if receipts:
            required.add("sync_reporting_receipts")
        if self.projection.consumer_status_enabled:
            required.add("sync_reporting_status")
        live = [proof for proof in self._mounts if proof.mount() is not None]
        return bool(live) and all(
            required <= proof.entries.keys()
            and all(proof.current().get(name) == proof.entries[name] for name in required)
            for proof in live
        )

    def _assert_components(self, *, running: bool = True, mounted: bool = True) -> None:
        from adcp.reporting.production.notifications import check_workers

        check_workers(self)
        writer = self.materializer.writer
        if (
            self._closed
            or self._failed
            or (running and (self._task is None or self._task.done()))
            or (
                running
                and self.notification_workers
                and (self._notification_task is None or self._notification_task.done())
            )
            or self._components != self._component_ids()
            or self._methods != self._handler_methods()
            or type(self.handler._adcp_version) is not str
            or self.handler._adcp_version != self._protocol_version
            or (
                (running or mounted)
                and not isinstance(self.store, InMemoryReportingProductionStore)
                and self.store._pool.closed is not False
            )
            or self.materializer.store is not self.store
            or self.projection.ledger is not self.store
            or not self._projection_outbox_linked()
            or self.handler.receipt_store is not self.store
            or self.handler.reporting_feed_store is not self.store
            or self.handler._feed_consumer_status_enabled != self.projection.consumer_status_enabled
            or self.store._projection_read_policy != self.projection.policy
            or type(self.materializer.io) is not ReportingDestinationIO
            or isinstance(writer, ReferenceReportingDestinationWriter)
            or isinstance(self.materializer.io.resolver, ReferenceReportingResolver)
            or not isinstance(writer, ReportingProductionDestination)
            or writer is not self.materializer.io.resolver
            or writer.production_eligible is not True
            or type(writer.resource_retention_days) is not int
            or not 1 <= writer.resource_retention_days <= 36500
            or type(writer.authorization_revocation_seconds) is not int
            or not 0 <= writer.authorization_revocation_seconds <= 86400
            or (mounted and not self._mounted())
        ):
            raise ReportingNotificationError("reporting_production_component_unready")

    def _projection_outbox_linked(self) -> bool:
        # The projection queue participates even when optional HTTP delivery
        # workers are absent. Its connection ownership cannot ride a cached
        # catalog proof belonging to a different pool or in-memory ledger.
        if isinstance(self.store, InMemoryReportingProductionStore):
            from adcp.reporting.projection.memory import InMemoryReportingProjectionOutbox

            return (
                type(self.projection.outbox) is InMemoryReportingProjectionOutbox
                and self.projection.outbox._store is self.store
            )
        from adcp.reporting.projection.notifications import PgReportingProjectionOutbox

        return (
            type(self.projection.outbox) is PgReportingProjectionOutbox
            and self.projection.outbox._pool is self.store._pool
        )

    def _offering_ready(self, offering: ReportingProductionOffering) -> bool:
        try:
            if (
                offering.producer.store is not self.store
                or type(offering.producer._max_periods_per_turn) is not int
                or not 1 <= offering.producer._max_periods_per_turn <= 64
                or offering.producer.escalation != self.projection.escalation
                or not _method_ready(self.materializer.writer, offering)
                or (offering.reconciled and not self._mounted(receipts=True))
            ):
                return False
            verifier = self.materializer.io.registry.require(offering.verification_key)
            profile = offering.wire()["reporting_profile"]
            contract = json.loads(verifier.canonicalization_bytes)
            definition = json.loads(verifier.definition_bytes)
            if (
                offering.producer._revision_verifier is not verifier
                or profile["primary_keys"] != contract["primary_keys"]
                or profile["grain"] != definition.get("grain")
            ):
                return False
            offering.check_source()
            if self.source_registry is not None:
                self.source_registry._registration(offering)
            return True
        except Exception:
            return False

    def _admission_policy(self) -> dict[str, Any]:
        self._assert_components()
        # This fixes the installed contract, not its current authorization.
        # Each reservation separately proves its own offering; an unrelated
        # provider outage must not rewrite this epoch or veto healthy offerings.
        ready = self.offerings
        return {
            "version": 2,
            "verification_keys": sorted({verification_key_id(o.verification_key) for o in ready}),
            "offerings": sorted(hashlib.sha256(o._wire).hexdigest() for o in ready),
            "producers": sorted(o._producer_key for o in ready),
            "notifications_enabled": self.notifications_enabled,
            "projection": self.projection.policy,
        }

    def _configuration_offering(
        self,
        configuration: ReportingConfiguration,
        binding: ReportingDestinationBinding,
        *,
        offering_id: str | None = None,
        producer_key: str | None = None,
    ) -> ReportingProductionOffering:
        self._assert_components()
        writer = self.materializer.writer
        assert isinstance(writer, ReportingProductionDestination)
        matches = []
        for offering in self.offerings:
            if (offering_id is not None and offering.offering_id != offering_id) or (
                producer_key is not None and offering._producer_key != producer_key
            ):
                continue
            if not self._offering_ready(offering):
                continue
            try:
                offering.check_configuration(
                    configuration, binding, retention_days=writer.resource_retention_days
                )
                self._destination_binding(binding, offering)
            # A failed proof never qualifies this offering.
            except Exception:  # nosec B112
                continue
            matches.append(offering)
        if (
            len(matches) != 1
            or configuration.automated_recovery_window != self.automated_recovery_window
            or configuration.status_retention_days < self.status_retention_days
        ):
            raise failure("BINDING_MISMATCH")
        return matches[0]

    def _destination_binding(
        self, binding: ReportingDestinationBinding, offering: ReportingProductionOffering
    ) -> ReportingProductionDestinationBinding:
        try:
            writer = self.materializer.writer
            assert isinstance(writer, ReportingProductionDestination)
            resolved = writer.configuration_binding(binding)
            if (
                type(resolved) is not ReportingProductionDestinationBinding
                or resolved.binding != binding
                or resolved.method.capability != offering.verification_key.capability
                or resolved.method.wire() != offering.wire()["method"]
            ):
                raise failure("BINDING_MISMATCH")
            return resolved
        except Exception:
            raise failure("BINDING_MISMATCH") from None

    def _producer_keys(self) -> tuple[str, ...]:
        self._assert_components()
        producer = self._producer_turn.get()
        return tuple(
            o._producer_key
            for o in self.offerings
            if o.producer is producer and self._offering_ready(o)
        )

    def validate_configuration(self, value: ReportingConfigurationAdmission) -> None:
        value.check(self)

    def _source_binding(
        self,
        configuration: ReportingConfiguration,
        producer_key: str,
        *,
        document: dict[str, Any] | None = None,
        service_context: ReportingProductionSourceContext | None = None,
    ) -> ReportingProductionSourceBinding:
        self._assert_components()
        matches = [
            offering
            for offering in self.offerings
            if offering._producer_key == producer_key and self._offering_ready(offering)
        ]
        if len(matches) != 1:
            raise failure("BINDING_MISMATCH")
        offering = matches[0]
        binding = offering.source_binding(configuration)
        if self.source_registry is not None:
            context = self.source_registry.recover(
                configuration,
                offering,
                offering.check_source(effective=True),
                (
                    service_context.document()
                    if service_context is not None
                    else (document or {}).get("service_context")
                ),
            )
            binding = replace(binding, service_context=context)
        elif service_context is not None or (document or {}).get("service_context") is not None:
            raise failure("BINDING_MISMATCH")
        return binding

    async def _admit_configuration(self, value: ReportingConfigurationAdmission) -> None:
        context = None
        if self.source_registry is not None:
            offering = self._configuration_offering(
                value.configuration, value.binding, offering_id=value.offering_id
            )
            context = await self.source_registry.resolve(
                value.configuration, offering, offering.check_source(effective=True)
            )
        await self.store.admit_production_configuration(
            value.configuration,
            value.binding,
            offering_id=value.offering_id,
            service_context=context,
        )

    def _check_source_binding(
        self,
        configuration: ReportingConfiguration,
        producer_key: str,
        document: dict[str, Any] | None,
    ) -> ReportingProductionSourceBinding:
        try:
            binding = self._source_binding(configuration, producer_key, document=document)
        except (ValueError, TypeError):
            # Runtime incompatibility follows the existing scoped refusal path:
            # unavailable legacy/profile facts cannot stop unrelated work.
            raise failure("BINDING_MISMATCH") from None
        if binding.document() != document:
            raise failure("BINDING_MISMATCH")
        return binding

    def _check_context(
        self,
        context: MaterializerContext,
        key: ReportingVerificationKey,
        policy: dict[str, Any],
        producer_key: str | None,
        source_binding: dict[str, Any] | None,
        destination_binding: dict[str, Any] | None,
    ) -> None:
        if producer_key is None:
            raise failure("BINDING_MISMATCH")
        offering = self._configuration_offering(
            context.configuration, context.binding, producer_key=producer_key
        )
        self._check_source_binding(context.configuration, producer_key, source_binding)
        if self._destination_binding(context.binding, offering).wire() != destination_binding:
            raise failure("BINDING_MISMATCH")
        if (
            policy != self._admission_policy()
            or offering.verification_key != key
            or verification_key_id(key) not in policy["verification_keys"]
            or context.obligation.generation_key != context.configuration.generation_key
        ):
            raise failure("BINDING_MISMATCH")

    def invalidate_schema_validation(self) -> None:
        """Drain first; an older scan cannot publish a new epoch's proof."""
        self._schema_epoch += 1
        self._schema_positive = None
        self._schema_task = None

    async def _schema_ready(self) -> bool:
        from adcp.reporting.production.pg import PgReportingProductionStore
        from adcp.reporting.production.schema import validate_production_schema

        if not isinstance(self.store, PgReportingProductionStore):
            return False
        store = self.store
        pool, epoch = store._pool, self._schema_epoch
        identity = (id(pool), epoch)
        if self._schema_positive == identity:
            return True
        task = self._schema_task
        if task is None:

            async def scan() -> bool:
                try:
                    async with pool.connection() as connection:
                        await validate_production_schema(
                            connection,
                            notifications=self.notifications_enabled,
                            service_context=self.source_registry is not None,
                        )
                    if epoch == self._schema_epoch and pool is store._pool:
                        self._schema_positive = identity
                        return True
                    return False
                finally:
                    if self._schema_task is asyncio.current_task():
                        self._schema_task = None

            task = asyncio.create_task(scan())
            self._schema_task = task
            task.add_done_callback(lambda done: None if done.cancelled() else done.exception())
        if task.get_loop() is not asyncio.get_running_loop():
            return False
        return await asyncio.shield(task)

    async def start(self) -> None:
        if self._task is not None:
            self._assert_components()
            return
        self._assert_components(running=False)
        if (
            not isinstance(self.store, InMemoryReportingProductionStore)
            and not await self._schema_ready()
        ):
            raise ReportingNotificationError("reporting_production_schema_unready")
        self._assert_components(running=False)
        self._task = asyncio.create_task(self._run(), name="adcp-reporting-production")
        if self.notification_workers:
            self._notification_task = asyncio.create_task(
                self._run_notifications(), name="adcp-reporting-production-notifications"
            )
        await self._started.wait()
        if self.notification_workers:
            await self._notification_started.wait()
        self._assert_components()

    async def _run(self) -> None:
        boundary: _WorkerBoundary = "producer"
        try:
            while not self._stop.is_set():
                # Each producer leases a generation through the reviewed fair
                # indexed acquisition path; no account enumeration is required.
                with source_turn():
                    for producer in dict.fromkeys(o.producer for o in self.offerings):
                        boundary = "producer"
                        token = self._producer_turn.set(producer)
                        try:
                            await producer.run_worker()
                        finally:
                            self._producer_turn.reset(token)
                boundary = "materializer"
                await self.materializer.run_once()
                boundary = "projection"
                await self.projection.rebuild_one()
                boundary = "sweeper"
                await self.projection.sweep_one()
                self._started.set()
                try:
                    await asyncio.wait_for(self._stop.wait(), self.poll_seconds)
                except asyncio.TimeoutError:
                    pass
        except asyncio.CancelledError:
            self._stop.set()
            raise
        except Exception as error:
            already_stopping = self._stop.is_set()
            self._failed = True
            self._stop.set()
            if not already_stopping and not isinstance(error, ReportingNotificationError):
                _worker_stopped(boundary=boundary)
        finally:
            self._started.set()

    async def _run_notifications(self) -> None:
        from adcp.reporting.production.notifications import notification_turn

        try:
            while not self._stop.is_set():
                await notification_turn(self)
                self._notification_started.set()
                try:
                    await asyncio.wait_for(self._stop.wait(), self.poll_seconds)
                except asyncio.TimeoutError:
                    pass
        except asyncio.CancelledError:
            self._stop.set()
            raise
        except Exception as error:
            already_stopping = self._stop.is_set()
            self._failed = True
            self._stop.set()
            if not already_stopping and not isinstance(error, ReportingNotificationError):
                _worker_stopped(boundary="notifications")
        finally:
            self._notification_started.set()

    async def _check_notifications(self, account_id: str) -> None:
        from adcp.reporting.production.notifications import check_account_notifications

        self._assert_components()
        try:
            await check_account_notifications(self, account_id)
        except Exception:
            raise ReportingNotificationError("notification_chain_unready") from None
        self._assert_components()

    async def activate(self, *, account_id: str) -> bool:
        await self._check_notifications(account_id)
        await self.projection.activate(account_id=account_id)
        return await self.store._activate_production(account_id=account_id)

    async def aclose(self) -> None:
        self._stop.set()
        task = self._task
        if task is not None:
            # Drain short database turns instead of interrupting psycopg's
            # transaction entry/exit. Long I/O already has SDK-owned deadlines.
            await asyncio.gather(task, return_exceptions=True)
        if self._notification_task is not None:
            await asyncio.gather(self._notification_task, return_exceptions=True)
        self._notification_task = None
        self._task = None
        self._closed = True
        proof = self._schema_task
        if proof is not None:
            await asyncio.gather(proof, return_exceptions=True)
        if self.store._production_support is not None and self.store._production_support() is self:
            self.store._production_support = None

    async def reporting_delivery(self, *, extra: Mapping[str, Any] | None = None) -> dict[str, Any]:
        # Validate protected extra even when the concrete surface is unready.
        producer = self.offerings[0].producer
        payload = producer.advertised_reporting_delivery(
            consumer_status_task=self.projection.consumer_status_enabled,
            offerings=[],
            automated_recovery_window=self.automated_recovery_window,
            status_retention_days=self.status_retention_days,
            extra=extra,
        )
        try:
            if self._stop.is_set():
                return {}
            self._assert_components()
            if not await self._schema_ready():
                return {}
            # Catalog proof is cached; live component/mount ownership is not.
            # It must still hold after an awaited first scan or invalidation.
            self._assert_components()
            offerings = [o for o in self.offerings if self._offering_ready(o)]
            if not offerings:
                return {}
            writer = self.materializer.writer
            assert isinstance(writer, ReportingProductionDestination)
            payload.update(
                offerings=[o.wire() for o in offerings],
                managed_delivery=True,
                resource_retention_days=writer.resource_retention_days,
                authorization_revocation_seconds=writer.authorization_revocation_seconds,
            )
            if any(o.reconciled for o in offerings):
                payload.update(reconciled_billing=True, receipt_task="sync_reporting_receipts")
            if self.notification_workers:
                payload.update(
                    ledger_notification="reporting.ledger_changed",
                    readiness_notification="reporting.delivery_ready",
                    status_notification="reporting.status_changed",
                )
            return payload
        except Exception:
            return {}
