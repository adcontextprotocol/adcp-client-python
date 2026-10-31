"""Optional status participants and connection-bound snapshot/lifecycle helpers."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from adcp.reporting._timestamp import aware_timestamp
from adcp.reporting.ledger.models import ConsumerStatusRecord, ReportingConfigurationGenerationKey
from adcp.reporting.ledger.notification_models import ReportingNotificationError
from adcp.reporting.ledger.status_projection import (
    ReportingStatusSnapshot,
    StatusLifecycleIntent,
    lifecycle_intents,
    status_matches_obligation,
)
from adcp.reporting.revision_selection import select_reporting_revision

if TYPE_CHECKING:
    from adcp.reporting.ledger.models import ReportingCaller
    from adcp.reporting.ledger.store import InMemoryReportingLedgerStore


@runtime_checkable
class ReportingStatusParticipant(Protocol):
    """Optional upgrade; existing ReportingLedgerStore implementations stay valid."""

    async def read_status_snapshot(self, *, caller: ReportingCaller) -> ReportingStatusSnapshot: ...

    async def record_consumer_status_with_lifecycle(
        self, status: ConsumerStatusRecord
    ) -> tuple[ConsumerStatusRecord, bool]: ...


def validate_status_evidence(
    status: ConsumerStatusRecord, snapshot: ReportingStatusSnapshot
) -> None:
    """Repeat ingest validation under the source transaction's account lock.

    Transport validation cannot fence a concurrent restatement. These readers
    consume only already captured evidence and never acquire a connection.
    """
    from adcp.reporting.ledger.consumer_status import validate_consumer_status_timing
    from adcp.reporting.ledger.store import LedgerConflictError

    generation = next(
        (c for c in snapshot.configurations if c.generation_key == status.generation_key), None
    )
    if generation is None:
        raise LedgerConflictError(
            "UNKNOWN_CONFIGURATION_GENERATION", "status generation is unavailable"
        )
    if status.report_definition_id != generation.report_definition_id:
        raise LedgerConflictError("REPORT_DEFINITION_MISMATCH", "status definition differs")
    if (status.seller_ledger_snapshot_id is None) != (status.seller_ledger_as_of is None):
        raise LedgerConflictError(
            "SELLER_SNAPSHOT_EVIDENCE_INCOMPLETE", "seller snapshot evidence is incomplete"
        )
    obligation = next(
        (o for o in snapshot.obligations if status_matches_obligation(status, o)),
        None,
    )
    if status.reporting_obligation_id is not None:
        # Trusted legacy writers retained a claimed ID on obligation_missing
        # before an account-local obligation existed. It is never an ownership
        # coordinate: attachment uses the validated generation and period only.
        # Public ingest still resolves every supplied ID before this participant.
        if obligation is None and status.consumer_status != "obligation_missing":
            raise LedgerConflictError("LOOKUP_UNAVAILABLE", "status evidence is unavailable")
        if obligation is not None and (
            obligation.generation_key != status.generation_key
            or obligation.report_definition_id != status.report_definition_id
            or obligation.period.start != status.period_start
            or obligation.period.end != status.period_end
        ):
            raise LedgerConflictError("OBLIGATION_IDENTITY_MISMATCH", "status evidence differs")
    validate_consumer_status_timing(status, generation, as_of=snapshot.as_of)
    required = None
    if obligation is not None:
        revisions = tuple(
            r
            for r in snapshot.revisions
            if r.reporting_obligation_id == obligation.reporting_obligation_id
        )
        selection = select_reporting_revision(
            revisions,
            account_id=obligation.account_id,
            reporting_obligation_id=obligation.reporting_obligation_id,
            required_finality=obligation.required_finality,
        )
        if selection.kind == "corrupt":
            raise LedgerConflictError("HISTORY_UNAVAILABLE", "the revision history requires repair")
        if selection.kind == "selected":
            required = selection.revision
    if status.reporting_revision_id is None:
        return
    revision = next(
        (r for r in snapshot.revisions if r.reporting_revision_id == status.reporting_revision_id),
        None,
    )
    if revision is None or (
        obligation is not None
        and revision.reporting_obligation_id != obligation.reporting_obligation_id
    ):
        raise LedgerConflictError("LOOKUP_UNAVAILABLE", "status evidence is unavailable")
    if status.consumer_status == "content_mismatch" and obligation is not None:
        if required is None or required.reporting_revision_id != status.reporting_revision_id:
            raise LedgerConflictError(
                "REVISION_NOT_CURRENTLY_REQUIRED", "status revision is no longer required"
            )


def memory_snapshot(
    store: InMemoryReportingLedgerStore, account_id: str
) -> ReportingStatusSnapshot:
    """Caller holds the memory transaction. No public method or lock acquisition."""
    statuses = tuple(s for s in store._statuses.values() if s.account_id == account_id)
    issues = tuple(i for i in store._issues.values() if i.account_id == account_id)
    consumer_ids = {s.consumer_id for s in statuses} | {
        i.consumer_id for i in issues if i.consumer_id is not None
    }
    # Reconciliation subclasses retain independently scoped destination/receipt evidence.
    for _, who, _ in getattr(store, "_delivery_records", []):
        if who.account_id == account_id:
            consumer_ids.add(who.consumer_id)
    return ReportingStatusSnapshot(
        account_id=account_id,
        as_of=store._clock(),
        configurations=tuple(
            c for c in store._configurations.values() if c.account_id == account_id
        ),
        obligations=tuple(o for o in store._obligations.values() if o.account_id == account_id),
        revisions=tuple(r for r in store._revisions.values() if r.account_id == account_id),
        statuses=statuses,
        lifecycles=issues,
        issue_scopes=tuple(
            (i, s) for (a, i), s in store._issue_status_scopes.items() if a == account_id
        ),
        consumer_ids=tuple(sorted(consumer_ids)),
        adjustments=tuple(a for a in store._adjustments.values() if a.account_id == account_id),
        changes=tuple(
            (seq, kind, record_id, store._change_owners[seq])
            for seq, a, kind, record_id, _ in store._changes
            if a == account_id
        ),
    )


def apply_memory_intents(
    store: InMemoryReportingLedgerStore, intents: tuple[StatusLifecycleIntent, ...]
) -> None:
    for intent in intents:
        issue = intent.lifecycle
        key = (issue.account_id, issue.issue_key)
        store._issues[key] = issue
        store._issue_generations[key] = max(store._issue_generations.get(key, 0), issue.generation)
        store._dirty_issue(issue, intent.scope, enqueue=False)


def settle_memory_snapshot(
    store: InMemoryReportingLedgerStore, account_id: str
) -> ReportingStatusSnapshot:
    for _ in range(3):
        snapshot = memory_snapshot(store, account_id)
        intents = lifecycle_intents(snapshot)
        if not intents:
            return snapshot
        apply_memory_intents(store, intents)
    raise ReportingNotificationError("status_lifecycle_did_not_converge")


def snapshot_from_storage(raw: dict[str, Any]) -> ReportingStatusSnapshot:
    """Decode the exact SQL boundary input through the ledger's existing mappers."""
    from adcp.reporting.ledger._record_rows import (
        _ADJUSTMENT_COLUMNS,
        _CONFIGURATION_COLUMNS,
        _ISSUE_COLUMNS,
        _OBLIGATION_COLUMNS,
        _REVISION_COLUMNS,
        _STATUS_COLUMNS_BARE,
        _adjustment_from_row,
        _configuration_from_row,
        _issue_from_row,
        _obligation_from_row,
        _revision_from_row,
        _status_from_row,
    )
    from adcp.reporting.ledger.notification_models import decode_status_scope

    dates = {
        "activated_at",
        "deactivated_at",
        "period_start",
        "period_end",
        "expected_at",
        "scope_resolved_at",
        "automated_recovery_deadline_at",
        "created_at",
        "observed_at",
        "data_through",
        "finalized_at",
        "status_as_of",
        "recorded_at",
        "seller_ledger_as_of",
        "opened_at",
        "retired_at",
        "accounting_period_start",
        "accounting_period_end",
        "correction_observed_at",
    }

    def decode(key: str, columns: str, builder: Callable[[Any], Any]) -> tuple[Any, ...]:
        names = [name.strip() for name in columns.split(",")]
        result = []
        for row in raw.get(key, []):
            values = [
                (aware_timestamp(row[n]) if n in dates and row.get(n) is not None else row.get(n))
                for n in names
            ]
            result.append(builder(values))
        return tuple(result)

    configurations = decode(
        "configurations",
        _CONFIGURATION_COLUMNS,
        _configuration_from_row,
    )
    statuses = decode("statuses", _STATUS_COLUMNS_BARE, _status_from_row)
    lifecycles = decode(
        "lifecycles",
        _ISSUE_COLUMNS + ", waived_reporting_status_id, waived_conflict_sha256",
        _issue_from_row,
    )
    waivers = {
        (w["account_id"], w["issue_key"], w["generation"]): w for w in raw.get("waivers", [])
    }
    lifecycles = tuple(
        (
            replace(
                issue,
                waived_reporting_status_id=binding["reporting_status_id"],
                waived_conflict_sha256=binding["conflict_sha256"],
            )
            if (binding := waivers.get((issue.account_id, issue.issue_key, issue.generation)))
            else issue
        )
        for issue in lifecycles
    )
    consumers = {row["consumer_id"] for row in raw.get("consumers", [])}
    consumers.update(s.consumer_id for s in statuses)
    consumers.update(i.consumer_id for i in lifecycles if i.consumer_id is not None)
    return ReportingStatusSnapshot(
        account_id=raw["account_id"],
        as_of=aware_timestamp(raw["as_of"]),
        configurations=configurations,
        obligations=decode("obligations", _OBLIGATION_COLUMNS, _obligation_from_row),
        revisions=decode("revisions", _REVISION_COLUMNS, _revision_from_row),
        statuses=statuses,
        lifecycles=lifecycles,
        issue_scopes=tuple(
            (row["issue_id"], decode_status_scope(row["scope"]))
            for row in raw.get("issue_scopes", [])
        ),
        consumer_ids=tuple(sorted(consumers)),
        adjustments=decode("adjustments", _ADJUSTMENT_COLUMNS, _adjustment_from_row),
        changes=tuple(
            (row["seq"], row["record_kind"], row["record_id"], row["consumer_id"])
            for row in raw.get("changes", [])
        ),
        owed_through=tuple(
            (
                ReportingConfigurationGenerationKey(
                    row["account_id"],
                    row["consumer_id"],
                    row["delivery_config_id"],
                    int(row["delivery_config_version"]),
                    row["offering_id"],
                ),
                aware_timestamp(row["owed_through"]),
            )
            for row in raw.get("owed_sets", [])
        ),
    )
