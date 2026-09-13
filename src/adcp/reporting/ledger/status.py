"""``get_reporting_status``: one authoritative read over the obligation ledger.

This is the buyer's answer to "do I have definitive reporting for this period --
and if not, whose problem is it?".  Polling it is the authoritative recovery
path; a polling-only seller is fully conformant, and push is a doorbell, never
the source of truth.

The handler is framework-agnostic on purpose.  It takes a plain request mapping
plus an authenticated caller and returns a plain response mapping, so the same
projection serves an :mod:`adcp.server` handler, a bare ASGI route, a CLI, or a
test.  Nothing here imports a web framework.

Three views:

``summary``
    Health, coverage, ``data_through``, obligation counts, and issues for a
    scope.  No records, no cursor.
``periods``
    The paginated flat union of obligations, revisions, adjustments, and (when
    the preview ingest is on) this caller's consumer statuses.  Every page from
    one cursor repeats the same ``ledger_snapshot_id`` and ``ledger_as_of``.
``revision``
    One exact revision plus its adjustments.

Caller isolation is not a filter applied at the end; it is a parameter to every
lookup.  Unknown and unauthorized identifiers deliberately produce the *same*
shape, because a distinguishable "not found" is an enumeration oracle.
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Literal

from adcp.reporting.canonical_json import canonical_json_utf8_v1
from adcp.reporting.ledger.health import aggregate_reporting_health, project_obligation_health
from adcp.reporting.ledger.models import (
    ConsumerStatusRecord,
    ReportingAdjustmentRecord,
    ReportingHealth,
    ReportingIssue,
    ReportingObligationRecord,
    ReportingRevisionRecord,
)
from adcp.reporting.ledger.store import (
    LedgerConflictError,
    ReportingLedgerStore,
    decode_cursor,
    encode_cursor,
)

__all__ = [
    "ReportingStatusCaller",
    "ReportingStatusHandler",
    "ReportingStatusView",
]

ReportingStatusView = Literal["summary", "periods", "revision"]

_DEFAULT_PAGE_SIZE = 100


def _utc(value: datetime) -> datetime:
    return value.astimezone(timezone.utc)


def _iso(value: datetime) -> str:
    return _utc(value).isoformat().replace("+00:00", "Z")


@dataclass(frozen=True)
class ReportingStatusCaller:
    """The authenticated caller, resolved from transport, never from the body.

    ``consumer_id`` is what scopes consumer-status visibility.  A request body
    that asserts a buyer principal is ignored: identity comes from the
    authenticated transport or it does not exist.
    """

    account_id: str
    consumer_id: str


class ReportingStatusHandler:
    """Projects a ledger store onto the ``get_reporting_status`` wire shape."""

    def __init__(
        self,
        store: ReportingLedgerStore,
        *,
        page_size: int = _DEFAULT_PAGE_SIZE,
        consumer_status_enabled: bool = False,
    ) -> None:
        # Deliberately no clock. ``ledger_as_of`` is the *store's* observation
        # boundary -- in Postgres, the database clock -- because that is what
        # makes every page of one cursor describe the same instant. A handler
        # with its own clock could disagree with the snapshot it is paginating.
        self._store = store
        self._page_size = page_size
        self._consumer_status_enabled = consumer_status_enabled

    async def handle(
        self, request: dict[str, Any], *, caller: ReportingStatusCaller
    ) -> dict[str, Any]:
        """Serve one ``get_reporting_status`` request."""
        view: ReportingStatusView = request.get("view", "summary")
        if view not in {"summary", "periods", "revision"}:
            raise LedgerConflictError("INVALID_VIEW", f"unsupported reporting status view {view!r}")
        if view == "revision":
            return await self._revision_view(request, caller=caller)

        filters = _filters(request)
        snapshot = await self._store.open_snapshot(
            account_id=caller.account_id, filters_fingerprint=_fingerprint(filters)
        )
        cursor = (request.get("pagination") or {}).get("cursor")
        offset = 0
        if cursor:
            decoded = decode_cursor(cursor)
            if decoded.get("snapshot") != snapshot.snapshot_id:
                # A cursor from another snapshot, caller, account, or filter set
                # is unusable: continuing it would silently blend two ledger
                # boundaries into one "consistent" walk.
                raise LedgerConflictError(
                    "CURSOR_SNAPSHOT_MISMATCH",
                    "this cursor belongs to a different ledger snapshot or filter set; "
                    "restart the walk",
                )
            offset = int(decoded.get("offset", 0))

        changes_after = request.get("changes_after")
        page = await self._store.read_page(
            snapshot=snapshot,
            consumer_id=caller.consumer_id if self._consumer_status_enabled else None,
            delivery_config_ids=filters["delivery_config_ids"],
            media_buy_ids=filters["media_buy_ids"],
            offset=offset,
            limit=self._page_size,
            changes_after_sequence=_checkpoint_sequence(changes_after),
        )
        if view == "periods":
            return await self._periods_view(page, snapshot, caller=caller, request=request)
        return await self._summary_view(snapshot, caller=caller, request=request, filters=filters)

    # -- summary ---------------------------------------------------------

    async def _summary_view(
        self,
        snapshot: Any,
        *,
        caller: ReportingStatusCaller,
        request: dict[str, Any],
        filters: dict[str, Any],
    ) -> dict[str, Any]:
        obligations, projections, issues = await self._project_scope(
            snapshot, caller=caller, filters=filters
        )
        scope_closed = _scope_closed(obligations, ledger_as_of=snapshot.ledger_as_of)
        health = aggregate_reporting_health(
            (projection.health for projection in projections.values()),
            scope_closed=scope_closed,
            coverage_complete=all(
                obligation.coverage_status == "full" for obligation in obligations
            ),
        )
        data_through = _scope_data_through(projections.values())
        counts = {
            "total": len(obligations),
            "satisfied": sum(1 for item in projections.values() if item.satisfied),
            "delayed": sum(1 for item in projections.values() if item.health == "delayed"),
            "action_required": sum(
                1 for item in projections.values() if item.health == "action_required"
            ),
        }
        return {
            "view": "summary",
            "ledger_snapshot_id": snapshot.snapshot_id,
            "ledger_as_of": _iso(snapshot.ledger_as_of),
            "account_id": caller.account_id,
            "scope": {
                "scope_closed": scope_closed,
                "coverage_complete": all(
                    obligation.coverage_status == "full" for obligation in obligations
                ),
                "delivery_config_generations": sorted(
                    {
                        f"{item.delivery_config_id}@{item.delivery_config_version}"
                        for item in obligations
                    }
                ),
                "feed_purposes": sorted({item.feed_purpose for item in obligations}),
                "finality": sorted({item.required_finality for item in obligations}),
            },
            "health": health,
            "coverage": _coverage_roll_up(obligations),
            "data_through": _iso(data_through) if data_through else None,
            "next_expected_at": _next_expected(obligations, ledger_as_of=snapshot.ledger_as_of),
            "obligation_counts": counts,
            "issues": [issue.to_wire() for issue in issues],
        }

    # -- periods ---------------------------------------------------------

    async def _periods_view(
        self,
        page: Any,
        snapshot: Any,
        *,
        caller: ReportingStatusCaller,
        request: dict[str, Any],
    ) -> dict[str, Any]:
        obligations: list[dict[str, Any]] = []
        for obligation in page.obligations:
            revisions = await self._store.list_revisions(
                account_id=caller.account_id,
                reporting_obligation_id=obligation.reporting_obligation_id,
            )
            statuses = await self._statuses_for(obligation, caller=caller)
            projection = project_obligation_health(
                obligation,
                revisions,
                ledger_as_of=snapshot.ledger_as_of,
                scope_closed=_obligation_closed(obligation, snapshot.ledger_as_of),
            )
            projection_issues = list(projection.issues)
            health = projection.health
            production_status = projection.production_status
            if self._consumer_status_enabled:
                from adcp.reporting.ledger.consumer_status import project_consumer_mismatch

                mismatch = project_consumer_mismatch(
                    obligation=obligation,
                    current_revision=projection.current_revision,
                    statuses=statuses,
                    seller_health=health,
                )
                if mismatch is not None:
                    # Only this caller's view degrades. One consumer's
                    # statement never changes another caller's view or the
                    # seller's advertised reliability statistics.
                    health = "action_required"
                    projection_issues.append(mismatch)
            obligations.append(
                _obligation_to_wire(
                    obligation,
                    revisions=revisions,
                    health=health,
                    production_status=production_status,
                    issues=projection_issues,
                    statuses=statuses if self._consumer_status_enabled else (),
                )
            )

        payload: dict[str, Any] = {
            "view": "periods",
            "ledger_snapshot_id": snapshot.snapshot_id,
            "ledger_as_of": _iso(snapshot.ledger_as_of),
            "changes_checkpoint": _encode_checkpoint(snapshot.max_sequence),
            "account_id": caller.account_id,
            "periods": obligations,
            "revisions": [_revision_to_wire(item) for item in page.revisions],
            "adjustments": [_adjustment_to_wire(item) for item in page.adjustments],
            "pagination": {
                "total_count": page.total_count,
                "has_more": page.has_more,
                **({"cursor": page.cursor} if page.cursor else {}),
            },
        }
        if self._consumer_status_enabled:
            payload["consumer_statuses"] = [
                _consumer_status_to_wire(item) for item in page.consumer_statuses
            ]
        return payload

    async def _statuses_for(
        self, obligation: ReportingObligationRecord, *, caller: ReportingStatusCaller
    ) -> tuple[ConsumerStatusRecord, ...]:
        if not self._consumer_status_enabled:
            return ()
        return await self._store.list_consumer_statuses(
            account_id=caller.account_id,
            consumer_id=caller.consumer_id,
            reporting_obligation_ids=[obligation.reporting_obligation_id],
        )

    # -- revision --------------------------------------------------------

    async def _revision_view(
        self, request: dict[str, Any], *, caller: ReportingStatusCaller
    ) -> dict[str, Any]:
        revision_id = request.get("reporting_revision_id")
        if not revision_id:
            raise LedgerConflictError(
                "MISSING_REVISION_ID", "a revision view requires reporting_revision_id"
            )
        revision = await self._store.get_revision(
            account_id=caller.account_id, reporting_revision_id=revision_id
        )
        if revision is None:
            # Identical shape for unknown and unauthorized: a distinguishable
            # "not found" would let a caller enumerate another tenant's ids.
            raise LedgerConflictError(
                "LOOKUP_UNAVAILABLE", "no such revision is available to this caller"
            )
        adjustments = await self._store.list_adjustments(
            account_id=caller.account_id, reporting_revision_ids=[revision_id]
        )
        snapshot = await self._store.open_snapshot(
            account_id=caller.account_id,
            filters_fingerprint=_fingerprint({"revision": revision_id}),
        )
        return {
            "view": "revision",
            "ledger_snapshot_id": snapshot.snapshot_id,
            "ledger_as_of": _iso(snapshot.ledger_as_of),
            "account_id": caller.account_id,
            "revision": _revision_to_wire(revision),
            "adjustments": [_adjustment_to_wire(item) for item in adjustments],
        }

    # -- shared projection ------------------------------------------------

    async def _project_scope(
        self, snapshot: Any, *, caller: ReportingStatusCaller, filters: dict[str, Any]
    ) -> tuple[
        list[ReportingObligationRecord],
        dict[str, Any],
        list[ReportingIssue],
    ]:
        """Walk the whole snapshot for a summary.

        A summary is a roll-up, so it deliberately reads every obligation in
        scope rather than a page: a summary computed from one page would report
        health for an arbitrary subset and call it the scope.
        """
        obligations: list[ReportingObligationRecord] = []
        offset = 0
        while True:
            page = await self._store.read_page(
                snapshot=snapshot,
                consumer_id=caller.consumer_id if self._consumer_status_enabled else None,
                delivery_config_ids=filters["delivery_config_ids"],
                media_buy_ids=filters["media_buy_ids"],
                offset=offset,
                limit=self._page_size,
                changes_after_sequence=None,
            )
            obligations.extend(page.obligations)
            if not page.has_more:
                break
            offset += self._page_size

        projections: dict[str, Any] = {}
        issues: list[ReportingIssue] = []
        for obligation in obligations:
            revisions = await self._store.list_revisions(
                account_id=caller.account_id,
                reporting_obligation_id=obligation.reporting_obligation_id,
            )
            projection = project_obligation_health(
                obligation,
                revisions,
                ledger_as_of=snapshot.ledger_as_of,
                scope_closed=_obligation_closed(obligation, snapshot.ledger_as_of),
            )
            projections[obligation.reporting_obligation_id] = projection
            issues.extend(projection.issues)
            if self._consumer_status_enabled:
                from adcp.reporting.ledger.consumer_status import project_consumer_mismatch

                statuses = await self._statuses_for(obligation, caller=caller)
                mismatch = project_consumer_mismatch(
                    obligation=obligation,
                    current_revision=projection.current_revision,
                    statuses=statuses,
                    seller_health=projection.health,
                )
                if mismatch is not None:
                    issues.append(mismatch)
                    projections[obligation.reporting_obligation_id] = _degraded(projection)
        return obligations, projections, issues


def _degraded(projection: Any) -> Any:
    from dataclasses import replace

    return replace(projection, health="action_required")


def _filters(request: dict[str, Any]) -> dict[str, Any]:
    return {
        "delivery_config_ids": list(request.get("delivery_config_ids") or []) or None,
        "media_buy_ids": list(request.get("media_buy_ids") or []) or None,
        "feed_purposes": list(request.get("feed_purposes") or []) or None,
    }


def _fingerprint(value: object) -> str:
    return hashlib.sha256(canonical_json_utf8_v1(value)).hexdigest()


def _encode_checkpoint(sequence: int) -> str:
    return encode_cursor({"seq": sequence})


def _checkpoint_sequence(checkpoint: str | None) -> int | None:
    if not checkpoint:
        return None
    value = decode_cursor(checkpoint).get("seq")
    if not isinstance(value, int):
        raise LedgerConflictError("INVALID_CHECKPOINT", "changes_after is not a valid checkpoint")
    return value


def _scope_closed(
    obligations: Sequence[ReportingObligationRecord], *, ledger_as_of: datetime
) -> bool:
    """A scope is closed when no further obligation can arrive inside it.

    An empty denominator is vacuously closed -- and vacuously complete, which
    is a valid answer and not an error.
    """
    return all(_obligation_closed(item, ledger_as_of) for item in obligations)


def _obligation_closed(obligation: ReportingObligationRecord, ledger_as_of: datetime) -> bool:
    return _utc(ledger_as_of) >= _utc(obligation.period.end)


def _scope_data_through(projections: Any) -> datetime | None:
    """The earliest watermark across the scope, not the latest.

    A scope is only good through its *weakest* member: reporting the newest
    watermark would claim completeness the laggard has not delivered.
    """
    watermarks = [
        projection.current_revision.data_through
        for projection in projections
        if projection.current_revision is not None
        and projection.current_revision.data_through is not None
    ]
    return min(watermarks) if watermarks else None


def _next_expected(
    obligations: Sequence[ReportingObligationRecord], *, ledger_as_of: datetime
) -> str | None:
    upcoming = [
        item.period.expected_at
        for item in obligations
        if _utc(item.period.expected_at) > _utc(ledger_as_of)
    ]
    return _iso(min(upcoming)) if upcoming else None


def _coverage_roll_up(obligations: Sequence[ReportingObligationRecord]) -> dict[str, Any]:
    media_buy_ids = sorted(
        {item for obligation in obligations for item in obligation.media_buy_ids}
    )
    package_ids = sorted({item for obligation in obligations for item in obligation.package_ids})
    statuses = {obligation.coverage_status for obligation in obligations}
    status = "full" if statuses <= {"full"} else ("partial" if "full" in statuses else "none")
    if "unknown" in statuses:
        status = "unknown"
    evaluated = (
        max(obligation.scope_resolved_at for obligation in obligations) if obligations else None
    )
    return {
        "status": status,
        "evaluated_at": _iso(evaluated) if evaluated else None,
        "media_buy_ids": media_buy_ids,
        "fully_covered_media_buy_ids": media_buy_ids if status == "full" else [],
        "partially_covered_media_buy_ids": [],
        "unsupported_media_buy_ids": [],
        "unknown_media_buy_ids": [],
        "package_ids": package_ids,
        "covered_package_ids": package_ids if status == "full" else [],
        "unsupported_package_ids": [],
        "unknown_package_ids": [],
        "limitations": [],
    }


def _obligation_to_wire(
    obligation: ReportingObligationRecord,
    *,
    revisions: Sequence[ReportingRevisionRecord],
    health: ReportingHealth,
    production_status: str,
    issues: Sequence[ReportingIssue],
    statuses: Sequence[ConsumerStatusRecord],
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "reporting_obligation_id": obligation.reporting_obligation_id,
        "delivery_config_id": obligation.delivery_config_id,
        "delivery_config_version": obligation.delivery_config_version,
        "report_definition_id": obligation.report_definition_id,
        "feed_purpose": obligation.feed_purpose,
        "reporting_profile": obligation.reporting_profile,
        "account_id": obligation.account_id,
        "media_buy_ids": list(obligation.media_buy_ids),
        "scope_resolved_at": _iso(obligation.scope_resolved_at),
        "period": {
            "start": _iso(obligation.period.start),
            "end": _iso(obligation.period.end),
        },
        "expected_at": _iso(obligation.period.expected_at),
        "schedule": {
            "period_duration": obligation.schedule.period_duration,
            "alignment": obligation.schedule.alignment,
            "delivery_sla": obligation.schedule.delivery_sla,
        },
        "required_finality": obligation.required_finality,
        "reconciliation_mode": "delivery_only",
        "reconciliation_status": "not_required",
        "health": health,
        "production_status": production_status,
        "revision_count": len(revisions),
        "adjustment_count": 0,
        "issues": [issue.to_wire() for issue in issues],
    }
    if statuses:
        current = next((item for item in statuses if not item.superseded), None)
        payload["consumer_status_count"] = len(statuses)
        if current is not None:
            payload["current_consumer_status_id"] = current.reporting_status_id
    return payload


def _revision_to_wire(revision: ReportingRevisionRecord) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "reporting_revision_id": revision.reporting_revision_id,
        "revision_content_sha256": revision.revision_content_sha256,
        "reporting_obligation_id": revision.reporting_obligation_id,
        "account_id": revision.account_id,
        "finality": revision.finality,
        "observed_at": _iso(revision.observed_at),
        "data_through": _iso(revision.data_through) if revision.data_through else None,
        "data_through_precision": "exact" if revision.data_through else "unknown",
        "row_count": revision.row_count,
        "control_totals": [
            {"name": name, "value": value} for name, value in revision.control_totals
        ],
        "created_at": _iso(revision.created_at),
    }
    optional = {
        "supersedes_reporting_revision_id": revision.supersedes_reporting_revision_id,
        "finality_basis": revision.finality_basis,
        "finality_policy_id": revision.finality_policy_id,
        "finalized_at": _iso(revision.finalized_at) if revision.finalized_at else None,
    }
    payload.update({key: value for key, value in optional.items() if value is not None})
    return payload


def _adjustment_to_wire(adjustment: ReportingAdjustmentRecord) -> dict[str, Any]:
    return {
        "reporting_adjustment_id": adjustment.reporting_adjustment_id,
        "adjusts_reporting_revision_id": adjustment.adjusts_reporting_revision_id,
        "reason_code": adjustment.reason_code,
        "accounting_period": {
            "start": _iso(adjustment.accounting_period_start),
            "end": _iso(adjustment.accounting_period_end),
        },
        "control_total_deltas": [
            {"name": name, "value": value} for name, value in adjustment.control_total_deltas
        ],
        "correction_observed_at": _iso(adjustment.correction_observed_at),
        "created_at": _iso(adjustment.created_at),
    }


def _consumer_status_to_wire(status: ConsumerStatusRecord) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "reporting_status_id": status.reporting_status_id,
        "delivery_config_id": status.delivery_config_id,
        "delivery_config_version": status.delivery_config_version,
        "report_definition_id": status.report_definition_id,
        "period": {
            "start": _iso(status.period_start),
            "end": _iso(status.period_end),
            "source_timezone": status.period_source_timezone,
        },
        "consumer_status": status.consumer_status,
        "status_as_of": _iso(status.status_as_of),
        "recorded_at": _iso(status.recorded_at),
    }
    optional = {
        "supersedes_reporting_status_id": status.supersedes_reporting_status_id,
        "reporting_obligation_id": status.reporting_obligation_id,
        "reporting_revision_id": status.reporting_revision_id,
        "observed_revision_content_sha256": status.observed_revision_content_sha256,
        "failure_code": status.failure_code,
        "consumer_commit_ref": status.consumer_commit_ref,
        "seller_ledger_snapshot_id": status.seller_ledger_snapshot_id,
        "seller_ledger_as_of": (
            _iso(status.seller_ledger_as_of) if status.seller_ledger_as_of else None
        ),
    }
    payload.update({key: value for key, value in optional.items() if value is not None})
    return payload
