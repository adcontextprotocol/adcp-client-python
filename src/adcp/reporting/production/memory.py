"""Reference production transactions for shared state-machine conformance."""

from __future__ import annotations

import json
import weakref
from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

from adcp.reporting.canonical_json import canonical_json_utf8_v1
from adcp.reporting.ledger.delivery_models import (
    ReportingDeliveryPrincipal,
    ReportingDestinationBinding,
    ReportingMaterializationRecord,
)
from adcp.reporting.ledger.models import (
    ReportingConfiguration,
    ReportingConfigurationGenerationKey,
    ReportingDeliveryEscalation,
    ReportingObligationRecord,
)
from adcp.reporting.ledger.notification_models import (
    ReportingDomainEvent,
    ReportingNotificationError,
)
from adcp.reporting.ledger.producer_progress import acquisition_state, check_next_period
from adcp.reporting.ledger.store import LedgerConflictError
from adcp.reporting.materializer.capture import ReportingMaterializerBoundary
from adcp.reporting.materializer.contracts import ReportingVerificationKey, failure
from adcp.reporting.materializer.work import MaterializerContext, ReportingMaterializerLease
from adcp.reporting.outbox.activity import ActivityRequest, ReportingActivityStore, WebhookAttempt
from adcp.reporting.outbox.memory import InMemoryReportingOutbox, NotificationState
from adcp.reporting.outbox.models import DeliveryLease
from adcp.reporting.production.contracts import ReportingProductionSourceBinding
from adcp.reporting.production.source_registry import ReportingProductionSourceContext
from adcp.reporting.production.store import (
    MaintenanceOperation,
    ReportingProductionProjection,
    SavedDeliveryWindow,
)
from adcp.reporting.projection.memory import (
    InMemoryReportingProjectionOutbox,
    InMemoryReportingProjectionStore,
    InMemoryReportingStatusProjection,
)
from adcp.reporting.source import ReportingConstituent

if TYPE_CHECKING:
    from adcp.reporting.materializer.memory import _Work
    from adcp.reporting.production.service import ReportingProductionSupport


@dataclass(frozen=True)
class _Admission:
    activated_at: datetime
    policy: dict[str, Any]


@dataclass(frozen=True)
class _SourceWork:
    obligation: ReportingObligationRecord
    turn: int = 0
    state: str = "pending"


class InMemoryReportingProductionOutbox(InMemoryReportingOutbox):
    """The ordinary SDK expansion/delivery state machine, in a separate collection."""

    _store: InMemoryReportingProductionStore

    def __init__(self, store: InMemoryReportingProductionStore) -> None:
        if store._production_outbox is None:
            raise ReportingNotificationError("notifications_disabled")
        self._store = store

    @property
    def _state(self) -> NotificationState:
        assert self._store._production_outbox is not None
        return self._store._production_outbox


class InMemoryReportingProductionStore(InMemoryReportingProjectionStore):
    """Matches production atomicity and epochs, without claiming durable storage."""

    _materializer_writer_epoch = 2
    production_support: weakref.ReferenceType[ReportingProductionSupport] | None = None

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._production_accounts: dict[str, _Admission] = {}
        self._production_delivery_windows: dict[
            tuple[str, str], tuple[str, str, datetime, datetime]
        ] = {}
        self._production_generations: dict[ReportingConfigurationGenerationKey, str] = {}
        self._production_source_bindings: dict[
            ReportingConfigurationGenerationKey, ReportingProductionSourceBinding
        ] = {}
        self._production_destination_bindings: dict[
            tuple[ReportingConfigurationGenerationKey, str], bytes
        ] = {}
        self._production_outbox = (
            NotificationState() if self._notification_state is not None else None
        )
        self._production_status_heads: dict[ReportingDeliveryPrincipal, int] = {}
        self._production_boundaries: list[ReportingMaterializerBoundary] = []
        self._production_closed: dict[ReportingConfigurationGenerationKey, datetime] = {}
        self._production_source_turns: dict[ReportingConfigurationGenerationKey, int] = {}
        self._production_source_work: dict[str, _SourceWork] = {}
        #: Reporting-subscription generations (owed sets) and each owed
        #: obligation's constituents, frozen at planning from the host's pool.
        self._production_owed_sets: set[ReportingConfigurationGenerationKey] = set()
        self._production_obligation_constituents: dict[str, tuple[ReportingConstituent, ...]] = {}

    def _owner(self) -> ReportingProductionSupport:
        from adcp.reporting.production.service import production_owner

        return production_owner(self)

    # -- the production store contract (adcp.reporting.production.store) ------

    @property
    def durable(self) -> bool:
        return False

    @property
    def storage_identity(self) -> object:
        return self

    @property
    def storage_open(self) -> bool:
        return True

    def is_sdk_production_store(self) -> bool:
        return type(self) is InMemoryReportingProductionStore

    def status_projection(
        self,
        *,
        consumer_status_enabled: bool,
        escalation: ReportingDeliveryEscalation | None,
        revision_ownership: bool,
    ) -> InMemoryReportingStatusProjection:
        return InMemoryReportingStatusProjection(
            self,
            consumer_status_enabled=consumer_status_enabled,
            escalation=escalation,
            revision_ownership=revision_ownership,
        )

    def is_sdk_status_projection(self, projection: object) -> bool:
        return type(projection) is InMemoryReportingStatusProjection

    def projection_linked(self, projection: ReportingProductionProjection) -> bool:
        return (
            type(projection.outbox) is InMemoryReportingProjectionOutbox
            and projection.outbox._store is self
        )

    def production_outboxes(
        self, projection: ReportingProductionProjection
    ) -> tuple[Any, Any, Any]:
        if type(self) is not InMemoryReportingProductionStore:
            raise ReportingNotificationError("notification_chain_unready")
        return (
            InMemoryReportingOutbox(self),
            projection.outbox,
            InMemoryReportingProductionOutbox(self),
        )

    def notification_workers_linked(self, workers: Sequence[Any]) -> bool:
        expected = (
            InMemoryReportingOutbox,
            InMemoryReportingProjectionOutbox,
            InMemoryReportingProductionOutbox,
        )
        return (
            all(getattr(w.outbox, "_store", None) is self for w in workers)
            and tuple(type(w.outbox) for w in workers) == expected
        )

    async def validate_production_schema(
        self, *, notifications: bool, service_context: bool
    ) -> None:
        """Memory has no catalog; it never advertises durability either."""

    async def read_delivery_window(
        self, key: tuple[str, str], *, now: datetime
    ) -> tuple[SavedDeliveryWindow | None, datetime]:
        async with self._lock:
            return self._production_delivery_windows.get(key), now

    async def reserve_delivery_attempt(
        self,
        activity: ReportingActivityStore,
        lease: DeliveryLease,
        *,
        request: ActivityRequest,
        now: datetime,
        queue: str,
        expected: tuple[str, str],
        horizon_seconds: int,
    ) -> tuple[WebhookAttempt | None, datetime | None, bool]:
        from adcp.reporting.production.delivery_window import inspect_window

        binding = lease.delivery.binding
        key = (binding.account_id, binding.idempotency_key)
        if not isinstance(activity, InMemoryReportingOutbox) or activity._store is not self:
            raise ReportingNotificationError("notification_retry_unready")
        async with self._mutation():
            saved = self._production_delivery_windows.get(key)
            deadline, expired = inspect_window(saved, expected, now)
            if expired:
                return None, deadline, True
            attempt = activity._reserve_attempt_locked(lease, request=request, now=now)
            if attempt is None:
                return None, deadline, False
            retained = self._production_delivery_windows.setdefault(
                key,
                (
                    *expected,
                    attempt.fired_at,
                    attempt.fired_at + timedelta(seconds=horizon_seconds),
                ),
            )
            return attempt, retained[3], False

    def maintenance_operations(self) -> tuple[MaintenanceOperation, ...]:
        return ()

    async def admit_production_configuration(
        self,
        configuration: ReportingConfiguration,
        binding: ReportingDestinationBinding,
        *,
        offering_id: str,
        service_context: ReportingProductionSourceContext | None = None,
    ) -> None:
        offering = self._owner()._configuration_offering(
            configuration, binding, offering_id=offering_id
        )
        async with self._mutation():
            await self.put_configuration(configuration)
            await self.put_destination_binding(binding)
            self._enroll(
                configuration, offering._producer_key, binding, service_context=service_context
            )

    async def admit_subscription_generation(
        self,
        configuration: ReportingConfiguration,
        binding: ReportingDestinationBinding,
        *,
        offering_id: str,
    ) -> None:
        """Install a reporting subscription's generation with its managed binding.

        An owed set has no generation-wide source denominator: each obligation
        freezes its own constituents (``commit_subscription_obligation``).
        Idempotent; a changed generation or binding is a conflict.
        """
        offering = self._owner()._subscription_offering(
            configuration, binding, offering_id=offering_id
        )
        async with self._mutation():
            await self.put_configuration(configuration)
            await self.put_destination_binding(binding)
            self._enroll_owed_set(configuration, offering._producer_key, binding)

    def _enroll_owed_set(
        self,
        configuration: ReportingConfiguration,
        producer_key: str,
        destination: ReportingDestinationBinding,
    ) -> None:
        owner = self._owner()
        previous = self._production_generations.setdefault(
            configuration.generation_key, producer_key
        )
        if previous != producer_key:
            raise ReportingNotificationError("reporting_production_source_conflict")
        offering = owner._subscription_offering(configuration, destination)
        key = configuration.generation_key
        if any(
            other != key and replace(other, offering_id=key.offering_id) == key
            for other in self._production_owed_sets
        ):
            # Status checkpoints hold one configuration scope per version.
            raise ReportingNotificationError("reporting_production_offering_conflict")
        document = canonical_json_utf8_v1(owner._destination_binding(destination, offering).wire())
        original = self._production_destination_bindings.setdefault(
            (configuration.generation_key, destination.consumer_id), document
        )
        if original != document:
            raise ReportingNotificationError("reporting_production_destination_conflict")
        self._production_owed_sets.add(configuration.generation_key)

    async def subscription_generation_admitted(self, configuration: ReportingConfiguration) -> bool:
        """Whether this exact generation is already admitted (it is bound once)."""
        async with self._lock:
            return (
                configuration.generation_key in self._production_owed_sets
                and self._configurations.get(configuration.generation_key) == configuration
            )

    async def commit_subscription_obligation(
        self,
        configuration: ReportingConfiguration,
        obligation: ReportingObligationRecord,
        constituents: tuple[ReportingConstituent, ...],
    ) -> ReportingObligationRecord:
        """Commit an owed obligation with the constituents it freezes, atomically."""
        if obligation.generation_key != configuration.generation_key or not constituents:
            raise LedgerConflictError("HISTORY_UNAVAILABLE", "owed obligation is unbound")
        async with self._mutation():
            self._check_source_generation(configuration)
            stored = await self.commit_obligation(obligation)
            frozen = self._production_obligation_constituents.setdefault(
                stored.reporting_obligation_id, constituents
            )
            if frozen != constituents:
                raise LedgerConflictError(
                    "HISTORY_UNAVAILABLE", "owed obligation constituents are immutable"
                )
            return stored

    async def production_owed_sets(
        self, producer_keys: Sequence[str]
    ) -> tuple[ReportingConfiguration, ...]:
        """Admitted owed-set generations of these producers, in key order."""
        async with self._lock:
            return tuple(
                self._configurations[key]
                for key in sorted(
                    self._production_owed_sets,
                    key=lambda k: (
                        k.account_id,
                        k.consumer_id,
                        k.delivery_config_id,
                        k.delivery_config_version,
                        k.offering_id or "",
                    ),
                )
                if key.account_id in self._production_accounts
                and self._production_generations.get(key) in producer_keys
                and key in self._configurations
                and not self._configurations[key].quarantined
            )

    def _enroll(
        self,
        configuration: ReportingConfiguration,
        producer_key: str,
        destination: ReportingDestinationBinding,
        *,
        service_context: ReportingProductionSourceContext | None = None,
    ) -> None:
        previous_context = self._production_source_bindings.get(configuration.generation_key)
        binding = self._owner()._source_binding(
            configuration,
            producer_key,
            service_context=service_context,
            document=previous_context.document() if previous_context is not None else None,
        )
        previous = self._production_generations.setdefault(
            configuration.generation_key, producer_key
        )
        previous_binding = self._production_source_bindings.setdefault(
            configuration.generation_key, binding
        )
        if previous != producer_key or previous_binding != binding:
            raise ReportingNotificationError("reporting_production_source_conflict")
        owner = self._owner()
        offering = owner._configuration_offering(
            configuration, destination, producer_key=producer_key
        )
        document = canonical_json_utf8_v1(owner._destination_binding(destination, offering).wire())
        original = self._production_destination_bindings.setdefault(
            (configuration.generation_key, destination.consumer_id), document
        )
        if original != document:
            raise ReportingNotificationError("reporting_production_destination_conflict")

    def _destination_document(self, binding: ReportingDestinationBinding) -> dict[str, Any] | None:
        raw = self._production_destination_bindings.get(
            (binding.generation_key, binding.consumer_id)
        )
        return dict(json.loads(raw)) if raw is not None else None

    def _configuration_lease_eligible(self, configuration: ReportingConfiguration) -> bool:
        if configuration.quarantined or configuration.owed_set:
            return False
        try:
            self._check_source_generation(configuration)
        except Exception:
            return False
        return True

    def _check_source_generation(self, configuration: ReportingConfiguration) -> None:
        producer_key = self._production_generations.get(configuration.generation_key)
        if configuration.owed_set:
            # The host's pool is checked at each dispatch and publication.
            owner = self._owner()
            # Planning commits outside a producer turn: any ready offering's
            # producer qualifies; dispatch re-checks within its own turn.
            ready = {o._producer_key for o in owner.offerings if owner._offering_ready(o)}
            if (
                configuration.account_id not in self._production_accounts
                or configuration.generation_key not in self._production_owed_sets
                or producer_key not in ready
                or self._configurations.get(configuration.generation_key) != configuration
            ):
                raise LedgerConflictError(
                    "HISTORY_UNAVAILABLE", "producer generation is unavailable"
                )
            return
        binding = self._production_source_bindings.get(configuration.generation_key)
        if (
            configuration.account_id not in self._production_accounts
            or producer_key not in self._owner()._producer_keys()
            or binding is None
            or self._configurations.get(configuration.generation_key) != configuration
        ):
            raise LedgerConflictError("HISTORY_UNAVAILABLE", "producer generation is unavailable")
        self._owner()._check_source_binding(configuration, producer_key, binding.document())

    def _wake_obligation(self, account_id: str, obligation_id: str) -> None:
        super()._wake_obligation(account_id, obligation_id)
        obligation = self._obligations.get(obligation_id)
        if (
            obligation is not None
            and obligation.account_id == account_id
            and obligation.generation_key in self._production_generations
        ):
            previous = self._production_source_work.get(obligation_id)
            self._production_source_work[obligation_id] = _SourceWork(
                obligation, previous.turn if previous else 0
            )

    async def producer_closed_through(
        self, configuration: ReportingConfiguration
    ) -> datetime | None:
        async with self._lock:
            self._check_source_generation(configuration)
            return self._production_closed.get(configuration.generation_key)

    async def producer_constituents(
        self, configuration: ReportingConfiguration, obligation: ReportingObligationRecord
    ) -> tuple[ReportingConstituent, ...]:
        async with self._lock:
            self._check_source_generation(configuration)
            if configuration.owed_set:
                frozen = self._production_obligation_constituents.get(
                    obligation.reporting_obligation_id
                )
                if obligation.generation_key != configuration.generation_key or frozen is None:
                    raise LedgerConflictError(
                        "HISTORY_UNAVAILABLE", "owed obligation constituents are unavailable"
                    )
                return frozen
            if obligation.generation_key != configuration.generation_key or set(
                obligation.media_buy_ids
            ) != set(configuration.media_buy_ids):
                raise LedgerConflictError("HISTORY_UNAVAILABLE", "source denominator differs")
            return self._production_source_bindings[configuration.generation_key].constituents()

    async def commit_producer_period(
        self,
        configuration: ReportingConfiguration,
        obligation: ReportingObligationRecord,
        *,
        previous_end: datetime | None,
    ) -> ReportingObligationRecord:
        check_next_period(configuration, obligation, previous_end)
        async with self._mutation():
            self._check_source_generation(configuration)
            current = self._production_closed.get(configuration.generation_key)
            if current != previous_end and (current is None or current < obligation.period.end):
                raise LedgerConflictError("HISTORY_UNAVAILABLE", "producer progress changed")
            stored = await self.commit_obligation(obligation)
            self._production_source_work.setdefault(
                stored.reporting_obligation_id, _SourceWork(stored)
            )
            self._production_closed[configuration.generation_key] = max(
                obligation.period.end, current or obligation.period.end
            )
            return stored

    async def next_producer_obligations(
        self, configuration: ReportingConfiguration, *, now: datetime, limit: int
    ) -> tuple[str, ...]:
        if type(limit) is not int or not 1 <= limit <= 64:
            raise ValueError("production acquisition limit must be in 1..64")
        async with self._lock:
            self._check_source_generation(configuration)
            candidates = sorted(
                (work.turn, work.obligation.reporting_obligation_id)
                for work in self._production_source_work.values()
                if work.obligation.generation_key == configuration.generation_key
                and work.obligation.period.end <= now
                and work.state == "pending"
            )[:limit]
            turn = self._production_source_turns.get(configuration.generation_key, 0)
            for offset, (_, identifier) in enumerate(candidates, 1):
                work = self._production_source_work[identifier]
                self._production_source_work[identifier] = _SourceWork(
                    work.obligation, turn + offset
                )
            self._production_source_turns[configuration.generation_key] = turn + len(candidates)
            return tuple(identifier for _, identifier in candidates)

    async def finish_producer_acquisition(
        self, configuration: ReportingConfiguration, *, reporting_obligation_id: str
    ) -> None:
        async with self._lock:
            self._check_source_generation(configuration)
            work = self._production_source_work[reporting_obligation_id]
            if work.obligation.generation_key != configuration.generation_key:
                raise LedgerConflictError("HISTORY_UNAVAILABLE", "producer generation differs")
            revisions = await self.list_revisions(
                account_id=configuration.account_id,
                reporting_obligation_id=reporting_obligation_id,
            )
            self._production_source_work[reporting_obligation_id] = _SourceWork(
                work.obligation, work.turn, acquisition_state(work.obligation, revisions)
            )

    async def activate_production(self, *, account_id: str) -> bool:
        owner = self._owner()
        async with self._mutation():
            projection = self._projection_accounts.get(account_id)
            if (
                projection is None
                or not projection.ready
                or projection.policy != owner.projection.policy
            ):
                raise ReportingNotificationError("status_projection_activation_required")
            policy = owner._admission_policy()
            current = self._production_accounts.get(account_id)
            if current is not None:
                if current.policy != policy:
                    raise ReportingNotificationError("reporting_production_policy_conflict")
                return False
            self._production_accounts[account_id] = _Admission(self._clock(), policy)
            for _, _, record in self._retained_delivery_records():
                if (
                    isinstance(record, ReportingDestinationBinding)
                    and record.principal.account_id == account_id
                ):
                    configuration = self._configurations[record.generation_key]
                    if configuration.owed_set:
                        try:
                            offering = owner._subscription_offering(configuration, record)
                        # Unsupported or unavailable bindings remain unadmitted.
                        except Exception:  # nosec B112
                            continue
                        self._enroll_owed_set(configuration, offering._producer_key, record)
                        continue
                    try:
                        offering = owner._configuration_offering(configuration, record)
                    # Unsupported or unavailable bindings remain unadmitted.
                    except Exception:  # nosec B112
                        continue
                    self._enroll(configuration, offering._producer_key, record)
            return True

    def _check_admission(self, lease: ReportingMaterializerLease) -> None:
        if lease.admission_epoch != 2:
            return
        admission = self._production_accounts.get(lease.scope.principal.account_id)
        if admission is None or lease.attempt.created_at < admission.activated_at:
            raise failure("BINDING_MISMATCH")
        self._owner()._check_context(
            self._context(lease.scope),
            lease.request.verification_key,
            admission.policy,
            self._production_generations.get(lease.scope.generation_key),
            (
                self._production_source_bindings[lease.scope.generation_key].document()
                if lease.scope.generation_key in self._production_source_bindings
                else None
            ),
            self._destination_document(self._context(lease.scope).binding),
        )

    def _new_admission_epoch(
        self, context: MaterializerContext, key: ReportingVerificationKey
    ) -> int:
        admission = self._production_accounts.get(context.scope.principal.account_id)
        if admission is None:
            raise ReportingNotificationError("reporting_production_activation_required")
        self._owner()._check_context(
            context,
            key,
            admission.policy,
            self._production_generations.get(context.configuration.generation_key),
            (
                self._production_source_bindings[context.configuration.generation_key].document()
                if context.configuration.generation_key in self._production_source_bindings
                else None
            ),
            self._destination_document(context.binding),
        )
        return 2

    def _materializer_candidate_enabled(self, account_id: str) -> bool:
        return account_id in self._production_accounts

    def _held(self, lease: ReportingMaterializerLease) -> _Work | None:
        work = super()._held(lease)
        if work is not None:
            self._check_admission(lease)
        return work

    def _materializer_capture_collections(
        self, outcome: ReportingMaterializationRecord
    ) -> tuple[dict[ReportingDeliveryPrincipal, int], list[ReportingMaterializerBoundary]]:
        work = self._materializer_work.get(
            (
                outcome.scope.principal.account_id,
                outcome.scope.consumer_id,
                outcome.reporting_materialization_id,
            )
        )
        if work is not None and work.admission_epoch == 2:
            return self._production_status_heads, self._production_boundaries
        return super()._materializer_capture_collections(outcome)

    def _enqueue_materializer(
        self, event: ReportingDomainEvent, lease: ReportingMaterializerLease
    ) -> None:
        if lease.admission_epoch == 0:
            return super()._enqueue_materializer(event, lease)
        self._check_admission(lease)
        if self._production_outbox is None:
            raise failure("BINDING_MISMATCH")
        self._production_outbox.enqueue(event)
        if (
            self._production_outbox.events.get(
                (event.account_id, event.consumer_namespace, event.notification_id)
            )
            != event
        ):
            raise failure("BINDING_MISMATCH")

    async def read_production_boundaries(
        self, *, caller: ReportingDeliveryPrincipal, after: int = 0, limit: int = 100
    ) -> tuple[ReportingMaterializerBoundary, ...]:
        if type(after) is not int or after < 0 or type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("production boundary reads require bounded positions")
        async with self._lock:
            return tuple(
                b for b in self._production_boundaries if b.caller == caller and b.sequence > after
            )[:limit]
