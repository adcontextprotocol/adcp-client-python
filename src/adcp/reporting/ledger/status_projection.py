"""Pure status projection shared by polling, source transactions and clock sweeps.

No connection, clock or checkpoint is consulted here. A provisional result
contains closed lifecycle intents; callers apply them and reproject before
publishing. Checkpoints remember notifications, never determine health.
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any, Literal, cast

from adcp.reporting.canonical_json import canonical_json_utf8_v1
from adcp.reporting.ledger.consumer_status import (
    consumer_mismatch_issue_key,
    consumer_statement_conflicts,
    current_consumer_statement,
    project_consumer_mismatch,
    stale_received_grace_deadline,
)
from adcp.reporting.ledger.delivery_models import ReportingDeliveryRecord
from adcp.reporting.ledger.health import (
    ObligationProjection,
    aggregate_reporting_health,
    issue_id_for,
    issue_id_for_occurrence,
    project_obligation_health,
)
from adcp.reporting.ledger.models import (
    ConsumerStatusRecord,
    ReportingAdjustmentRecord,
    ReportingConfiguration,
    ReportingDeliveryEscalation,
    ReportingHealth,
    ReportingIssue,
    ReportingIssueLifecycle,
    ReportingObligationRecord,
    ReportingRevisionRecord,
    iso_duration_to_timedelta,
)
from adcp.reporting.ledger.notification_models import (
    ReportingNotificationError,
    ReportingStatusScope,
    validate_scope_refinement,
)
from adcp.reporting.revision_selection import select_reporting_revision

if TYPE_CHECKING:
    from adcp.reporting.ledger.reconciliation_projection import ReconciliationProjection


@dataclass(frozen=True)
class ReportingStatusSnapshot:
    account_id: str
    as_of: datetime
    configurations: tuple[ReportingConfiguration, ...] = ()
    obligations: tuple[ReportingObligationRecord, ...] = ()
    revisions: tuple[ReportingRevisionRecord, ...] = ()
    statuses: tuple[ConsumerStatusRecord, ...] = ()
    lifecycles: tuple[ReportingIssueLifecycle, ...] = ()
    issue_scopes: tuple[tuple[str, ReportingStatusScope], ...] = ()
    consumer_ids: tuple[str, ...] = ()
    adjustments: tuple[ReportingAdjustmentRecord, ...] = ()
    # Sequence, record kind, ID, consumer namespace (empty for seller records).
    changes: tuple[tuple[int, str, str, str], ...] = ()

    @property
    def max_sequence(self) -> int:
        return max((row[0] for row in self.changes), default=0)


@dataclass(frozen=True)
class StatusLifecycleIntent:
    action: Literal["ensure_mismatch", "retire_mismatch", "refine_scope"]
    lifecycle: ReportingIssueLifecycle
    scope: ReportingStatusScope


@dataclass(frozen=True)
class StatusProjectionInput:
    snapshot: ReportingStatusSnapshot
    scope: ReportingStatusScope
    escalation: ReportingDeliveryEscalation | None = None
    delivery_config_ids: tuple[str, ...] = ()
    media_buy_ids: tuple[str, ...] = ()
    feed_purposes: tuple[str, ...] = ()
    period_start: datetime | None = None
    period_end: datetime | None = None
    # None selects the immutable legacy C representation. Versioned callers
    # pass the complete captured account history, even when it is empty.
    reconciliation: tuple[ReportingDeliveryRecord, ...] | None = None
    consumer_status_enabled: bool = True


@dataclass(frozen=True)
class StatusObligationProjection:
    obligation: ReportingObligationRecord
    projection: ObligationProjection
    revisions: tuple[ReportingRevisionRecord, ...]
    statuses: tuple[ConsumerStatusRecord, ...]
    reconciliation: ReconciliationProjection | None = None


@dataclass(frozen=True)
class StatusProjectionResult:
    scope: ReportingStatusScope
    health: ReportingHealth
    issues: tuple[ReportingIssue, ...]
    obligations: tuple[StatusObligationProjection, ...]
    configurations: tuple[ReportingConfiguration, ...]
    pending_count: int
    next_due_at: datetime | None
    intents: tuple[StatusLifecycleIntent, ...]
    fingerprint: str

    @property
    def publishable(self) -> bool:
        return not self.intents and (
            any(i.severity == self.health for i in self.issues)
            if self.health in {"delayed", "action_required"}
            else not self.issues
        )

    @property
    def issue_ids(self) -> tuple[str, ...]:
        return tuple(sorted({issue.issue_id for issue in self.issues}))[:16]

    def canonical(self) -> dict[str, Any]:
        return {
            "health": self.health,
            "issues": [issue.to_wire() for issue in self.issues],
        }


def status_matches_obligation(
    status: ConsumerStatusRecord, obligation: ReportingObligationRecord
) -> bool:
    return status.account_id == obligation.account_id and (
        status.reporting_obligation_id == obligation.reporting_obligation_id
        or (
            status.generation_key == obligation.generation_key
            and status.report_definition_id == obligation.report_definition_id
            and status.period_start == obligation.period.start
            and status.period_end == obligation.period.end
        )
    )


def mismatch_key(status: ConsumerStatusRecord) -> str:
    return consumer_mismatch_issue_key(
        account_id=status.account_id,
        consumer_id=status.consumer_id,
        delivery_config_id=status.delivery_config_id,
        delivery_config_version=status.delivery_config_version,
        report_definition_id=status.report_definition_id,
        period_start=status.period_start,
        period_end=status.period_end,
    )


def lifecycle_intents(snapshot: ReportingStatusSnapshot) -> tuple[StatusLifecycleIntent, ...]:
    """Plan lifecycle from current chains, including pre-obligation statements.

    Repair old status-without-issue rows using their earliest provable recorded
    observation (or the first superseding revision for stale receipts), never
    a sweeper's later wall clock.
    """
    latest: dict[str, ReportingIssueLifecycle] = {}
    for issue in snapshot.lifecycles:
        if issue.generation >= getattr(latest.get(issue.issue_key), "generation", 0):
            latest[issue.issue_key] = issue
    scopes = dict(snapshot.issue_scopes)
    configurations = {c.generation_key: c for c in snapshot.configurations}
    result: list[StatusLifecycleIntent] = []
    for status in snapshot.statuses:
        if status.superseded:
            continue
        configuration = configurations.get(status.generation_key)
        if configuration is None:
            continue
        owner = next(
            (o for o in snapshot.obligations if status_matches_obligation(status, o)), None
        )
        revisions = tuple(
            r
            for r in snapshot.revisions
            if owner is not None and r.reporting_obligation_id == owner.reporting_obligation_id
        )
        required = None
        if owner is not None:
            selection = select_reporting_revision(
                revisions,
                account_id=owner.account_id,
                reporting_obligation_id=owner.reporting_obligation_id,
                required_finality=owner.required_finality,
            )
            if selection.kind == "corrupt":
                # Keep prior consumer occurrences intact until seller history
                # can establish agreement. Health exposes HISTORY_UNAVAILABLE.
                continue
            if selection.kind == "selected":
                required = selection.revision
        conflicts = consumer_statement_conflicts(
            current=status, current_revision=required, revisions=revisions
        )
        scope = ReportingStatusScope(
            snapshot.account_id,
            status.generation_key,
            owner.reporting_obligation_id if owner is not None else None,
            status.consumer_id,
            cast(Literal["pacing", "analytics", "billing"], configuration.feed_purpose),
        )
        key = mismatch_key(status)
        previous = latest.get(key)
        live = previous if previous is not None and previous.live else None
        if live is not None:
            existing_scope = scopes.get(live.issue_id)
            validate_scope_refinement(existing_scope, scope)
            if existing_scope != scope:
                result.append(StatusLifecycleIntent("refine_scope", live, scope))
        if not conflicts:
            if live is not None:
                result.append(
                    StatusLifecycleIntent(
                        "retire_mismatch",
                        replace(live, issue_state="resolved", retired_at=snapshot.as_of),
                        scope,
                    )
                )
            continue
        if live is not None:
            continue
        generation = previous.generation + 1 if previous is not None else 1
        observed_at = status.recorded_at
        superseders = [
            r.created_at
            for r in revisions
            if r.supersedes_reporting_revision_id == status.reporting_revision_id
        ]
        if status.consumer_status == "received" and superseders:
            observed_at = max(observed_at, min(superseders))
        if previous is not None and previous.retired_at is not None:
            observed_at = max(observed_at, previous.retired_at)
        result.append(
            StatusLifecycleIntent(
                "ensure_mismatch",
                ReportingIssueLifecycle(
                    issue_key=key,
                    issue_id=issue_id_for_occurrence(key, generation),
                    account_id=snapshot.account_id,
                    consumer_id=status.consumer_id,
                    opened_at=observed_at,
                    generation=generation,
                ),
                scope,
            )
        )
    return tuple(result)


def apply_intents_to_snapshot(
    snapshot: ReportingStatusSnapshot, intents: Sequence[StatusLifecycleIntent]
) -> ReportingStatusSnapshot:
    """Pure replay helper. Durable callers persist the same intents first."""
    lifecycles = {(i.issue_key, i.generation): i for i in snapshot.lifecycles}
    scopes = dict(snapshot.issue_scopes)
    for intent in intents:
        issue = intent.lifecycle
        lifecycles[(issue.issue_key, issue.generation)] = issue
        scopes[issue.issue_id] = intent.scope
    return replace(
        snapshot, lifecycles=tuple(lifecycles.values()), issue_scopes=tuple(scopes.items())
    )


def with_replay_lifecycles(
    snapshot: ReportingStatusSnapshot,
    prior: ReportingStatusSnapshot | None,
) -> ReportingStatusSnapshot:
    """Carry derived occurrences across old-writer boundaries lacking lifecycle.

    A later capture may contain an old writer's still-open occurrence after a
    matching statement already retired it in replay. Never regress that state,
    drop a derived recurrence, or broaden its validated persisted scope.
    """
    if prior is None:
        return snapshot
    issues = {(i.issue_key, i.generation): i for i in prior.lifecycles}
    rank = {"open": 0, "acknowledged": 1, "resolved": 2, "waived": 2}
    for issue in snapshot.lifecycles:
        key = (issue.issue_key, issue.generation)
        old = issues.get(key)
        if old is None or (
            rank[issue.issue_state] >= rank[old.issue_state] and old.issue_state != "resolved"
        ):
            issues[key] = issue
    scopes = dict(prior.issue_scopes)
    for issue_id, scope in snapshot.issue_scopes:
        previous = scopes.get(issue_id)
        if previous is None:
            scopes[issue_id] = scope
            continue
        try:
            validate_scope_refinement(previous, scope)
            scopes[issue_id] = scope
        except ReportingNotificationError:
            validate_scope_refinement(scope, previous)
    return replace(snapshot, lifecycles=tuple(issues.values()), issue_scopes=tuple(scopes.items()))


def projection_scopes(snapshot: ReportingStatusSnapshot) -> tuple[ReportingStatusScope, ...]:
    """Seller mutations affect every extant private view of that account.

    Conservative expansion deliberately includes configurations without a
    consumer statement yet. No opaque issue key is parsed to discover scope.
    """
    consumers = {
        None,
        *snapshot.consumer_ids,
        *(s.consumer_id for s in snapshot.statuses),
        *(i.consumer_id for i in snapshot.lifecycles),
    }
    scopes: list[ReportingStatusScope] = []
    for consumer in consumers:
        for configuration in snapshot.configurations:
            scopes.append(
                ReportingStatusScope(
                    snapshot.account_id,
                    configuration.generation_key,
                    consumer_id=consumer,
                    feed_purpose=cast(
                        Literal["pacing", "analytics", "billing"], configuration.feed_purpose
                    ),
                )
            )
        scopes.extend(
            ReportingStatusScope.for_obligation(o, consumer) for o in snapshot.obligations
        )
    return tuple(sorted(scopes, key=lambda s: s.checkpoint_key))


def _selected(scope: ReportingStatusScope, target: ReportingStatusScope) -> bool:
    return (
        scope.account_id == target.account_id
        and (scope.consumer_id is None or scope.consumer_id == target.consumer_id)
        and (
            scope.feed_purpose is None
            or target.feed_purpose is None
            or scope.feed_purpose == target.feed_purpose
        )
        and (
            scope.generation_key is None
            or target.generation_key is None
            or scope.generation_key == target.generation_key
        )
        and (
            scope.reporting_obligation_id is None
            or target.reporting_obligation_id is None
            or scope.reporting_obligation_id == target.reporting_obligation_id
        )
    )


def _sorted_issues(issues: Sequence[ReportingIssue]) -> tuple[ReportingIssue, ...]:
    by_id: dict[str, ReportingIssue] = {}
    for issue in issues:
        existing = by_id.get(issue.issue_id)
        if existing is not None and existing.to_wire() != issue.to_wire():
            raise ReportingNotificationError("conflicting_projected_issue")
        by_id[issue.issue_id] = issue
    return tuple(
        sorted(by_id.values(), key=lambda i: (i.issue_id, canonical_json_utf8_v1(i.to_wire())))
    )


def project_status_scope(value: StatusProjectionInput) -> StatusProjectionResult:
    """Project exact semantics and only deadlines that can change those semantics."""
    result, candidates = _project(value)
    if result.intents:
        return result
    for at in sorted(candidates):
        if at <= value.snapshot.as_of:
            continue
        future, _ = _project(replace(value, snapshot=replace(value.snapshot, as_of=at)))
        if future.intents or future.fingerprint != result.fingerprint:
            return replace(result, next_due_at=at)
    return result


def status_retained_from(
    configurations: Sequence[ReportingConfiguration], as_of: datetime
) -> datetime:
    """The common provable horizon is the latest per-generation boundary."""
    return max(
        (
            (
                max(as_of - timedelta(days=c.status_retention_days), min(c.activated_at, as_of))
                if c.activated_at is not None
                else as_of - timedelta(days=c.status_retention_days)
            )
            for c in configurations
        ),
        default=as_of,
    )


def configuration_selected(
    configuration: ReportingConfiguration,
    *,
    delivery_config_ids: Sequence[str] = (),
    media_buy_ids: Sequence[str] = (),
    feed_purposes: Sequence[str] = (),
) -> bool:
    return (
        (not delivery_config_ids or configuration.delivery_config_id in delivery_config_ids)
        and (not feed_purposes or configuration.feed_purpose in feed_purposes)
        and (
            not media_buy_ids or bool(set(configuration.media_buy_ids).intersection(media_buy_ids))
        )
    )


def period_selected(
    start: datetime,
    end: datetime,
    requested_start: datetime | None,
    requested_end: datetime | None,
) -> bool:
    """One half-open intersection rule for records and aggregate projection."""
    return (requested_start is None or end > requested_start) and (
        requested_end is None or start < requested_end
    )


def _project(value: StatusProjectionInput) -> tuple[StatusProjectionResult, set[datetime]]:
    snapshot, scope = value.snapshot, value.scope
    if snapshot.account_id != scope.account_id:
        raise ReportingNotificationError("invalid_status_scope")
    configurations = tuple(
        c
        for c in snapshot.configurations
        if (scope.generation_key is None or c.generation_key == scope.generation_key)
        and (scope.feed_purpose is None or c.feed_purpose == scope.feed_purpose)
        and configuration_selected(
            c,
            delivery_config_ids=value.delivery_config_ids,
            media_buy_ids=value.media_buy_ids,
            feed_purposes=value.feed_purposes,
        )
    )
    generations = {c.generation_key: c for c in configurations}
    obligations = tuple(
        o
        for o in snapshot.obligations
        # Frozen obligation evidence can outlive the configuration registry in
        # restored/custom stores. Never erase its health by filtering through
        # only the extant configuration rows; apply the same typed selection.
        if (scope.generation_key is None or o.generation_key == scope.generation_key)
        and (scope.feed_purpose is None or o.feed_purpose == scope.feed_purpose)
        and (not value.delivery_config_ids or o.delivery_config_id in value.delivery_config_ids)
        and (not value.feed_purposes or o.feed_purpose in value.feed_purposes)
        and (
            scope.reporting_obligation_id is None
            or o.reporting_obligation_id == scope.reporting_obligation_id
        )
        and (not value.media_buy_ids or set(o.media_buy_ids).intersection(value.media_buy_ids))
        and period_selected(o.period.start, o.period.end, value.period_start, value.period_end)
    )
    intents = tuple(
        i
        for i in lifecycle_intents(snapshot)
        if _selected(i.scope, scope)
        and i.scope.generation_key in generations
        and (value.consumer_status_enabled or i.scope.consumer_id is None)
    )
    live = {i.issue_key: i for i in snapshot.lifecycles if i.live}
    issue_scopes = dict(snapshot.issue_scopes)
    issues: list[ReportingIssue] = []
    projected: list[StatusObligationProjection] = []
    candidates: set[datetime] = set()
    pending = 0
    if (scope.reporting_obligation_id is not None and not obligations) or (
        scope.generation_key is not None and not configurations and not obligations
    ):
        # A retained checkpoint can outlive the registry/obligation that made
        # its scope discoverable. Keep the row and its event identity, publish
        # the absence honestly, and leave no obsolete clock deadline behind.
        issues.append(
            ReportingIssue(
                issue_id=issue_id_for(
                    "retained-scope-history-unavailable-v2",
                    canonical_json_utf8_v1(asdict(scope)).hex(),
                ),
                code="HISTORY_UNAVAILABLE",
                severity="action_required",
                responsible_party="seller",
                recommended_action="contact_seller",
                reporting_obligation_id=scope.reporting_obligation_id,
                delivery_config_id=(
                    scope.generation_key.delivery_config_id if scope.generation_key else None
                ),
                delivery_config_version=(
                    scope.generation_key.delivery_config_version if scope.generation_key else None
                ),
                feed_purpose=scope.feed_purpose,
                message="The retained reporting scope has no available source history.",
            )
        )
    retained_from = status_retained_from(configurations, snapshot.as_of)
    if value.period_start is not None and value.period_start < retained_from:
        for configuration in configurations:
            issues.append(
                ReportingIssue(
                    issue_id=issue_id_for(
                        "history-unavailable-v1",
                        snapshot.account_id,
                        configuration.delivery_config_id,
                        configuration.delivery_config_version,
                        value.period_start.isoformat(),
                        value.period_end.isoformat() if value.period_end else "",
                    ),
                    code="HISTORY_UNAVAILABLE",
                    severity="action_required",
                    responsible_party="seller",
                    recommended_action="contact_seller",
                    delivery_config_id=configuration.delivery_config_id,
                    delivery_config_version=configuration.delivery_config_version,
                    feed_purpose=configuration.feed_purpose,
                    period_start=value.period_start,
                    period_end=min(retained_from, value.period_end or snapshot.as_of),
                    message="The requested horizon predates retained reporting evidence.",
                )
            )
    for obligation in obligations:
        revisions = tuple(
            sorted(
                (
                    r
                    for r in snapshot.revisions
                    if r.reporting_obligation_id == obligation.reporting_obligation_id
                ),
                key=lambda r: (r.created_at, r.reporting_revision_id),
            )
        )
        statuses = tuple(
            s
            for s in snapshot.statuses
            if value.consumer_status_enabled
            and s.consumer_id == scope.consumer_id
            and status_matches_obligation(s, obligation)
        )
        projection = project_obligation_health(
            obligation,
            revisions,
            ledger_as_of=snapshot.as_of,
            scope_closed=snapshot.as_of >= obligation.period.end,
        )
        local = list(projection.issues)
        if obligation.coverage_status != "full":
            local.append(
                ReportingIssue(
                    issue_id=issue_id_for(
                        "coverage-incomplete-v1",
                        snapshot.account_id,
                        obligation.reporting_obligation_id,
                    ),
                    code="REPORTING_COVERAGE_INCOMPLETE",
                    severity="action_required",
                    responsible_party="seller",
                    recommended_action="change_reporting_scope",
                    reporting_obligation_id=obligation.reporting_obligation_id,
                    delivery_config_id=obligation.delivery_config_id,
                    delivery_config_version=obligation.delivery_config_version,
                    feed_purpose=obligation.feed_purpose,
                    media_buy_ids=obligation.media_buy_ids,
                )
            )
            projection = replace(projection, health="action_required")
        current = current_consumer_statement(statuses)
        if (
            value.consumer_status_enabled
            and scope.consumer_id is not None
            and current is None
            and (snapshot.as_of >= obligation.automated_recovery_deadline_at)
        ):
            pending += 1
        generation = generations.get(obligation.generation_key)
        sla = iso_duration_to_timedelta(
            (generation.schedule if generation else obligation.schedule).delivery_sla
        )
        recovery = (
            generation.automated_recovery_window
            if generation
            else obligation.automated_recovery_deadline_at - obligation.period.expected_at
        )
        if current is not None:
            lifecycle = live.get(mismatch_key(current))
            mismatch = project_consumer_mismatch(
                obligation=obligation,
                current_revision=projection.current_revision,
                statuses=statuses,
                seller_health=projection.health,
                revisions=revisions,
                ledger_as_of=snapshot.as_of,
                delivery_sla=sla,
                automated_recovery_window=recovery,
                escalation=value.escalation,
                lifecycle=lifecycle,
            )
            if mismatch is not None and mismatch.published:
                projection = replace(projection, health=mismatch.severity)
                if mismatch.published:
                    local.append(mismatch.issue)
                if current.consumer_status == "received" and current.reporting_revision_id:
                    grace = stale_received_grace_deadline(
                        received_revision_id=current.reporting_revision_id,
                        revisions=revisions,
                        delivery_sla=sla,
                        automated_recovery_window=recovery,
                    )
                    if grace is not None:
                        candidates.add(grace)
                if (
                    lifecycle is not None
                    and value.escalation is not None
                    and value.escalation.consumer_mismatch_escalation is not None
                ):
                    candidates.add(
                        lifecycle.opened_at + value.escalation.consumer_mismatch_escalation
                    )
        reconciliation = None
        if value.reconciliation is not None:
            from adcp.reporting.ledger.reconciliation_projection import project_reconciliation
            from adcp.reporting.ledger.store import LedgerConflictError

            try:
                reconciliation = project_reconciliation(
                    obligation,
                    revisions,
                    snapshot.adjustments,
                    value.reconciliation,
                    consumer_id=scope.consumer_id,
                    as_of=snapshot.as_of,
                )
            except LedgerConflictError:
                local.append(
                    ReportingIssue(
                        issue_id_for(
                            "reconciliation-history-v2",
                            snapshot.account_id,
                            scope.consumer_id,
                            obligation.reporting_obligation_id,
                        ),
                        "HISTORY_UNAVAILABLE",
                        "action_required",
                        "seller",
                        "contact_seller",
                        reporting_obligation_id=obligation.reporting_obligation_id,
                        delivery_config_id=obligation.delivery_config_id,
                        delivery_config_version=obligation.delivery_config_version,
                        feed_purpose=obligation.feed_purpose,
                    )
                )
                projection = replace(projection, health="action_required", satisfied=False)
            else:
                local.extend(reconciliation.issues)
                candidates.update(reconciliation.deadlines)
                if not reconciliation.satisfied:
                    projection = replace(projection, satisfied=False)
                    if projection.health in {"healthy", "complete"}:
                        projection = replace(projection, health="waiting")
                if any(i.severity == "action_required" for i in local):
                    projection = replace(projection, health="action_required")
                elif any(i.severity == "delayed" for i in local):
                    projection = replace(projection, health="delayed")
        projection = replace(projection, issues=_sorted_issues(local))
        projected.append(
            StatusObligationProjection(obligation, projection, revisions, statuses, reconciliation)
        )
        issues.extend(local)
        candidates.update(
            (
                obligation.period.expected_at,
                obligation.automated_recovery_deadline_at,
                obligation.period.end,
            )
        )

    # A pre-obligation missing statement remains a configuration-scoped issue.
    mismatch_keys = {mismatch_key(s) for s in snapshot.statuses}
    for status in snapshot.statuses:
        if (
            not value.consumer_status_enabled
            or status.superseded
            or status.consumer_id != scope.consumer_id
        ):
            continue
        if status.generation_key not in generations or scope.reporting_obligation_id is not None:
            continue
        if (value.period_start is not None and status.period_end <= value.period_start) or (
            value.period_end is not None and status.period_start >= value.period_end
        ):
            continue
        if any(status_matches_obligation(status, o) for o in snapshot.obligations):
            continue
        lifecycle = live.get(mismatch_key(status))
        if lifecycle is not None and lifecycle.published:
            issues.append(
                ReportingIssue(
                    issue_id=lifecycle.issue_id,
                    code="CONSUMER_STATUS_MISMATCH",
                    severity="action_required",
                    responsible_party="seller",
                    recommended_action="contact_seller",
                    delivery_config_id=status.delivery_config_id,
                    delivery_config_version=status.delivery_config_version,
                    feed_purpose=generations[status.generation_key].feed_purpose,
                    reporting_status_id=status.reporting_status_id,
                    opened_at=lifecycle.opened_at,
                    issue_state=lifecycle.issue_state,
                    external_ref=lifecycle.external_ref,
                )
            )
    for issue in live.values():
        if issue.issue_key in mismatch_keys or not issue.published:
            continue
        issue_scope = issue_scopes.get(
            issue.issue_id, ReportingStatusScope(snapshot.account_id, consumer_id=issue.consumer_id)
        )
        if not _selected(issue_scope, scope):
            continue
        if issue_scope.generation_key is not None and issue_scope.generation_key not in generations:
            continue
        if issue_scope.reporting_obligation_id is not None and not any(
            o.reporting_obligation_id == issue_scope.reporting_obligation_id for o in obligations
        ):
            continue
        key = issue_scope.generation_key
        issues.append(
            ReportingIssue(
                issue_id=issue.issue_id,
                code="CONFIGURATION_REQUIRED",
                severity="action_required",
                responsible_party="seller",
                recommended_action="contact_seller",
                reporting_obligation_id=issue_scope.reporting_obligation_id,
                delivery_config_id=key.delivery_config_id if key else None,
                delivery_config_version=key.delivery_config_version if key else None,
                feed_purpose=issue_scope.feed_purpose,
                opened_at=issue.opened_at,
                issue_state=issue.issue_state,
                external_ref=issue.external_ref,
            )
        )
    full_issues = _sorted_issues(issues)
    if scope.reporting_obligation_id is not None and projected:
        health = projected[0].projection.health
    else:
        health = aggregate_reporting_health(
            (p.projection.health for p in projected),
            scope_closed=(value.period_end is None or value.period_end <= snapshot.as_of)
            and all(snapshot.as_of >= o.period.end for o in obligations),
            coverage_complete=all(o.coverage_status == "full" for o in obligations),
        )
    if any(i.severity == "action_required" for i in full_issues):
        health = "action_required"
    elif any(i.severity == "delayed" for i in full_issues):
        health = "delayed"
    canonical = {
        "version": 1,
        "scope": asdict(scope),
        "health": health,
        "issues": [i.to_wire() for i in full_issues],
        "filters": {
            "delivery_config_ids": sorted(value.delivery_config_ids),
            "media_buy_ids": sorted(value.media_buy_ids),
            "feed_purposes": sorted(value.feed_purposes),
            "period_start": value.period_start.isoformat() if value.period_start else None,
            "period_end": value.period_end.isoformat() if value.period_end else None,
        },
    }
    if value.reconciliation is not None:
        canonical["version"] = 2
        canonical["reconciliation"] = [
            (
                p.reconciliation.evidence_json.decode("utf-8")
                if p.reconciliation is not None
                else {
                    "reporting_obligation_id": p.obligation.reporting_obligation_id,
                    "unavailable": True,
                }
            )
            for p in projected
        ]
    fingerprint = hashlib.sha256(canonical_json_utf8_v1(canonical)).hexdigest()
    return (
        StatusProjectionResult(
            scope,
            health,
            full_issues,
            tuple(projected),
            configurations,
            pending,
            None,
            intents,
            fingerprint,
        ),
        candidates,
    )
