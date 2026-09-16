"""Deriving reporting health from immutable evidence and the clock.

Health is never stored.  It is a pure function of the obligations, the
revisions associated with them, and the snapshot's clock boundary -- which is
what makes it impossible for a stored health column to go stale, and what makes
two readers of one snapshot agree.

The five states answer one question each:

``waiting``
    Nothing is due yet.  ``ledger_as_of`` has not reached ``expected_at``.
``healthy``
    Everything due has been produced, and the scope is still open.
``delayed``
    Something due is late and automated recovery is still running.  Bounded by
    ``automated_recovery_deadline_at`` -- a dead feed must never be parked here.
``action_required``
    A human on the named ``responsible_party`` must act.
``complete``
    The queried scope is closed and every final obligation is satisfied.

A committed zero-row revision satisfies an obligation exactly like any other.
Absence is represented only by an empty association set, never by a zero.
"""

from __future__ import annotations

import base64
import hashlib
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone

from adcp.reporting.canonical_json import canonical_json_utf8_v1
from adcp.reporting.ledger.models import (
    ReportingHealth,
    ReportingIssue,
    ReportingObligationRecord,
    ReportingProductionStatus,
    ReportingRevisionRecord,
)

__all__ = [
    "ObligationProjection",
    "aggregate_reporting_health",
    "current_required_revision",
    "issue_id_for",
    "issue_id_for_occurrence",
    "project_obligation_health",
]


def _utc(value: datetime) -> datetime:
    return value.astimezone(timezone.utc)


@dataclass(frozen=True)
class ObligationProjection:
    """One obligation's derived status at a snapshot boundary."""

    health: ReportingHealth
    production_status: ReportingProductionStatus
    issues: tuple[ReportingIssue, ...]
    satisfied: bool
    current_revision: ReportingRevisionRecord | None


def issue_id_for(kind: str, *parts: object) -> str:
    """A stable, derived issue identity.

    Derived rather than stored because these conditions are monotone for one
    immutable obligation: once a qualifying revision is associated,
    ``REPORT_OVERDUE`` cannot recur.  That makes a stateless identity correct
    without claiming a general issue-lifecycle ledger -- and it means a
    consumer polling twice deduplicates on the same id both times.
    """
    digest = hashlib.sha256(canonical_json_utf8_v1([kind, *[str(part) for part in parts]])).digest()
    return "rpti_" + base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")[:32]


def issue_id_for_occurrence(issue_key: str, generation: int) -> str:
    """The id for one *occurrence* of a stored, non-monotone condition.

    :func:`issue_id_for` is enough for conditions that cannot recur once
    satisfied.  A consumer mismatch can: the buyer supersedes, the seller
    restates, the disagreement clears and comes back.  AdCP 3.2.0-rc.3 requires
    a recurrence after retirement to get a *new* ``issue_id``, so identity has
    to include which occurrence this is -- the generation counter held by
    :class:`~adcp.reporting.ledger.models.ReportingIssueLifecycle`.

    Folding the generation into the digest rather than appending it keeps the
    id opaque, so nothing downstream can parse it back into "how many times
    has this buyer complained".
    """
    digest = hashlib.sha256(
        canonical_json_utf8_v1(["core-issue-occurrence-v1", issue_key, generation])
    ).digest()
    return "rpti_" + base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")[:32]


def project_obligation_health(
    obligation: ReportingObligationRecord,
    revisions: Sequence[ReportingRevisionRecord],
    *,
    ledger_as_of: datetime,
    scope_closed: bool,
) -> ObligationProjection:
    """Classify one obligation's immutable evidence at the snapshot's clock."""
    boundary = _utc(ledger_as_of)
    qualifying = [
        revision
        for revision in revisions
        if obligation.required_finality == "snapshot" or revision.finality == "official"
    ]
    current = _current_revision(qualifying)

    if obligation.currency is None:
        return ObligationProjection(
            health="action_required",
            production_status="published" if revisions else "pending",
            issues=(
                ReportingIssue(
                    issue_id=issue_id_for(
                        "core-currency-unresolved-v1", obligation.reporting_obligation_id
                    ),
                    code="HISTORY_UNAVAILABLE",
                    severity="action_required",
                    responsible_party="seller",
                    recommended_action="contact_seller",
                    reporting_obligation_id=obligation.reporting_obligation_id,
                    delivery_config_id=obligation.delivery_config_id,
                    delivery_config_version=obligation.delivery_config_version,
                    feed_purpose=obligation.feed_purpose,
                    media_buy_ids=obligation.media_buy_ids,
                    period_start=obligation.period.start,
                    period_end=obligation.period.end,
                    message=(
                        "Currency was not retained for this legacy obligation; "
                        "verified historical evidence is required."
                    ),
                ),
            ),
            satisfied=False,
            current_revision=current,
        )

    readable = [revision for revision in qualifying if revision.readable]
    if readable:
        return ObligationProjection(
            health="complete" if scope_closed else "healthy",
            production_status="published",
            issues=(),
            satisfied=True,
            current_revision=current,
        )

    if qualifying:
        # A qualifying revision exists but nothing is readable. Core's promise
        # is retained readability, so this is a seller repair, not a wait.
        ordered = sorted(
            qualifying, key=lambda item: (_utc(item.created_at), item.reporting_revision_id)
        )
        once_readable = [item for item in ordered if item.readable_at_commit]
        opening = once_readable[-1] if once_readable else ordered[0]
        return ObligationProjection(
            health="action_required",
            production_status="published",
            issues=(_unreadable_issue(obligation, opening.reporting_revision_id),),
            satisfied=False,
            current_revision=current,
        )

    if revisions:
        # Revisions exist but none meet the required finality: the seller has
        # published something, so production is not pending -- it is simply not
        # yet official.
        production_status: ReportingProductionStatus = "published"
    elif boundary < _utc(obligation.period.expected_at):
        production_status = "not_due"
    else:
        production_status = "pending"

    if boundary < _utc(obligation.period.expected_at):
        return ObligationProjection(
            health="waiting",
            production_status=production_status,
            issues=(),
            satisfied=False,
            current_revision=None,
        )

    health: ReportingHealth = (
        "delayed"
        if boundary < _utc(obligation.automated_recovery_deadline_at)
        else "action_required"
    )
    return ObligationProjection(
        health=health,
        production_status=production_status,
        issues=(_overdue_issue(obligation, health),),
        satisfied=False,
        current_revision=None,
    )


def current_required_revision(
    obligation: ReportingObligationRecord,
    revisions: Sequence[ReportingRevisionRecord],
) -> ReportingRevisionRecord | None:
    """The revision the seller currently requires for this obligation.

    Shared by the health projection and the ``sync_reporting_status`` ingest on
    purpose. If the two computed "current" differently, a buyer could file a
    ``content_mismatch`` the ingest accepts and the projection then treats as
    naming a superseded revision -- a statement permanently stuck disputing
    bytes nobody stands behind.

    Applies the obligation's ``required_finality`` first, then takes the
    unsuperseded leaf: an official revision is terminal so it wins outright,
    and among snapshots the current one is whichever no other supersedes.
    """
    qualifying = [
        revision
        for revision in revisions
        if obligation.required_finality == "snapshot" or revision.finality == "official"
    ]
    return _current_revision(qualifying)


def _current_revision(
    revisions: Sequence[ReportingRevisionRecord],
) -> ReportingRevisionRecord | None:
    """The unsuperseded leaf of a revision chain.

    An official revision is terminal, so it wins outright.  Among snapshots,
    the current one is whichever no other snapshot supersedes.
    """
    if not revisions:
        return None
    official = [item for item in revisions if item.finality == "official"]
    if official:
        return max(official, key=lambda item: (_utc(item.created_at), item.reporting_revision_id))
    superseded = {
        item.supersedes_reporting_revision_id
        for item in revisions
        if item.supersedes_reporting_revision_id
    }
    leaves = [item for item in revisions if item.reporting_revision_id not in superseded]
    if not leaves:
        return None
    return max(leaves, key=lambda item: (_utc(item.created_at), item.reporting_revision_id))


def _overdue_issue(
    obligation: ReportingObligationRecord, severity: ReportingHealth
) -> ReportingIssue:
    return ReportingIssue(
        issue_id=issue_id_for(
            "core-report-overdue-v1",
            obligation.reporting_obligation_id,
            obligation.required_finality,
        ),
        code="REPORT_OVERDUE",
        severity="delayed" if severity == "delayed" else "action_required",
        responsible_party="seller",
        recommended_action="wait_for_retry" if severity == "delayed" else "contact_seller",
        reporting_obligation_id=obligation.reporting_obligation_id,
        delivery_config_id=obligation.delivery_config_id,
        delivery_config_version=obligation.delivery_config_version,
        feed_purpose=obligation.feed_purpose,
        media_buy_ids=obligation.media_buy_ids,
        period_start=obligation.period.start,
        period_end=obligation.period.end,
        expected_at=obligation.period.expected_at,
    )


def _unreadable_issue(
    obligation: ReportingObligationRecord, opening_revision_id: str
) -> ReportingIssue:
    return ReportingIssue(
        issue_id=issue_id_for(
            "core-revision-unreadable-v1",
            obligation.reporting_obligation_id,
            obligation.required_finality,
            opening_revision_id,
        ),
        code="RESOURCE_EXPIRED",
        severity="action_required",
        responsible_party="seller",
        recommended_action="contact_seller",
        reporting_obligation_id=obligation.reporting_obligation_id,
        delivery_config_id=obligation.delivery_config_id,
        delivery_config_version=obligation.delivery_config_version,
        feed_purpose=obligation.feed_purpose,
        media_buy_ids=obligation.media_buy_ids,
        period_start=obligation.period.start,
        period_end=obligation.period.end,
        expected_at=obligation.period.expected_at,
    )


def aggregate_reporting_health(
    obligation_health: Iterable[ReportingHealth],
    *,
    scope_closed: bool,
    coverage_complete: bool,
) -> ReportingHealth:
    """Roll obligation health up to a scope.

    Worst-case wins, with one addition: incomplete coverage is itself
    ``action_required`` regardless of the obligations underneath it.  A scope
    whose denominator is not fully known cannot honestly be called healthy --
    the obligations that *are* present may simply be the ones that happened to
    resolve.
    """
    values = list(obligation_health)
    if not coverage_complete:
        return "action_required"
    if "action_required" in values:
        return "action_required"
    if "delayed" in values:
        return "delayed"
    if scope_closed and values and all(value == "complete" for value in values):
        return "complete"
    if any(value in {"healthy", "complete"} for value in values):
        return "healthy"
    return "waiting"
