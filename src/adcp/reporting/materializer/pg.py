"""PostgreSQL account-first reservation, fenced resume and verified atomic finish."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta
from importlib.resources import files
from typing import Any, cast
from uuid import uuid4

from adcp.reporting.ledger._delivery_state import RecordT
from adcp.reporting.ledger.delivery_models import (
    ReportingDeliveryPrincipal,
    ReportingDeliveryScope,
    ReportingDestinationBinding,
    ReportingMaterializationAttempt,
    ReportingMaterializationRecord,
    ReportingObligationDeliveryRecord,
)
from adcp.reporting.ledger.delivery_pg import PgReportingReconciliationStore
from adcp.reporting.ledger.models import ReportingConfigurationGenerationKey
from adcp.reporting.ledger.notification_events import materialization_event
from adcp.reporting.ledger.pg import (
    _OBLIGATION_COLUMNS,
    _REVISION_COLUMNS,
    _configuration_from_row,
    _obligation_from_row,
    _revision_from_row,
)
from adcp.reporting.ledger.store import LedgerConflictError
from adcp.reporting.materializer._errors import (
    ReportingMaterializerUsageError,
    materializer_errors,
)
from adcp.reporting.materializer.capture import (
    ReportingMaterializerBoundary,
    decode_materializer_boundary,
    enqueue_materializer_event_on,
    private_snapshot,
)
from adcp.reporting.materializer.contracts import (
    ReportingDestinationRequest,
    ReportingPreparedRevision,
    ReportingVerificationKey,
    ReportingWriterError,
    ReportingWriterFailure,
    binding_fingerprint,
    failure,
)
from adcp.reporting.materializer.schema import validate_materializer_schema
from adcp.reporting.materializer.verification import (
    ReportingVerifiedDestination,
    validate_materialization_target,
    validate_verified_destination,
)
from adcp.reporting.materializer.work import (
    MaterializerContext,
    MaterializerReason,
    ReportingMaterializerLease,
    ReportingMaterializerTurn,
    key_for,
    public_failure,
    validate_lease_seconds,
    verification_key_id,
)

_SCOPE_WHERE = (
    "account_id=%s AND consumer_id=%s AND delivery_config_id=%s"
    " AND delivery_config_version=%s AND reporting_obligation_id=%s"
)


def _scope_args(scope: ReportingDeliveryScope) -> tuple[str | int, ...]:
    generation = scope.generation_key
    return (
        generation.account_id,
        scope.consumer_id,
        generation.delivery_config_id,
        generation.delivery_config_version,
        scope.reporting_obligation_id,
    )


def _scope(row: dict[str, Any]) -> ReportingDeliveryScope:
    return ReportingDeliveryScope(
        ReportingConfigurationGenerationKey(
            row["account_id"], row["delivery_config_id"], row["delivery_config_version"]
        ),
        row["consumer_id"],
        row["reporting_obligation_id"],
    )


async def _now(connection: Any) -> datetime:
    row = await (await connection.execute("SELECT clock_timestamp()")).fetchone()
    assert row is not None
    return cast(datetime, row[0])


class PgReportingMaterializerStore(PgReportingReconciliationStore):
    """Additive durable extension. No account enumeration or destination I/O.

    Each turn samples at most sixteen due accounts without row locks, tries the
    Core advisory lock first, then claims within that account. Long work returns
    its connection before any source/destination call; even a size-one pool can
    renew its lease. Clock overrides affect old conformance APIs only: work time
    is always PostgreSQL time.
    """

    # Only a read-only sampling position, never a lease or durable correctness
    # boundary. Walk past busy accounts in bounded pages, wrapping after the end.
    # Restart may repeat a page; it cannot lose or acknowledge work.
    _materializer_sample_after: tuple[str, str] | None = None

    async def _commit_record_on(
        self, connection: Any, record: RecordT, *, notify: bool = True, dirty: bool = True
    ) -> tuple[RecordT, bool]:
        # Only the fenced verified finish may enqueue a readiness intent. An
        # ordinary public outcome keeps the projection dirty work it has always
        # produced; suppressing that would silently stall the status projector.
        return await super()._commit_record_on(
            connection,
            record,
            notify=notify and not isinstance(record, ReportingMaterializationRecord),
            dirty=dirty,
        )

    @materializer_errors
    async def create_schema(self) -> None:
        async with self._connection() as connection, connection.transaction():
            await self._create_schema_on(connection)
            await connection.execute(
                files("adcp.reporting.ledger").joinpath("reporting_materializer.sql").read_text()
            )

    @materializer_errors
    async def materializer_ready(self) -> bool:
        async with self._connection() as connection:
            await validate_materializer_schema(
                connection, notifications=self._notifications_enabled
            )
        return True

    async def _materializer_context_on(
        self, connection: Any, scope: ReportingDeliveryScope
    ) -> MaterializerContext:
        records = await self._records(connection, scope.principal)
        binding = next(
            (
                r
                for r in records
                if isinstance(r, ReportingDestinationBinding)
                and r.generation_key == scope.generation_key
            ),
            None,
        )
        obligation_row = await (
            await connection.execute(
                f"SELECT {_OBLIGATION_COLUMNS} FROM reporting_obligations"  # nosec B608
                " WHERE account_id=%s AND reporting_obligation_id=%s",
                (scope.principal.account_id, scope.reporting_obligation_id),
            )
        ).fetchone()
        config_row = await (
            await connection.execute(
                "SELECT delivery_config_id, delivery_config_version, account_id,"
                " report_definition_id, reporting_profile, feed_purpose, required_finality,"
                " account_timezone, schedule, media_buy_ids, activated_at, deactivated_at,"
                " automated_recovery_seconds, status_retention_days, definition,"
                " authoritative_party"
                " FROM reporting_configurations WHERE account_id=%s"
                " AND delivery_config_id=%s AND delivery_config_version=%s",
                (
                    scope.principal.account_id,
                    scope.generation_key.delivery_config_id,
                    scope.generation_key.delivery_config_version,
                ),
            )
        ).fetchone()
        revision_rows = await (
            await connection.execute(
                f"SELECT {_REVISION_COLUMNS} FROM reporting_revisions"  # nosec B608
                " WHERE account_id=%s AND reporting_obligation_id=%s",
                (scope.principal.account_id, scope.reporting_obligation_id),
            )
        ).fetchall()
        if binding is None or obligation_row is None or config_row is None:
            raise failure("BINDING_MISMATCH")
        obligation = _obligation_from_row(obligation_row)
        configuration = _configuration_from_row(config_row)
        if obligation.generation_key != scope.generation_key:
            raise failure("BINDING_MISMATCH")
        delivery = next(
            (
                r
                for r in records
                if isinstance(r, ReportingObligationDeliveryRecord) and r.scope == scope
            ),
            None,
        )
        return MaterializerContext(
            configuration,
            obligation,
            binding,
            delivery,
            tuple(_revision_from_row(r) for r in revision_rows),
            records,
        )

    async def _schedule_account_on(self, connection: Any, account_id: str) -> None:
        await connection.execute(
            "UPDATE reporting_materializer_accounts SET due_at=(SELECT min(due) FROM ("
            " SELECT due_at AS due FROM reporting_materializer_work"
            " WHERE account_id=%s AND state='pending'"
            " UNION ALL SELECT c.due_at FROM reporting_materializer_candidates c"
            " WHERE c.account_id=%s AND c.due_at IS NOT NULL AND NOT EXISTS ("
            " SELECT 1 FROM reporting_materializer_work w WHERE w.account_id=c.account_id"
            " AND w.consumer_id=c.consumer_id AND w.delivery_config_id=c.delivery_config_id"
            " AND w.delivery_config_version=c.delivery_config_version"
            " AND w.reporting_obligation_id=c.reporting_obligation_id AND w.state='pending')"
            " UNION ALL SELECT clock_timestamp() FROM reporting_materializer_discovery"
            " WHERE account_id=%s AND NOT complete) ready) WHERE account_id=%s",
            (account_id,) * 4,
        )

    async def _discover_on(self, connection: Any, account_id: str) -> bool:
        row = await (
            await connection.execute(
                "SELECT consumer_id, delivery_config_id, delivery_config_version,"
                " after_obligation_id"
                " FROM reporting_materializer_discovery WHERE account_id=%s AND NOT complete"
                " ORDER BY consumer_id, delivery_config_id, delivery_config_version"
                " LIMIT 1 FOR UPDATE",
                (account_id,),
            )
        ).fetchone()
        if row is None:
            return False
        consumer, config, version, after = row
        obligations = await (
            await connection.execute(
                "SELECT reporting_obligation_id FROM reporting_obligations WHERE account_id=%s"
                " AND delivery_config_id=%s AND delivery_config_version=%s"
                " AND reporting_obligation_id>%s ORDER BY reporting_obligation_id LIMIT 32",
                (account_id, config, version, after),
            )
        ).fetchall()
        for (obligation_id,) in obligations:
            # Only missing candidates: a later backfill batch never invalidates
            # a worker already reserved through the publication trigger.
            await connection.execute(
                "INSERT INTO reporting_materializer_candidates"
                " (account_id,consumer_id,delivery_config_id,delivery_config_version,"
                " reporting_obligation_id,due_at) VALUES (%s,%s,%s,%s,%s,clock_timestamp())"
                " ON CONFLICT DO NOTHING",
                (account_id, consumer, config, version, obligation_id),
            )
        await connection.execute(
            "UPDATE reporting_materializer_discovery SET after_obligation_id=%s,complete=%s"
            " WHERE account_id=%s AND consumer_id=%s AND delivery_config_id=%s"
            " AND delivery_config_version=%s",
            (
                obligations[-1][0] if obligations else after,
                len(obligations) < 32,
                account_id,
                consumer,
                config,
                version,
            ),
        )
        return True

    async def _park_on(
        self,
        connection: Any,
        scope: ReportingDeliveryScope,
        reason: MaterializerReason,
        *,
        due_at: datetime | None = None,
    ) -> ReportingMaterializerTurn:
        await connection.execute(
            "UPDATE reporting_materializer_candidates SET reason=%s,due_at=%s,"
            f" served_at=clock_timestamp() WHERE {_SCOPE_WHERE}",  # nosec B608
            (reason, due_at, *_scope_args(scope)),
        )
        return ReportingMaterializerTurn("parked", reason)

    @materializer_errors
    async def claim_materialization(
        self, *, keys: tuple[ReportingVerificationKey, ...], lease_seconds: int = 30
    ) -> ReportingMaterializerLease | ReportingMaterializerTurn:
        validate_lease_seconds(lease_seconds)
        await self.materializer_ready()
        async with self._connection() as connection:
            # Read-only sampling. Never lock global candidate rows before the
            # account advisory lock, including when a competing worker is busy.
            for _ in range(2):
                after = self._materializer_sample_after
                if after:
                    query = (
                        "SELECT account_id,served_at::text FROM reporting_materializer_accounts"
                        " WHERE due_at<=%s AND (served_at,account_id)>(%s::timestamptz,%s)"
                        " ORDER BY served_at,account_id LIMIT 16"
                    )
                else:
                    query = (
                        "SELECT account_id,served_at::text FROM reporting_materializer_accounts"
                        " WHERE due_at<=%s ORDER BY served_at,account_id LIMIT 16"
                    )
                accounts = await (
                    await connection.execute(
                        query,
                        (await _now(connection), *(after or ())),
                    )
                ).fetchall()
                if accounts:
                    break
                self._materializer_sample_after = None
                if after is None:
                    break
        for account_id, served_at in accounts:
            self._materializer_sample_after = (served_at, account_id)
            async with self._connection() as connection, connection.transaction():
                held = await (
                    await connection.execute(
                        "SELECT pg_try_advisory_xact_lock(hashtext(%s))",
                        (f"adcp.reporting:{account_id}",),
                    )
                ).fetchone()
                if held is None or not held[0]:
                    continue
                await self._lock_account(connection, account_id)
                await validate_materializer_schema(
                    connection, notifications=self._notifications_enabled
                )
                await connection.execute(
                    "UPDATE reporting_materializer_accounts SET served_at=clock_timestamp()"
                    " WHERE account_id=%s",
                    (account_id,),
                )
                result = await self._claim_account_on(connection, account_id, keys, lease_seconds)
                await self._schedule_account_on(connection, account_id)
                return result
        return ReportingMaterializerTurn("idle")

    async def _claim_account_on(
        self,
        connection: Any,
        account_id: str,
        keys: tuple[ReportingVerificationKey, ...],
        lease_seconds: int,
    ) -> ReportingMaterializerLease | ReportingMaterializerTurn:
        # A bound DB instant is an indexable range predicate. clock_timestamp()
        # is volatile and cannot be used as a PostgreSQL index scan boundary.
        now = await _now(connection)
        pending = await (
            await connection.execute(
                "SELECT to_jsonb(w) FROM reporting_materializer_work w WHERE account_id=%s"
                " AND state='pending' AND due_at<=%s"
                " AND (lease_until IS NULL OR lease_until<=%s)"
                " ORDER BY due_at,reporting_materialization_id LIMIT 1 FOR UPDATE",
                (account_id, now, now),
            )
        ).fetchone()
        if pending is not None:
            return await self._lease_on(connection, pending[0], keys, lease_seconds)
        discovered = await self._discover_on(connection, account_id)
        row = await (
            await connection.execute(
                "SELECT to_jsonb(c) FROM reporting_materializer_candidates c WHERE account_id=%s"
                " AND due_at<=%s AND NOT EXISTS ("
                " SELECT 1 FROM reporting_materializer_work w WHERE w.account_id=c.account_id"
                " AND w.consumer_id=c.consumer_id AND w.delivery_config_id=c.delivery_config_id"
                " AND w.delivery_config_version=c.delivery_config_version"
                " AND w.reporting_obligation_id=c.reporting_obligation_id AND w.state='pending')"
                " ORDER BY served_at,consumer_id,reporting_obligation_id LIMIT 1 FOR UPDATE",
                (account_id, await _now(connection)),
            )
        ).fetchone()
        if row is None:
            return ReportingMaterializerTurn("discovered" if discovered else "idle")
        scope = _scope(row[0])
        try:
            context = await self._materializer_context_on(connection, scope)
            key = key_for(context.binding, context.obligation, keys)
        except LedgerConflictError:
            return await self._park_on(connection, scope, "history_corrupt")
        except ReportingWriterError:
            return await self._park_on(connection, scope, "component_unavailable")
        now = await _now(connection)
        revision, reason = context.selection(now)
        if reason != "ready" or revision is None:
            due = context.configuration.activated_at
            return await self._park_on(
                connection, scope, reason, due_at=due if due is not None and due > now else None
            )
        attempts = context.attempts(revision.reporting_revision_id)
        if tuple(a.attempt for a in attempts) != tuple(range(1, len(attempts) + 1)):
            return await self._park_on(connection, scope, "history_corrupt")
        if context.pending_attempts():
            # Includes legacy pending effects on an older selected revision.
            # Publication cannot make their unknown external history disappear.
            return await self._park_on(connection, scope, "legacy_pending")
        if attempts:
            outcome = context.outcome(attempts[-1])
            assert outcome is not None
            if outcome.status != "failed":
                reason, expires = context.retained_success(outcome, now)
                return await self._park_on(connection, scope, reason, due_at=expires)
            owned = await (
                await connection.execute(
                    "SELECT retry_allowed FROM reporting_materializer_work WHERE account_id=%s"
                    " AND consumer_id=%s AND reporting_materialization_id=%s AND state='acked'",
                    (account_id, scope.consumer_id, attempts[-1].reporting_materialization_id),
                )
            ).fetchone()
            if owned is None or not owned[0]:
                return await self._park_on(
                    connection, scope, "legacy_terminal" if owned is None else "operator_required"
                )
        delivery = context.delivery
        if delivery is None:
            if context.obligation.currency is None:
                return await self._park_on(connection, scope, "operator_required")
            delivery = ReportingObligationDeliveryRecord(
                scope,
                context.obligation.currency,
                max(now, context.obligation.period.end)
                + timedelta(days=context.binding.resource_retention_days),
                now,
            )
            await self._commit_record_on(connection, delivery, notify=False, dirty=False)
        attempt = ReportingMaterializationAttempt(
            scope, revision.reporting_revision_id, "rpm_" + uuid4().hex, len(attempts) + 1, now
        )
        await self._commit_record_on(connection, attempt, notify=False, dirty=False)
        request = ReportingDestinationRequest.from_binding(context.binding, attempt, key)
        inserted = await (
            await connection.execute(
                "INSERT INTO reporting_materializer_work"
                " (account_id,consumer_id,delivery_config_id,delivery_config_version,"
                " reporting_obligation_id,reporting_revision_id,reporting_materialization_id,"
                " generation,binding_sha256,verification_key_sha256,external_id,"
                " notifications_enabled)"
                " VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)"
                " RETURNING to_jsonb(reporting_materializer_work)",
                (
                    *_scope_args(scope),
                    attempt.reporting_revision_id,
                    attempt.reporting_materialization_id,
                    row[0]["generation"],
                    request.binding_fingerprint,
                    verification_key_id(key),
                    request.external_id,
                    self._notifications_enabled,
                ),
            )
        ).fetchone()
        assert inserted is not None
        await self._park_on(connection, scope, "ready")
        return await self._lease_on(connection, inserted[0], keys, lease_seconds)

    async def _lease_on(
        self,
        connection: Any,
        work: dict[str, Any],
        keys: tuple[ReportingVerificationKey, ...],
        lease_seconds: int,
    ) -> ReportingMaterializerLease | ReportingMaterializerTurn:
        scope = _scope(work)
        try:
            if work["notifications_enabled"] != self._notifications_enabled:
                raise failure("UNSUPPORTED_VERIFICATION")
            context = await self._materializer_context_on(connection, scope)
            key = key_for(
                context.binding, context.obligation, keys, required=work["verification_key_sha256"]
            )
            attempt = next(
                r
                for r in context.records
                if isinstance(r, ReportingMaterializationAttempt)
                and r.reporting_materialization_id == work["reporting_materialization_id"]
            )
            if not context.resumable(attempt):
                return await self._park_work_on(connection, work, "history_corrupt")
            request = ReportingDestinationRequest.from_binding(context.binding, attempt, key)
            if (
                request.external_id != work["external_id"]
                or request.binding_fingerprint != work["binding_sha256"]
            ):
                raise failure("BINDING_MISMATCH")
        except (LedgerConflictError, ReportingWriterError, StopIteration):
            # Park pending unknown effects until an operator restores the exact
            # installed contract. No hot loop and no replacement identity.
            return await self._park_work_on(connection, work, "component_unavailable")
        token = uuid4()
        updated = await (
            await connection.execute(
                "UPDATE reporting_materializer_work SET lease_token=%s,"
                " lease_until=clock_timestamp()+make_interval(secs=>%s),"
                " due_at=clock_timestamp()+make_interval(secs=>%s)"
                " WHERE account_id=%s AND consumer_id=%s AND reporting_materialization_id=%s"
                " AND state='pending' AND (lease_until IS NULL OR lease_until<=clock_timestamp())"
                " RETURNING lease_until",
                (
                    token,
                    lease_seconds,
                    lease_seconds,
                    scope.principal.account_id,
                    scope.consumer_id,
                    work["reporting_materialization_id"],
                ),
            )
        ).fetchone()
        if updated is None:
            return ReportingMaterializerTurn("idle")
        return ReportingMaterializerLease(
            scope,
            work["generation"],
            str(token),
            updated[0],
            attempt,
            request,
            context,
            work["notifications_enabled"],
        )

    async def _park_work_on(
        self, connection: Any, work: dict[str, Any], reason: MaterializerReason
    ) -> ReportingMaterializerTurn:
        scope = _scope(work)
        await connection.execute(
            "UPDATE reporting_materializer_work SET due_at='infinity',"
            " lease_token=NULL,lease_until=NULL,reason=%s"
            " WHERE account_id=%s AND consumer_id=%s AND reporting_materialization_id=%s",
            (
                reason,
                scope.principal.account_id,
                scope.consumer_id,
                work["reporting_materialization_id"],
            ),
        )
        return await self._park_on(connection, scope, reason)

    async def _held_on(
        self, connection: Any, lease: ReportingMaterializerLease
    ) -> dict[str, Any] | None:
        row = await (
            await connection.execute(
                "SELECT to_jsonb(w) FROM reporting_materializer_work w WHERE account_id=%s"
                " AND consumer_id=%s AND reporting_materialization_id=%s AND state='pending'"
                " AND lease_token=%s::uuid AND lease_until>clock_timestamp() FOR UPDATE",
                (
                    lease.scope.principal.account_id,
                    lease.scope.consumer_id,
                    lease.attempt.reporting_materialization_id,
                    lease.token,
                ),
            )
        ).fetchone()
        if row is None:
            return None
        work = cast(dict[str, Any], row[0])
        if (
            work["external_id"] != lease.request.external_id
            or work["generation"] != lease.generation
            or work["binding_sha256"] != lease.request.binding_fingerprint
            or work["notifications_enabled"] != lease.notifications_enabled
            or work["notifications_enabled"] != self._notifications_enabled
        ):
            raise failure("BINDING_MISMATCH")
        return work

    @materializer_errors
    async def renew_materialization(
        self, lease: ReportingMaterializerLease, *, lease_seconds: int
    ) -> bool:
        validate_lease_seconds(lease_seconds)
        async with self._connection() as connection, connection.transaction():
            await self._lock_account(connection, lease.scope.principal.account_id)
            if await self._held_on(connection, lease) is None:
                return False
            await connection.execute(
                "UPDATE reporting_materializer_work SET lease_until=clock_timestamp()"
                " +make_interval(secs=>%s),due_at=clock_timestamp()+make_interval(secs=>%s)"
                " WHERE account_id=%s AND consumer_id=%s AND reporting_materialization_id=%s",
                (
                    lease_seconds,
                    lease_seconds,
                    lease.scope.principal.account_id,
                    lease.scope.consumer_id,
                    lease.attempt.reporting_materialization_id,
                ),
            )
            await self._schedule_account_on(connection, lease.scope.principal.account_id)
            return True

    async def _target_on(
        self,
        connection: Any,
        lease: ReportingMaterializerLease,
    ) -> tuple[MaterializerContext, MaterializerReason]:
        context = await self._materializer_context_on(connection, lease.scope)
        selected, reason = context.selection(await _now(connection))
        if reason != "ready":
            return context, reason
        row = await (
            await connection.execute(
                f"SELECT generation FROM reporting_materializer_candidates WHERE {_SCOPE_WHERE}",  # nosec B608
                _scope_args(lease.scope),
            )
        ).fetchone()
        if (
            row is None
            or row[0] != lease.generation
            or selected is None
            or selected.reporting_revision_id != lease.attempt.reporting_revision_id
        ):
            return context, "target_changed"
        if binding_fingerprint(context.binding) != lease.request.binding_fingerprint:
            return context, "binding_changed"
        return context, "ready"

    @materializer_errors
    async def authorize_materialization(self, lease: ReportingMaterializerLease) -> None:
        async with self._connection() as connection, connection.transaction():
            await self._lock_account(connection, lease.scope.principal.account_id)
            await validate_materializer_schema(
                connection, notifications=self._notifications_enabled
            )
            if await self._held_on(connection, lease) is None:
                raise failure("LEASE_LOST")
            _, reason = await self._target_on(connection, lease)
            if reason != "ready":
                raise failure(
                    "HISTORY_CORRUPT" if reason == "history_corrupt" else "CURRENT_REVISION_CHANGED"
                )

    @materializer_errors
    async def finish_materialization(
        self,
        lease: ReportingMaterializerLease,
        *,
        prepared: ReportingPreparedRevision | None = None,
        verified: ReportingVerifiedDestination | None = None,
        error: ReportingWriterFailure | None = None,
    ) -> ReportingMaterializerTurn:
        if verified is not None:
            validate_verified_destination(verified, lease.request, token=lease.token)
            if prepared is None or prepared.request != lease.request or error is not None:
                raise failure("BINDING_MISMATCH")
        elif error is None:
            raise failure("BINDING_MISMATCH")
        async with self._connection() as connection, connection.transaction():
            await self._lock_account(connection, lease.scope.principal.account_id)
            await validate_materializer_schema(
                connection, notifications=self._notifications_enabled
            )
            held = await self._held_on(connection, lease)
            if held is None:
                replay = await (
                    await connection.execute(
                        "SELECT reason FROM reporting_materializer_work WHERE account_id=%s"
                        " AND consumer_id=%s AND reporting_materialization_id=%s AND state='acked'"
                        " AND completion_token=%s::uuid AND external_id=%s",
                        (
                            lease.scope.principal.account_id,
                            lease.scope.consumer_id,
                            lease.attempt.reporting_materialization_id,
                            lease.token,
                            lease.request.external_id,
                        ),
                    )
                ).fetchone()
                if replay is not None:
                    return ReportingMaterializerTurn(
                        "verified" if replay[0] == "verified" else "failed",
                        replay[0],
                        lease.attempt.reporting_materialization_id,
                    )
                return ReportingMaterializerTurn(
                    "pending", "effect_unknown", lease.attempt.reporting_materialization_id
                )
            context, reason = await self._target_on(connection, lease)
            if reason != "ready":
                error = ReportingWriterFailure("CURRENT_REVISION_CHANGED", "new_attempt", "applied")
                verified = None
            elif verified is not None and prepared is not None:
                validate_materialization_target(
                    prepared, binding=context.binding, revisions=context.revisions
                )
            if error is not None and (
                error.effect == "unknown"
                or error.retry == "same_identity"
                or error.code in {"DEADLINE_EXCEEDED", "LEASE_LOST"}
            ):
                delay = max(1, min(error.retry_after_seconds or 5, 300))
                await connection.execute(
                    "UPDATE reporting_materializer_work SET lease_token=NULL,lease_until=NULL,"
                    " due_at=clock_timestamp()+make_interval(secs=>%s),reason='effect_unknown'"
                    " WHERE account_id=%s AND consumer_id=%s AND reporting_materialization_id=%s",
                    (
                        delay,
                        lease.scope.principal.account_id,
                        lease.scope.consumer_id,
                        lease.attempt.reporting_materialization_id,
                    ),
                )
                await self._schedule_account_on(connection, lease.scope.principal.account_id)
                return ReportingMaterializerTurn(
                    "pending", "effect_unknown", lease.attempt.reporting_materialization_id
                )
            now = await _now(connection)
            if verified is not None and (
                context.delivery is None
                or verified.resource.expires_at
                < max(
                    context.delivery.resource_retained_until,
                    now + timedelta(days=context.binding.resource_retention_days),
                )
            ):
                error = ReportingWriterFailure("RESOURCE_UNAVAILABLE", "new_attempt", "applied")
                verified = None
            if verified is not None:
                # Same-connection finish validation after all lock acquisition
                # and DB-time reads; no I/O separates this from the copy below.
                validate_verified_destination(verified, lease.request, token=lease.token)
            outcome = ReportingMaterializationRecord(
                lease.scope,
                lease.attempt.reporting_revision_id,
                lease.attempt.reporting_materialization_id,
                context.binding.success_status if verified is not None else "failed",
                now,
                verified.resource if verified is not None else None,
                replace(verified.verification, verified_at=now) if verified is not None else None,
                public_failure(error) if error is not None else None,
            )
            stored, inserted = await self._commit_record_on(
                connection, outcome, notify=False, dirty=False
            )
            await self._materializer_dirty_on(connection, stored, context)
            if inserted and verified is not None and self._notifications_enabled:
                revision = next(
                    r
                    for r in context.revisions
                    if r.reporting_revision_id == stored.reporting_revision_id
                )
                event = materialization_event(
                    stored,
                    context.records,
                    context.obligation,
                    revision,
                    context.configuration,
                    now,
                )
                if event is None:
                    raise failure("BINDING_MISMATCH")
                await enqueue_materializer_event_on(connection, event)
            retry = error is not None and error.retry == "new_attempt"
            if reason == "ready":
                reason = (
                    "verified"
                    if verified is not None
                    else ("retry" if retry else "operator_required")
                )
            ack = await (
                await connection.execute(
                    "UPDATE reporting_materializer_work SET state='acked',acknowledged_at=%s,"
                    " completion_token=lease_token,lease_token=NULL,lease_until=NULL,"
                    " retry_allowed=%s,reason=%s"
                    " WHERE account_id=%s AND consumer_id=%s AND reporting_materialization_id=%s"
                    " AND lease_token=%s::uuid AND lease_until>clock_timestamp() RETURNING 1",
                    (
                        now,
                        retry,
                        reason,
                        lease.scope.principal.account_id,
                        lease.scope.consumer_id,
                        lease.attempt.reporting_materialization_id,
                        lease.token,
                    ),
                )
            ).fetchone()
            if ack is None:
                raise failure("LEASE_LOST")  # Rolls back evidence, dirty, ACK and outbox together.
            due = (
                now + timedelta(seconds=max(1, error.retry_after_seconds or 5))
                if retry and error
                else None
            )
            if reason == "target_changed":
                due = now
            elif reason not in {"retry", "verified"}:
                due = None
            if (
                reason == "inactive"
                and context.configuration.activated_at is not None
                and context.configuration.activated_at > now
            ):
                due = context.configuration.activated_at
            if verified is not None:
                due = verified.resource.expires_at
            await self._park_on(connection, lease.scope, reason, due_at=due)
            await self._schedule_account_on(connection, lease.scope.principal.account_id)
            return ReportingMaterializerTurn(
                "verified" if verified is not None else "failed",
                reason,
                stored.reporting_materialization_id,
            )

    async def _materializer_dirty_on(
        self, connection: Any, outcome: ReportingMaterializationRecord, context: MaterializerContext
    ) -> None:
        from adcp.reporting.ledger.notification_events import delivery_dirty

        scope, reason, evidence = delivery_dirty(outcome, context.obligation)
        await self._dirty_status(connection, scope, reason, after=evidence)

        from adcp.reporting.canonical_json import canonical_json_sha256_v1
        from adcp.reporting.ledger.pg import _json
        from adcp.reporting.ledger.status_snapshot import settle_snapshot_on

        core = await settle_snapshot_on(
            self, connection, account_id=scope.account_id, as_of=outcome.completed_at
        )
        row = await (
            await connection.execute(
                "INSERT INTO reporting_materializer_status_heads"
                " (account_id,consumer_id,max_sequence)"
                " VALUES (%s,%s,1) ON CONFLICT (account_id,consumer_id) DO UPDATE"
                " SET max_sequence=reporting_materializer_status_heads.max_sequence+1"
                " RETURNING max_sequence",
                (scope.account_id, outcome.scope.consumer_id),
            )
        ).fetchone()
        assert row is not None
        account = await (
            await connection.execute(
                "UPDATE reporting_materializer_accounts SET captured_sequence=captured_sequence+1"
                " WHERE account_id=%s RETURNING captured_sequence",
                (scope.account_id,),
            )
        ).fetchone()
        assert account is not None
        boundary = ReportingMaterializerBoundary(
            outcome.scope.principal,
            row[0],
            account[0],
            outcome.reporting_materialization_id,
            outcome.completed_at,
            private_snapshot(core, outcome.scope.principal),
            await self._records(connection, outcome.scope.principal),
        ).to_storage()
        await connection.execute(
            "INSERT INTO reporting_materializer_status_boundaries"
            " (account_id,consumer_id,sequence,account_sequence,reporting_materialization_id,"
            " as_of,input,content_sha256)"
            " VALUES (%s,%s,%s,%s,%s,%s,%s::jsonb,%s)",
            (
                scope.account_id,
                outcome.scope.consumer_id,
                row[0],
                account[0],
                outcome.reporting_materialization_id,
                outcome.completed_at,
                _json(boundary),
                canonical_json_sha256_v1(boundary),
            ),
        )

    @materializer_errors
    async def read_materializer_boundaries(
        self, *, caller: ReportingDeliveryPrincipal, after: int = 0, limit: int = 100
    ) -> tuple[ReportingMaterializerBoundary, ...]:
        if type(after) is not int or after < 0 or type(limit) is not int or not 1 <= limit <= 100:
            raise ReportingMaterializerUsageError(
                "materializer boundary reads require bounded positions"
            )
        async with self._connection() as connection, connection.transaction():
            await self._lock_account(connection, caller.account_id)
            rows = await (
                await connection.execute(
                    "SELECT input, content_sha256=reporting_payload_sha256(input)"
                    " FROM reporting_materializer_status_boundaries"
                    " WHERE account_id=%s AND consumer_id=%s AND sequence>%s"
                    " ORDER BY sequence LIMIT %s",
                    (caller.account_id, caller.consumer_id, after, limit),
                )
            ).fetchall()
            if any(not row[1] for row in rows):
                raise failure("HISTORY_CORRUPT")
            return tuple(decode_materializer_boundary(row[0]) for row in rows)

    @materializer_errors
    async def import_pending_materialization(
        self,
        *,
        scope: ReportingDeliveryScope,
        reporting_materialization_id: str,
        original_external_id: str,
        keys: tuple[ReportingVerificationKey, ...],
    ) -> None:
        """Explicit operator recovery after verifying the original external identity.

        Never infer legacy effect history. An unknown/non-SDK external identity
        requires operator cleanup and a public terminal outcome before recovery.
        """
        await self.materializer_ready()
        async with self._connection() as connection, connection.transaction():
            await self._lock_account(connection, scope.principal.account_id)
            context = await self._materializer_context_on(connection, scope)
            attempt = next(
                (
                    r
                    for r in context.records
                    if isinstance(r, ReportingMaterializationAttempt)
                    and r.reporting_materialization_id == reporting_materialization_id
                    and r.scope == scope
                ),
                None,
            )
            if attempt is None or context.outcome(attempt) is not None:
                raise failure("BINDING_MISMATCH")
            if not context.resumable(attempt):
                raise failure("HISTORY_CORRUPT")
            key = key_for(context.binding, context.obligation, keys)
            request = ReportingDestinationRequest.from_binding(context.binding, attempt, key)
            if original_external_id != request.external_id:
                raise failure("BINDING_MISMATCH")
            await connection.execute(
                "SELECT reporting_materializer_wake(%s)", (scope.principal.account_id,)
            )
            await connection.execute(
                "INSERT INTO reporting_materializer_candidates"
                " (account_id,consumer_id,delivery_config_id,delivery_config_version,"
                " reporting_obligation_id)"
                " VALUES (%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING",
                _scope_args(scope),
            )
            await connection.execute(
                "INSERT INTO reporting_materializer_work"
                " (account_id,consumer_id,delivery_config_id,delivery_config_version,"
                " reporting_obligation_id,reporting_revision_id,reporting_materialization_id,"
                " generation,binding_sha256,verification_key_sha256,external_id,imported,"
                " notifications_enabled)"
                " SELECT account_id,consumer_id,delivery_config_id,delivery_config_version,"
                " reporting_obligation_id,%s,%s,generation,%s,%s,%s,TRUE,%s"
                f" FROM reporting_materializer_candidates WHERE {_SCOPE_WHERE}"  # nosec B608
                " ON CONFLICT (account_id,consumer_id,reporting_materialization_id) DO NOTHING",
                (
                    attempt.reporting_revision_id,
                    reporting_materialization_id,
                    request.binding_fingerprint,
                    verification_key_id(key),
                    request.external_id,
                    self._notifications_enabled,
                    *_scope_args(scope),
                ),
            )
            await connection.execute(
                "UPDATE reporting_materializer_work SET due_at=clock_timestamp()"
                " WHERE account_id=%s AND consumer_id=%s AND reporting_materialization_id=%s"
                " AND state='pending' AND lease_token IS NULL",
                (scope.principal.account_id, scope.consumer_id, reporting_materialization_id),
            )
