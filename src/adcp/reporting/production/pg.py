"""Production admission selects new participants in the single B2.1 transaction."""

from __future__ import annotations

import asyncio
import json
import weakref
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from contextvars import ContextVar
from datetime import datetime, timedelta
from importlib.resources import files
from typing import TYPE_CHECKING, Any

from adcp.reporting.ledger._delivery_state import decode_record
from adcp.reporting.ledger.delivery_models import (
    ReportingDeliveryPrincipal,
    ReportingDeliveryScope,
    ReportingDestinationBinding,
)
from adcp.reporting.ledger.models import ReportingConfiguration, ReportingObligationRecord
from adcp.reporting.ledger.notification_models import ReportingNotificationError
from adcp.reporting.ledger.pg import _configuration_from_row
from adcp.reporting.ledger.producer_progress import acquisition_state, check_next_period
from adcp.reporting.ledger.store import LeasedConfiguration, LedgerConflictError, _utc
from adcp.reporting.materializer.capture import ReportingMaterializerBoundary
from adcp.reporting.materializer.contracts import (
    ReportingPreparedRevision,
    ReportingVerificationKey,
    ReportingWriterFailure,
)
from adcp.reporting.materializer.verification import ReportingVerifiedDestination
from adcp.reporting.materializer.work import (
    MaterializerContext,
    ReportingMaterializerLease,
    ReportingMaterializerTurn,
    key_for,
)
from adcp.reporting.outbox.status_pg import PgReportingStatusOutbox
from adcp.reporting.projection.pg import PgReportingProjectionStore
from adcp.reporting.source import ReportingConstituent

if TYPE_CHECKING:
    from adcp.reporting.production.service import ReportingProductionSupport

_EPOCH: ContextVar[tuple[int, int] | None] = ContextVar(
    "reporting_production_operation", default=None
)
_ADMISSION_CONNECTION: ContextVar[tuple[int, object, Any] | None] = ContextVar(
    "reporting_production_configuration_connection", default=None
)

_SOURCE_CONFIGURATION = (
    "SELECT c.delivery_config_id,c.delivery_config_version,c.account_id,"
    " c.report_definition_id,c.reporting_profile,c.feed_purpose,"
    " c.required_finality,c.account_timezone,c.schedule,c.media_buy_ids,"
    " c.activated_at,c.deactivated_at,"
    " c.automated_recovery_seconds,c.status_retention_days,c.definition,"
    " c.authoritative_party,g.producer_key,g.source_binding FROM reporting_configurations c"
    " JOIN reporting_production_generations g"
    " USING(account_id,delivery_config_id,delivery_config_version)"
    " JOIN reporting_production_accounts a ON a.account_id=g.account_id"
    " WHERE c.account_id=%s AND c.delivery_config_id=%s"
    " AND c.delivery_config_version=%s AND g.producer_key=ANY(%s)"
)

_OWNED = (
    "(SELECT account_id,consumer_id,delivery_config_id,delivery_config_version,"
    "reporting_obligation_id,reporting_materialization_id,state,retry_allowed"
    " FROM reporting_materializer_work UNION ALL"
    " SELECT account_id,consumer_id,delivery_config_id,delivery_config_version,"
    "reporting_obligation_id,reporting_materialization_id,state,retry_allowed"
    " FROM reporting_production_work)"
)

_ProducerSample = tuple[int, datetime | None, str, str, int]


class _ProductionConnection:
    """Only fixed SDK identifiers are selected; account/writer ordering stays intact."""

    def __init__(self, connection: Any) -> None:
        self.connection = connection

    def transaction(self) -> Any:
        return self.connection.transaction()

    async def execute(self, query: str, params: Any = None) -> Any:
        # A known terminal B2.1 failure remains eligible for an N+1 retry, but
        # pending/unknown effects in either epoch prohibit a new reservation.
        if query.startswith("SELECT retry_allowed FROM reporting_materializer_work"):
            query = query.replace("reporting_materializer_work", _OWNED + " owned", 1)
        elif "SELECT 1 FROM reporting_materializer_work w WHERE" in query:
            query = query.replace(
                "FROM reporting_materializer_work w WHERE", "FROM " + _OWNED + " w WHERE"
            )
        else:
            for old, new in (
                ("reporting_materializer_notification_", "reporting_production_notification_"),
                ("reporting_materializer_status_", "reporting_production_status_"),
                ("reporting_materializer_work", "reporting_production_work"),
            ):
                query = query.replace(old, new)
        return await self.connection.execute(query, params)


class _ProductionQueueConnection:
    def __init__(self, connection: Any) -> None:
        self.connection = connection

    def transaction(self) -> Any:
        return self.connection.transaction()

    async def execute(self, query: str, params: Any = None) -> Any:
        for old, new in (
            ("reporting_notification_", "reporting_production_notification_"),
            ("reporting_webhook_", "reporting_production_webhook_"),
        ):
            query = query.replace(old, new)
        return await self.connection.execute(query, params)


class PgReportingProductionOutbox(PgReportingStatusOutbox):
    """Generic crash-safe delivery/fanout and private activity on the admitted queue."""

    @asynccontextmanager
    async def _connection(self) -> AsyncIterator[Any]:
        async with self._pool.connection() as connection:
            yield _ProductionQueueConnection(connection)

    async def create_schema(self) -> None:
        await PgReportingProductionStore(pool=self._pool, notifications=True).create_schema()


class PgReportingProductionStore(PgReportingProjectionStore):
    """The production store retains all ordinary legacy reader/writer APIs.

    New work requires the live SDK composition and its mounted applicable
    routes. Pending epoch-zero work uses its original tables, identity and
    permanently quarantined enqueue. Installation alone admits nothing.
    """

    _production_support: weakref.ReferenceType[ReportingProductionSupport] | None = None
    _production_lease_samples: dict[tuple[str, ...], _ProducerSample] | None = None

    def _owner(self) -> ReportingProductionSupport:
        from adcp.reporting.production.service import production_owner

        return production_owner(self)

    @asynccontextmanager
    async def _connection(self) -> AsyncIterator[Any]:
        admission = _ADMISSION_CONNECTION.get()
        if admission is not None and admission[:2] == (id(self), asyncio.current_task()):
            yield admission[2]
            return
        async with super()._connection() as connection:
            if _EPOCH.get() == (id(self), 2):
                yield _ProductionConnection(connection)
            else:
                yield connection

    async def admit_production_configuration(
        self,
        configuration: ReportingConfiguration,
        binding: ReportingDestinationBinding,
        *,
        offering_id: str,
    ) -> None:
        offering = self._owner()._configuration_offering(
            configuration, binding, offering_id=offering_id
        )
        async with self._connection() as connection, connection.transaction():
            await self._lock_account(connection, configuration.account_id)
            token = _ADMISSION_CONNECTION.set((id(self), asyncio.current_task(), connection))
            try:
                await self.put_configuration(configuration)
                await self.put_destination_binding(binding)
                await self._enroll_on(connection, configuration, offering._producer_key, binding)
            finally:
                _ADMISSION_CONNECTION.reset(token)

    async def _enroll_on(
        self,
        connection: Any,
        configuration: ReportingConfiguration,
        producer_key: str,
        destination: ReportingDestinationBinding,
    ) -> None:
        key = configuration.generation_key
        identity = (key.account_id, key.delivery_config_id, key.delivery_config_version)
        binding = self._owner()._source_binding(configuration, producer_key).document()
        await connection.execute(
            "INSERT INTO reporting_production_generations VALUES(%s,%s,%s,%s,%s::jsonb)"
            " ON CONFLICT DO NOTHING",
            (*identity, producer_key, json.dumps(binding)),
        )
        row = await (
            await connection.execute(
                "SELECT producer_key,source_binding FROM reporting_production_generations"
                " WHERE account_id=%s AND delivery_config_id=%s AND delivery_config_version=%s",
                identity,
            )
        ).fetchone()
        if row is None or row != (producer_key, binding):
            raise ReportingNotificationError("reporting_production_source_conflict")
        owner = self._owner()
        offering = owner._configuration_offering(
            configuration, destination, producer_key=producer_key
        )
        document = owner._destination_binding(destination, offering).wire()
        destination_identity = (
            key.account_id,
            destination.consumer_id,
            key.delivery_config_id,
            key.delivery_config_version,
        )
        await connection.execute(
            "INSERT INTO reporting_production_destination_bindings VALUES(%s,%s,%s,%s,%s::jsonb)"
            " ON CONFLICT DO NOTHING",
            (*destination_identity, json.dumps(document)),
        )
        original = await (
            await connection.execute(
                "SELECT method FROM reporting_production_destination_bindings"
                " WHERE account_id=%s AND consumer_id=%s AND delivery_config_id=%s"
                " AND delivery_config_version=%s",
                destination_identity,
            )
        ).fetchone()
        if original is None or original[0] != document:
            raise ReportingNotificationError("reporting_production_destination_conflict")

    async def lease_period_close(
        self, *, worker_id: str, now: datetime, lease_seconds: float
    ) -> LeasedConfiguration | None:
        keys = self._owner()._producer_keys()
        if not keys:
            return None
        moment = _utc(now)
        expires = moment + timedelta(seconds=lease_seconds)
        if expires <= moment:
            raise ValueError("producer lease duration must be positive")
        if self._production_lease_samples is None:
            self._production_lease_samples = {}
        after = self._production_lease_samples.get(keys)
        following: _ProducerSample | None = None
        result = None
        async with self._connection() as connection, connection.transaction():
            # Discover a bounded set without locking configuration rows. The
            # inherited configuration trigger takes the account lock, so that
            # lock must precede the row lock here, just as it does in activation.
            # A busy account cannot advance a durable rank while another
            # transaction holds its lock. Continue a read-only sample instead
            # of repeatedly trying the same prefix. At most one nonempty
            # 32-row window is examined; an empty tail may wrap once.
            for _ in range(2):
                continuation = (
                    " AND (greatest(coalesce(t.lease_turn,0),coalesce(p.probe_turn,0)),"
                    " coalesce(c.lease_expires_at,'-infinity'::timestamptz),"
                    " c.account_id,c.delivery_config_id,c.delivery_config_version)"
                    " > (%s,coalesce(%s::timestamptz,'-infinity'::timestamptz),%s,%s,%s)"
                    if after is not None
                    else ""
                )
                query = (
                    "SELECT c.account_id,c.delivery_config_id,c.delivery_config_version,"  # nosec B608
                    " greatest(coalesce(t.lease_turn,0),coalesce(p.probe_turn,0)),"
                    " c.lease_expires_at"
                    " FROM reporting_production_generations g"
                    " JOIN reporting_production_accounts a ON a.account_id=g.account_id"
                    " JOIN reporting_configurations c"
                    " ON (c.account_id,c.delivery_config_id,c.delivery_config_version)="
                    " (g.account_id,g.delivery_config_id,g.delivery_config_version)"
                    " LEFT JOIN adcp_reporting_configuration_lease_turns t"
                    " ON (t.account_id,t.delivery_config_id,t.delivery_config_version)="
                    " (g.account_id,g.delivery_config_id,g.delivery_config_version)"
                    " LEFT JOIN reporting_production_source_probe_turns p"
                    " ON (p.account_id,p.delivery_config_id,p.delivery_config_version)="
                    " (g.account_id,g.delivery_config_id,g.delivery_config_version)"
                    " WHERE g.producer_key=ANY(%s)"
                    " AND (c.lease_expires_at IS NULL OR c.lease_expires_at<=%s)"
                    + continuation
                    + " ORDER BY greatest(coalesce(t.lease_turn,0),coalesce(p.probe_turn,0)),"
                    " c.lease_expires_at NULLS FIRST,"
                    " c.account_id,c.delivery_config_id,c.delivery_config_version LIMIT 32"
                )
                # Only the fixed SDK continuation above changes this SQL;
                # every cursor value and source identity remains a parameter.
                rows = await (
                    await connection.execute(query, (list(keys), moment, *(after or ())))
                ).fetchall()
                if rows or after is None:
                    break
                after = None
            for candidate in rows:
                row = candidate[:3]
                following = (candidate[3], candidate[4], row[0], row[1], row[2])
                locked = await (
                    await connection.execute(
                        "SELECT pg_try_advisory_xact_lock(hashtext('adcp.reporting:' || %s))",
                        (row[0],),
                    )
                ).fetchone()
                if not locked[0]:
                    continue
                current = await (
                    await connection.execute(_SOURCE_CONFIGURATION, (*row, list(keys)))
                ).fetchone()
                if current is None:
                    continue
                configuration = _configuration_from_row(current[:16])
                try:
                    self._owner()._check_source_binding(configuration, current[16], current[17])
                except Exception:
                    # A permanently revoked generation must not occupy the
                    # first bounded window forever. This is a probe, not a
                    # lease: preserve ordinary lease ranks and all source and
                    # external identities. Its durable rank shares the same
                    # ordering clock as successful configuration acquisitions.
                    await connection.execute(
                        "INSERT INTO reporting_production_source_probe_turns"
                        " (account_id,delivery_config_id,delivery_config_version,probe_turn)"
                        " VALUES(%s,%s,%s,nextval('adcp_reporting_configuration_lease_turn_seq'))"
                        " ON CONFLICT(account_id,delivery_config_id,delivery_config_version)"
                        " DO UPDATE SET probe_turn="
                        "nextval('adcp_reporting_configuration_lease_turn_seq')",
                        tuple(row),
                    )
                    continue
                acquired = await (
                    await connection.execute(
                        "UPDATE reporting_configurations SET lease_worker_id=%s,lease_expires_at=%s"
                        " WHERE account_id=%s AND delivery_config_id=%s"
                        " AND delivery_config_version=%s"
                        " AND (lease_expires_at IS NULL OR lease_expires_at<=%s)"
                        " RETURNING account_id",
                        (worker_id, expires, *row, moment),
                    )
                ).fetchone()
                if acquired is None:
                    continue
                await self._retain_materializer_generation_on_lease_change(connection, tuple(row))
                await connection.execute(
                    "INSERT INTO adcp_reporting_configuration_lease_turns"
                    " (account_id,delivery_config_id,delivery_config_version,lease_turn)"
                    " VALUES(%s,%s,%s,nextval('adcp_reporting_configuration_lease_turn_seq'))"
                    " ON CONFLICT(account_id,delivery_config_id,delivery_config_version)"
                    " DO UPDATE SET lease_turn="
                    "nextval('adcp_reporting_configuration_lease_turn_seq')",
                    tuple(row),
                )
                result = LeasedConfiguration(row[0], row[1], row[2], expires)
                break
        # Hints are per store and selected producer keys, not durable work or
        # leases. Publish a hint only after commit; a failed mutation retries
        # the same window. Successful acquisition returns to the durable
        # turn-primary order. A fresh store starts there too. Concurrent hints
        # may cause a bounded revisit, but cannot authorize or fence any work.
        if result is None and following is not None:
            self._production_lease_samples[keys] = following
        else:
            self._production_lease_samples.pop(keys, None)
        return result

    async def release_period_close(self, lease: LeasedConfiguration, *, worker_id: str) -> None:
        async with self._connection() as connection, connection.transaction():
            await self._lock_account(connection, lease.account_id)
            identity = (lease.account_id, lease.delivery_config_id, lease.delivery_config_version)
            released = await (
                await connection.execute(
                    "UPDATE reporting_configurations SET lease_worker_id=NULL,lease_expires_at=NULL"
                    " WHERE account_id=%s AND delivery_config_id=%s AND delivery_config_version=%s"
                    " AND lease_worker_id=%s AND lease_expires_at=%s RETURNING account_id",
                    (*identity, worker_id, lease.lease_expires_at),
                )
            ).fetchone()
            if released is not None:
                await self._retain_materializer_generation_on_lease_change(connection, identity)

    async def _retain_materializer_generation_on_lease_change(
        self, connection: Any, identity: tuple[str, str, int]
    ) -> None:
        # The immutable inherited trigger treats *every* configuration UPDATE
        # as source invalidation, including lease-only bookkeeping. These two
        # SDK statements change only lease fields, under the account lock.
        # Cancel just their one trigger increment in the same transaction;
        # the wakeup remains harmless. No intervening source write can be
        # hidden, no committed generation goes backwards, and pending work's
        # original generation/epoch/external identity is never rewritten.
        # A real configuration, revision or readability change still executes
        # the original trigger without this correction and fences old work.
        await connection.execute(
            "UPDATE reporting_materializer_candidates SET generation=generation-1"
            " WHERE account_id=%s AND delivery_config_id=%s AND delivery_config_version=%s",
            identity,
        )

    @asynccontextmanager
    async def _source_connection(
        self, configuration: ReportingConfiguration
    ) -> AsyncIterator[tuple[Any, tuple[str, str, int]]]:
        keys = self._owner()._producer_keys()
        key = configuration.generation_key
        identity = (key.account_id, key.delivery_config_id, key.delivery_config_version)
        async with self._connection() as connection, connection.transaction():
            await self._lock_account(connection, key.account_id)
            row = await (
                await connection.execute(
                    _SOURCE_CONFIGURATION,
                    (*identity, list(keys)),
                )
            ).fetchone()
            if row is None or _configuration_from_row(row[:16]) != configuration:
                raise LedgerConflictError("HISTORY_UNAVAILABLE", "producer generation unavailable")
            self._owner()._check_source_binding(configuration, row[16], row[17])
            await connection.execute(
                "INSERT INTO reporting_production_source_progress"
                " (account_id,delivery_config_id,delivery_config_version) VALUES(%s,%s,%s)"
                " ON CONFLICT DO NOTHING",
                identity,
            )
            token = _ADMISSION_CONNECTION.set((id(self), asyncio.current_task(), connection))
            try:
                yield connection, identity
            finally:
                _ADMISSION_CONNECTION.reset(token)

    async def producer_constituents(
        self, configuration: ReportingConfiguration, obligation: ReportingObligationRecord
    ) -> tuple[ReportingConstituent, ...]:
        async with self._source_connection(configuration) as (connection, identity):
            if obligation.generation_key != configuration.generation_key or set(
                obligation.media_buy_ids
            ) != set(configuration.media_buy_ids):
                raise LedgerConflictError("HISTORY_UNAVAILABLE", "source denominator differs")
            row = await (
                await connection.execute(
                    "SELECT producer_key,source_binding FROM reporting_production_generations"
                    " WHERE account_id=%s AND delivery_config_id=%s AND delivery_config_version=%s",
                    identity,
                )
            ).fetchone()
            binding = self._owner()._check_source_binding(configuration, row[0], row[1])
            return binding.constituents()

    async def producer_closed_through(
        self, configuration: ReportingConfiguration
    ) -> datetime | None:
        async with self._source_connection(configuration) as (connection, identity):
            row = await (
                await connection.execute(
                    "SELECT closed_through FROM reporting_production_source_progress"
                    " WHERE account_id=%s AND delivery_config_id=%s AND delivery_config_version=%s",
                    identity,
                )
            ).fetchone()
            return row[0] if row is not None else None

    async def commit_producer_period(
        self,
        configuration: ReportingConfiguration,
        obligation: ReportingObligationRecord,
        *,
        previous_end: datetime | None,
    ) -> ReportingObligationRecord:
        check_next_period(configuration, obligation, previous_end)
        async with self._source_connection(configuration) as (connection, identity):
            row = await (
                await connection.execute(
                    "SELECT closed_through FROM reporting_production_source_progress"
                    " WHERE account_id=%s AND delivery_config_id=%s AND delivery_config_version=%s",
                    identity,
                )
            ).fetchone()
            current = row[0]
            if current != previous_end and (current is None or current < obligation.period.end):
                raise LedgerConflictError("HISTORY_UNAVAILABLE", "producer progress changed")
            stored = await self.commit_obligation(obligation)
            await connection.execute(
                "INSERT INTO reporting_production_source_work"
                " (account_id,delivery_config_id,delivery_config_version,reporting_obligation_id,"
                " period_end) VALUES(%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING",
                (*identity, stored.reporting_obligation_id, stored.period.end),
            )
            await connection.execute(
                "UPDATE reporting_production_source_progress"
                " SET closed_through=greatest(closed_through,%s)"
                " WHERE account_id=%s AND delivery_config_id=%s AND delivery_config_version=%s",
                (stored.period.end, *identity),
            )
            return stored

    async def next_producer_obligations(
        self, configuration: ReportingConfiguration, *, now: datetime, limit: int
    ) -> tuple[str, ...]:
        if type(limit) is not int or not 1 <= limit <= 64:
            raise ValueError("production acquisition limit must be in 1..64")
        async with self._source_connection(configuration) as (connection, identity):
            rows = await (
                await connection.execute(
                    "SELECT reporting_obligation_id FROM reporting_production_source_work"
                    " WHERE account_id=%s AND delivery_config_id=%s AND delivery_config_version=%s"
                    " AND state='pending' AND period_end<=%s"
                    " ORDER BY acquisition_turn,reporting_obligation_id LIMIT %s FOR UPDATE",
                    (*identity, now, limit),
                )
            ).fetchall()
            if not rows:
                return ()
            head = await (
                await connection.execute(
                    "UPDATE reporting_production_source_progress"
                    " SET acquisition_turn=acquisition_turn+%s"
                    " WHERE account_id=%s AND delivery_config_id=%s AND delivery_config_version=%s"
                    " RETURNING acquisition_turn",
                    (len(rows), *identity),
                )
            ).fetchone()
            for offset, row in enumerate(rows, 1):
                await connection.execute(
                    "UPDATE reporting_production_source_work SET acquisition_turn=%s"
                    " WHERE account_id=%s AND reporting_obligation_id=%s",
                    (head[0] - len(rows) + offset, identity[0], row[0]),
                )
            return tuple(row[0] for row in rows)

    async def finish_producer_acquisition(
        self, configuration: ReportingConfiguration, *, reporting_obligation_id: str
    ) -> None:
        async with self._source_connection(configuration) as (connection, identity):
            obligation = await self.get_obligation(
                account_id=identity[0], reporting_obligation_id=reporting_obligation_id
            )
            if obligation is None or obligation.generation_key != configuration.generation_key:
                raise LedgerConflictError("HISTORY_UNAVAILABLE", "producer generation differs")
            revisions = await self.list_revisions(
                account_id=identity[0], reporting_obligation_id=reporting_obligation_id
            )
            await connection.execute(
                "UPDATE reporting_production_source_work SET state=%s"
                " WHERE account_id=%s AND delivery_config_id=%s AND delivery_config_version=%s"
                " AND reporting_obligation_id=%s",
                (acquisition_state(obligation, revisions), *identity, reporting_obligation_id),
            )

    @asynccontextmanager
    async def _lease_epoch(self, lease: ReportingMaterializerLease) -> AsyncIterator[None]:
        token = _EPOCH.set((id(self), lease.admission_epoch))
        try:
            yield
        finally:
            _EPOCH.reset(token)

    async def create_schema(self) -> None:
        async with self._connection() as connection, connection.transaction():
            await self._create_schema_on(connection)
            root = files("adcp.reporting.ledger")
            for name in (
                "reporting_materializer.sql",
                "reporting_receipt_ingestion.sql",
                "reporting_feed.sql",
                "reporting_status_notifications.sql",
                "reporting_status_selector_version.sql",
                "reporting_projection.sql",
                "reporting_projection_notifications.sql",
                "reporting_projection_feed.sql",
                "reporting_production.sql",
            ):
                await connection.execute(root.joinpath(name).read_text())

    async def materializer_ready(self) -> bool:
        owner = self._owner()
        if not await owner._schema_ready():
            raise ReportingNotificationError("reporting_production_schema_unready")
        owner._assert_components()
        return True

    async def _activate_production(self, *, account_id: str) -> bool:
        owner = self._owner()
        await self.materializer_ready()
        async with self._connection() as connection, connection.transaction():
            await self._lock_account(connection, account_id)
            owner._assert_components()
            projection = await (
                await connection.execute(
                    "SELECT policy FROM reporting_projection_accounts"
                    " WHERE account_id=%s AND current_input IS NOT NULL",
                    (account_id,),
                )
            ).fetchone()
            if projection is None or projection[0] != owner.projection.policy:
                raise ReportingNotificationError("status_projection_activation_required")
            policy = owner._admission_policy()
            current = await (
                await connection.execute(
                    "SELECT policy FROM reporting_production_accounts WHERE account_id=%s",
                    (account_id,),
                )
            ).fetchone()
            if current is not None:
                if current[0] != policy:
                    raise ReportingNotificationError("reporting_production_policy_conflict")
                return False
            await connection.execute(
                "INSERT INTO reporting_production_accounts(account_id,policy) VALUES(%s,%s::jsonb)",
                (account_id, json.dumps(policy)),
            )
            configurations = {
                c.generation_key: c
                for c in await self._list_configurations_on(connection, account_id=account_id)
            }
            bindings = await (
                await connection.execute(
                    "SELECT payload FROM reporting_reconciliation_records"
                    " WHERE account_id=%s AND namespace='destination_binding'",
                    (account_id,),
                )
            ).fetchall()
            for (document,) in bindings:
                binding = decode_record(document)
                if not isinstance(binding, ReportingDestinationBinding):
                    raise ReportingNotificationError("reporting_production_history_corrupt")
                configuration = configurations[binding.generation_key]
                try:
                    offering = owner._configuration_offering(configuration, binding)
                # Unsupported or unavailable bindings remain unadmitted.
                except Exception:  # nosec B112
                    continue
                await self._enroll_on(connection, configuration, offering._producer_key, binding)
            return True

    async def _materializer_context_on(
        self, connection: Any, scope: ReportingDeliveryScope
    ) -> MaterializerContext:
        context = await super()._materializer_context_on(connection, scope)
        if isinstance(connection, _ProductionConnection):
            owner = self._owner()
            row = await (
                await connection.execute(
                    "SELECT a.policy,g.producer_key,g.source_binding,d.method"
                    " FROM reporting_production_accounts a"
                    " LEFT JOIN reporting_production_generations g"
                    " ON g.account_id=a.account_id AND g.delivery_config_id=%s"
                    " AND g.delivery_config_version=%s"
                    " LEFT JOIN reporting_production_destination_bindings d"
                    " ON (d.account_id,d.delivery_config_id,d.delivery_config_version)="
                    " (g.account_id,g.delivery_config_id,g.delivery_config_version)"
                    " AND d.consumer_id=%s WHERE a.account_id=%s",
                    (
                        scope.generation_key.delivery_config_id,
                        scope.generation_key.delivery_config_version,
                        scope.consumer_id,
                        scope.principal.account_id,
                    ),
                )
            ).fetchone()
            if row is None:
                raise ReportingNotificationError("reporting_production_activation_required")
            owner._check_context(
                context,
                key_for(context.binding, context.obligation, owner.keys),
                row[0],
                row[1],
                row[2],
                row[3],
            )
        return context

    async def _claim_account_on(
        self,
        connection: Any,
        account_id: str,
        keys: tuple[ReportingVerificationKey, ...],
        lease_seconds: int,
    ) -> ReportingMaterializerLease | ReportingMaterializerTurn:
        activated = await (
            await connection.execute(
                "SELECT 1 FROM reporting_production_accounts WHERE account_id=%s", (account_id,)
            )
        ).fetchone()
        if activated is None:
            return ReportingMaterializerTurn("idle")
        old = await (
            await connection.execute(
                "SELECT to_jsonb(w) FROM reporting_materializer_work w WHERE account_id=%s"
                " AND state='pending' AND due_at<=clock_timestamp()"
                " AND (lease_until IS NULL OR lease_until<=clock_timestamp())"
                " ORDER BY due_at,reporting_materialization_id LIMIT 1 FOR UPDATE",
                (account_id,),
            )
        ).fetchone()
        if old is not None:
            return await super()._lease_on(connection, old[0], keys, lease_seconds)
        return await super()._claim_account_on(
            _ProductionConnection(connection), account_id, keys, lease_seconds
        )

    async def _schedule_account_on(self, connection: Any, account_id: str) -> None:
        if isinstance(connection, _ProductionConnection):
            connection = connection.connection
        # _OWNED contains only fixed SDK tables/columns; account values are bound.
        await connection.execute(
            "UPDATE reporting_materializer_accounts SET due_at=(SELECT min(due) FROM ("  # nosec B608
            " SELECT due_at AS due FROM reporting_materializer_work"
            " WHERE account_id=%s AND state='pending'"
            " UNION ALL SELECT due_at FROM reporting_production_work"
            " WHERE account_id=%s AND state='pending'"
            " UNION ALL SELECT c.due_at FROM reporting_materializer_candidates c"
            " WHERE c.account_id=%s AND c.due_at IS NOT NULL AND NOT EXISTS (SELECT 1 FROM "
            + _OWNED
            + " w WHERE w.account_id=c.account_id AND w.consumer_id=c.consumer_id"  # nosec B608
            " AND w.delivery_config_id=c.delivery_config_id"
            " AND w.delivery_config_version=c.delivery_config_version"
            " AND w.reporting_obligation_id=c.reporting_obligation_id AND w.state='pending')"
            " UNION ALL SELECT clock_timestamp() FROM reporting_materializer_discovery"
            " WHERE account_id=%s AND NOT complete) ready) WHERE account_id=%s",
            (account_id,) * 5,
        )

    async def renew_materialization(
        self, lease: ReportingMaterializerLease, *, lease_seconds: int = 30
    ) -> bool:
        async with self._lease_epoch(lease):
            return await super().renew_materialization(lease, lease_seconds=lease_seconds)

    async def authorize_materialization(self, lease: ReportingMaterializerLease) -> None:
        async with self._lease_epoch(lease):
            await super().authorize_materialization(lease)

    async def finish_materialization(
        self,
        lease: ReportingMaterializerLease,
        *,
        prepared: ReportingPreparedRevision | None = None,
        verified: ReportingVerifiedDestination | None = None,
        error: ReportingWriterFailure | None = None,
    ) -> ReportingMaterializerTurn:
        if lease.admission_epoch == 2:
            self._owner()._assert_components()
        async with self._lease_epoch(lease):
            return await super().finish_materialization(
                lease, prepared=prepared, verified=verified, error=error
            )

    async def read_production_boundaries(
        self, *, caller: ReportingDeliveryPrincipal, after: int = 0, limit: int = 100
    ) -> tuple[ReportingMaterializerBoundary, ...]:
        token = _EPOCH.set((id(self), 2))
        try:
            return await super().read_materializer_boundaries(
                caller=caller, after=after, limit=limit
            )
        finally:
            _EPOCH.reset(token)
