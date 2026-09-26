"""Reference production transactions for shared state-machine conformance."""

from __future__ import annotations

import json
import weakref
from dataclasses import dataclass
from datetime import datetime
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
from adcp.reporting.outbox.memory import InMemoryReportingOutbox, NotificationState
from adcp.reporting.production.contracts import ReportingProductionSourceBinding
from adcp.reporting.production.source_registry import ReportingProductionSourceContext
from adcp.reporting.projection.memory import InMemoryReportingProjectionStore
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
    _production_support: weakref.ReferenceType[ReportingProductionSupport] | None = None

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

    def _owner(self) -> ReportingProductionSupport:
        from adcp.reporting.production.service import production_owner

        return production_owner(self)

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
        if configuration.account_id not in self._production_accounts:
            return False
        producer_key = self._production_generations.get(configuration.generation_key)
        binding = self._production_source_bindings.get(configuration.generation_key)
        if producer_key not in self._owner()._producer_keys() or binding is None:
            return False
        try:
            self._owner()._check_source_binding(configuration, producer_key, binding.document())
        except Exception:
            return False
        return True

    def _check_source_generation(self, configuration: ReportingConfiguration) -> None:
        if (
            not self._configuration_lease_eligible(configuration)
            or self._configurations.get(configuration.generation_key) != configuration
        ):
            raise LedgerConflictError("HISTORY_UNAVAILABLE", "producer generation is unavailable")

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

    async def _activate_production(self, *, account_id: str) -> bool:
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
