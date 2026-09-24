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
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Any, Literal

from adcp._version import (
    is_adcp_version_at_least,
    normalize_to_release_precision,
    resolve_adcp_version,
)
from adcp.reporting.canonical_json import canonical_json_utf8_v1
from adcp.reporting.ledger.consumer_status import condition_after_waiver
from adcp.reporting.ledger.delivery_models import ReportingDeliveryRecord
from adcp.reporting.ledger.models import (
    ConsumerStatusRecord,
    ReportingAdjustmentRecord,
    ReportingConfiguration,
    ReportingDeliveryEscalation,
    ReportingHealth,
    ReportingIssue,
    ReportingObligationRecord,
    ReportingRevisionRecord,
)
from adcp.reporting.ledger.notification_models import ReportingStatusScope
from adcp.reporting.ledger.schedule import next_reporting_expectation, next_reporting_period_start
from adcp.reporting.ledger.status_projection import (
    ReportingStatusSnapshot,
    StatusProjectionInput,
    apply_intents_to_snapshot,
    lifecycle_intents,
    mismatch_key,
    project_status_scope,
    status_retained_from,
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
    """Render one captured status snapshot using the same pure projection as push."""

    def __init__(
        self,
        store: ReportingLedgerStore,
        *,
        page_size: int = _DEFAULT_PAGE_SIZE,
        consumer_status_enabled: bool = False,
        escalation: ReportingDeliveryEscalation | None = None,
    ) -> None:
        self._store = store
        self._page_size = page_size
        self._consumer_status_enabled = consumer_status_enabled
        self._escalation = escalation

    async def handle(
        self, request: dict[str, Any], *, caller: ReportingStatusCaller
    ) -> dict[str, Any]:
        snapshot = await self._load(caller)
        return self.render_snapshot(request, caller=caller, snapshot=snapshot)

    async def _load(self, caller: ReportingStatusCaller) -> ReportingStatusSnapshot:
        from adcp.reporting.ledger.status_snapshot import ReportingStatusParticipant

        if isinstance(self._store, ReportingStatusParticipant):
            return await self._store.read_status_snapshot(account_id=caller.account_id)
        # Optional upgrade: custom stores keep their original structural API.
        # This fallback cannot establish durable status-notification readiness.
        boundary = await self._store.open_snapshot(
            account_id=caller.account_id, filters_fingerprint="status-projection-v1"
        )
        configurations = await self._store.list_configurations(account_id=caller.account_id)
        obligations: list[ReportingObligationRecord] = []
        revisions: list[ReportingRevisionRecord] = []
        adjustments: list[ReportingAdjustmentRecord] = []
        statuses: list[ConsumerStatusRecord] = []
        offset = 0
        changes: list[tuple[int, str, str, str]] = []
        while True:
            page = await self._store.read_page(
                snapshot=boundary,
                consumer_id=caller.consumer_id if self._consumer_status_enabled else None,
                delivery_config_ids=None,
                media_buy_ids=None,
                offset=offset,
                limit=self._page_size,
                changes_after_sequence=None,
            )
            obligations.extend(page.obligations)
            revisions.extend(page.revisions)
            adjustments.extend(page.adjustments)
            statuses.extend(page.consumer_statuses)
            if not page.has_more:
                break
            offset += self._page_size
        lifecycles = []
        for key in sorted({mismatch_key(s) for s in statuses}):
            seen: set[str] = set()
            while (
                issue := await self._store.get_issue(account_id=caller.account_id, issue_key=key)
            ) is not None:
                if issue.issue_id in seen or issue.issue_key != key:
                    raise LedgerConflictError(
                        "STATUS_PROJECTION_UNAVAILABLE", "invalid waiver chain"
                    )
                seen.add(issue.issue_id)
                lifecycles.append(issue)
                if issue.issue_state != "waived":
                    break
                key = condition_after_waiver(issue)
        for kind, records, attribute in (
            ("obligation", obligations, "reporting_obligation_id"),
            ("revision", revisions, "reporting_revision_id"),
            ("adjustment", adjustments, "reporting_adjustment_id"),
            ("consumer_status", statuses, "reporting_status_id"),
        ):
            # The legacy page API has a durable boundary but no per-record
            # ordinals. Replay its retained records when that boundary advances;
            # incremental_repair permits identity-deduplicated over-inclusion.
            # Positional ordinals would instead omit later records after rebuilds.
            changes.extend(
                (boundary.max_sequence, kind, getattr(record, attribute), "") for record in records
            )
        snapshot = ReportingStatusSnapshot(
            caller.account_id,
            boundary.ledger_as_of,
            tuple(configurations),
            tuple(obligations),
            tuple(revisions),
            tuple(statuses),
            tuple(lifecycles),
            adjustments=tuple(adjustments),
            changes=tuple(changes),
        )
        for _ in range(3):
            intents = lifecycle_intents(snapshot)
            if not intents:
                return snapshot
            for intent in intents:
                issue = intent.lifecycle
                if intent.action == "ensure_mismatch":
                    await self._store.ensure_issue_opened(
                        issue_key=issue.issue_key,
                        account_id=issue.account_id,
                        consumer_id=issue.consumer_id,
                        observed_at=issue.opened_at,
                    )
                elif intent.action == "retire_mismatch":
                    await self._store.retire_issue(
                        issue_key=issue.issue_key, account_id=issue.account_id, at=snapshot.as_of
                    )
            snapshot = apply_intents_to_snapshot(snapshot, intents)
        raise LedgerConflictError(
            "STATUS_PROJECTION_UNAVAILABLE", "status lifecycle did not converge"
        )

    def render_snapshot(
        self,
        request: dict[str, Any],
        *,
        caller: ReportingStatusCaller,
        snapshot: ReportingStatusSnapshot,
        reconciliation: tuple[ReportingDeliveryRecord, ...] | None = None,
        revision_ownership: bool = False,
    ) -> dict[str, Any]:
        """Render captured database evidence without any store calls or clock reads."""
        view = request.get("view", "summary")
        if view not in {"summary", "periods", "revision"}:
            raise LedgerConflictError("INVALID_VIEW", "unsupported reporting status view")
        if snapshot.account_id != caller.account_id:
            raise LedgerConflictError("LOOKUP_UNAVAILABLE", "status is unavailable to this caller")
        filters = _filters(request)
        version = normalize_to_release_precision(
            request.get("adcp_version") or resolve_adcp_version(None)
        )
        complete_start_forecast = is_adcp_version_at_least(version, "3.2-rc.6")
        if complete_start_forecast:
            # Bind the new wire contract without relabelling explicit rc.3 snapshots.
            filters["adcp_version"] = version
        scope = ReportingStatusScope(
            caller.account_id,
            consumer_id=(
                caller.consumer_id
                if self._consumer_status_enabled or reconciliation is not None
                else None
            ),
        )
        snapshot_id = (
            "rpls_"
            + _fingerprint(
                [
                    caller.account_id,
                    (
                        caller.consumer_id
                        if self._consumer_status_enabled or reconciliation is not None
                        else None
                    ),
                    filters,
                    snapshot.max_sequence,
                ]
            )[:32]
        )
        if reconciliation is not None:
            from adcp.reporting.ledger._delivery_state import fingerprint, principal

            reconciliation = tuple(
                r
                for r in reconciliation
                if principal(r).account_id == caller.account_id
                and principal(r).consumer_id == caller.consumer_id
            )
            snapshot_id = (
                "rpls_"
                + _fingerprint(
                    [snapshot_id, [fingerprint(r) for r in reconciliation], _iso(snapshot.as_of)]
                )[:32]
            )
        offset = 0
        lower = _checkpoint_sequence(request.get("changes_after")) or 0
        cursor = (request.get("pagination") or {}).get("cursor")
        if cursor:
            decoded = decode_cursor(cursor)
            if decoded.get("snapshot") != snapshot_id:
                raise LedgerConflictError(
                    "CURSOR_SNAPSHOT_MISMATCH",
                    "this cursor belongs to a different snapshot, caller or filter set;"
                    " restart the walk",
                )
            cursor_lower = decoded.get("lower")
            if not isinstance(cursor_lower, int):
                raise LedgerConflictError(
                    "CURSOR_SNAPSHOT_MISMATCH",
                    "this cursor does not retain its incremental lower bound; restart the walk",
                )
            if "changes_after" in request and lower != cursor_lower:
                raise LedgerConflictError(
                    "CURSOR_SNAPSHOT_MISMATCH",
                    "this cursor belongs to a different incremental lower bound; restart the walk",
                )
            lower = cursor_lower
            offset = int(decoded.get("offset", 0))
            if "as_of" in decoded:
                snapshot = replace(snapshot, as_of=datetime.fromisoformat(decoded["as_of"]))
        value = StatusProjectionInput(
            snapshot,
            scope,
            self._escalation,
            tuple(filters["delivery_config_ids"] or ()),
            tuple(filters["media_buy_ids"] or ()),
            tuple(filters["feed_purposes"] or ()),
            _parse(filters["period_start"]),
            _parse(filters["period_end"]),
            reconciliation=reconciliation,
            consumer_status_enabled=self._consumer_status_enabled,
        )
        result = project_status_scope(value)
        if result.intents:
            raise LedgerConflictError(
                "STATUS_PROJECTION_UNAVAILABLE", "status lifecycle is pending"
            )
        common: dict[str, Any] = {
            "status": "completed",
            "view": view,
            "ledger_snapshot_id": snapshot_id,
            "ledger_as_of": _iso(snapshot.as_of),
            "account_id": caller.account_id,
        }
        if view == "revision":
            revision_id = request.get("reporting_revision_id")
            if not revision_id:
                raise LedgerConflictError(
                    "MISSING_REVISION_ID", "a revision view requires reporting_revision_id"
                )
            revision = next(
                (r for r in snapshot.revisions if r.reporting_revision_id == revision_id), None
            )
            if revision is None:
                raise LedgerConflictError(
                    "LOOKUP_UNAVAILABLE", "no such revision is available to this caller"
                )
            owner = next(
                (
                    o
                    for o in snapshot.obligations
                    if o.reporting_obligation_id == revision.reporting_obligation_id
                ),
                None,
            )
            response = {
                **common,
                "revision": _revision_to_wire(revision, owner),
                "adjustments": [
                    _adjustment_to_wire(a)
                    for a in snapshot.adjustments
                    if a.adjusts_reporting_revision_id == revision_id
                ],
                "materializations": [],
                "receipts": [],
                "pagination": {"total_count": 1, "has_more": False},
            }
            if reconciliation is not None:
                from adcp.reporting.projection.wire import exact_revision_evidence

                response.update(
                    exact_revision_evidence(snapshot, revision, owner, reconciliation, caller)
                )
                if revision_ownership:
                    from adcp.reporting.ownership import with_revision_ownership

                    response = with_revision_ownership(
                        response, {revision.reporting_revision_id: revision.reporting_obligation_id}
                    )
            return response
        common["scope"] = _scope_to_wire(
            result.configurations,
            ledger_as_of=snapshot.as_of,
            request=request,
            obligations=tuple(p.obligation for p in result.obligations),
        )
        obligations = tuple(p.obligation for p in result.obligations)
        if view == "summary":
            states: list[str] = [p.projection.health for p in result.obligations]
            counts = {
                "total": len(obligations),
                **{
                    state: states.count(state)
                    for state in ("waiting", "healthy", "delayed", "action_required", "complete")
                },
            }
            if self._consumer_status_enabled:
                counts["consumer_status_pending"] = result.pending_count
            watermark = _scope_data_through(p.projection for p in result.obligations)
            configurations = tuple(
                c
                for c in result.configurations
                if (not request.get("finality") or c.required_finality in request["finality"])
                and (
                    not request.get("health")
                    or project_status_scope(
                        replace(
                            value,
                            scope=ReportingStatusScope(
                                c.account_id, c.generation_key, consumer_id=scope.consumer_id
                            ),
                        )
                    ).health
                    in request["health"]
                )
            )
            selected_obligations = tuple(
                p.obligation
                for p in result.obligations
                if (
                    not request.get("finality")
                    or p.obligation.required_finality in request["finality"]
                )
                and (not request.get("health") or p.projection.health in request["health"])
            )
            next_expected = (
                next_reporting_period_start(configurations, as_of=snapshot.as_of)
                if complete_start_forecast and result.health == "complete"
                else next_reporting_expectation(
                    configurations,
                    selected_obligations,
                    as_of=snapshot.as_of,
                    period_start=value.period_start,
                    period_end=value.period_end,
                )
            )
            return {
                **common,
                "health": result.health,
                "coverage": _coverage_roll_up(obligations, as_of=snapshot.as_of),
                "data_through": _iso(watermark) if watermark else None,
                **({"next_expected_at": _iso(next_expected)} if next_expected is not None else {}),
                "obligation_counts": counts,
                "issues": [i.to_wire() for i in result.issues],
            }
        owners = {o.reporting_obligation_id: o for o in obligations}
        records: dict[tuple[str, str], Any] = {
            ("obligation", o.reporting_obligation_id): o for o in obligations
        }
        for r in snapshot.revisions:
            if r.reporting_obligation_id in owners:
                records[("revision", r.reporting_revision_id)] = r
        for a in snapshot.adjustments:
            if ("revision", a.adjusts_reporting_revision_id) in records:
                records[("adjustment", a.reporting_adjustment_id)] = a
        generation_keys = {c.generation_key for c in result.configurations}
        if self._consumer_status_enabled:
            for s in snapshot.statuses:
                if s.consumer_id == caller.consumer_id and s.generation_key in generation_keys:
                    if (value.period_start is not None and s.period_end <= value.period_start) or (
                        value.period_end is not None and s.period_start >= value.period_end
                    ):
                        continue
                    records[("consumer_status", s.reporting_status_id)] = s
        selected = [
            (kind, records[(kind, record_id)])
            for seq, kind, record_id, _ in sorted(snapshot.changes)
            if seq > lower and (kind, record_id) in records
        ]
        window = selected[offset : offset + self._page_size]
        has_more = offset + self._page_size < len(selected)
        periods = []
        for kind, record in window:
            if kind != "obligation":
                continue
            scoped = project_status_scope(
                replace(value, scope=ReportingStatusScope.for_obligation(record, scope.consumer_id))
            )
            item = scoped.obligations[0]
            periods.append(
                _obligation_to_wire(
                    record,
                    revisions=item.revisions,
                    health=scoped.health,
                    production_status=item.projection.production_status,
                    issues=scoped.issues,
                    statuses=item.statuses,
                )
            )
        payload = {
            **common,
            "health": result.health,
            "issues": [issue.to_wire() for issue in result.issues],
            "changes_checkpoint": _encode_checkpoint(snapshot.max_sequence),
            "periods": periods,
            "revisions": [
                _revision_to_wire(r, owners.get(r.reporting_obligation_id))
                for kind, r in window
                if kind == "revision"
            ],
            "adjustments": [_adjustment_to_wire(a) for kind, a in window if kind == "adjustment"],
            "materializations": [],
            "receipts": [],
            "pagination": {
                "total_count": len(selected),
                "has_more": has_more,
                **(
                    {
                        "cursor": encode_cursor(
                            {
                                "snapshot": snapshot_id,
                                "offset": offset + self._page_size,
                                "lower": lower,
                                "as_of": snapshot.as_of.isoformat(),
                            }
                        )
                    }
                    if has_more
                    else {}
                ),
            },
        }
        if self._consumer_status_enabled:
            payload["consumer_statuses"] = [
                _consumer_status_to_wire(s) for kind, s in window if kind == "consumer_status"
            ]
        return payload


def _consumer_status_pending(
    obligation: ReportingObligationRecord,
    statuses: Sequence[ConsumerStatusRecord],
    *,
    ledger_as_of: datetime,
) -> bool:
    """Whether this caller owes a status it has not filed.

    The deadline is ``expected_at + automated_recovery_window_seconds``, which
    the obligation already carries as ``automated_recovery_deadline_at``. A
    chain with *any* unsuperseded leaf counts as current whatever that leaf
    says; only an empty chain is pending.

    Visibility only. This never changes health, another count, or
    seller-advertised reliability statistics -- a buyer that has not integrated
    the loop is not evidence about the seller.
    """
    if _utc(ledger_as_of) < _utc(obligation.automated_recovery_deadline_at):
        return False
    return not any(not status.superseded for status in statuses)


def _degraded(projection: Any, severity: str = "action_required") -> Any:
    from dataclasses import replace

    return replace(projection, health=severity)


def _filters(request: dict[str, Any]) -> dict[str, Any]:
    period = request.get("period") or {}
    start, end = _parse(period.get("start")), _parse(period.get("end"))
    if start is not None and end is not None and end <= start:
        raise LedgerConflictError("INVALID_PERIOD", "the status horizon must be nonempty")
    return {
        "delivery_config_ids": sorted(set(request.get("delivery_config_ids") or [])) or None,
        "media_buy_ids": sorted(set(request.get("media_buy_ids") or [])) or None,
        "feed_purposes": sorted(set(request.get("feed_purposes") or [])) or None,
        "period_start": start.isoformat() if start else None,
        "period_end": end.isoformat() if end else None,
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


def _scope_to_wire(
    configurations: Sequence[ReportingConfiguration],
    *,
    ledger_as_of: datetime,
    request: dict[str, Any],
    obligations: Sequence[ReportingObligationRecord] = (),
) -> dict[str, Any]:
    """The denominator this read's health was computed over.

    Stated explicitly rather than implied, because health means nothing without
    it: "healthy" over a scope that silently excluded half the periods is worse
    than no answer.  An empty denominator is a valid, vacuously complete closed
    scope -- not an error and not an inaccessible-identifier signal.

    Derived from the caller's **configuration generations**, not from whichever
    obligations happened to land on a page.  Configurations *are* the
    denominator, and deriving from them is the only way every page of one
    cursor reports the identical scope -- a scope that shifted mid-walk would
    make the consumer's record count meaningless.
    """
    requested = request.get("period") or {}
    retained_from = status_retained_from(configurations, ledger_as_of)
    horizon_start = _parse(requested.get("start")) or retained_from
    horizon_end = _parse(requested.get("end")) or _utc(ledger_as_of)
    generations = {item.generation_key: item for item in configurations}
    return {
        "period_start": _iso(horizon_start),
        "period_end": _iso(horizon_end),
        # No further obligation can enter a horizon that has already elapsed.
        "scope_closed": horizon_end <= _utc(ledger_as_of)
        and all(o.period.end <= ledger_as_of for o in obligations),
        # True when the caller named no media buys, so the scope is every buy
        # it can reach rather than an enumerated subset.
        "all_accessible_media_buys": not request.get("media_buy_ids"),
        **(
            {"media_buy_ids": sorted(set(request["media_buy_ids"]))}
            if request.get("media_buy_ids")
            else {}
        ),
        "delivery_config_generations": [
            {
                "delivery_config_id": generation.delivery_config_id,
                "delivery_config_version": generation.delivery_config_version,
                "feed_purpose": generation.feed_purpose,
            }
            for generation in sorted(
                generations.values(),
                key=lambda item: (
                    item.account_id,
                    item.delivery_config_id,
                    item.delivery_config_version,
                ),
            )
        ],
        "feed_purposes": sorted({item.feed_purpose for item in configurations}),
        "finality": sorted({item.required_finality for item in configurations}),
        "ledger_retained_from": _iso(retained_from),
        # False means health cannot prove completeness for the whole requested
        # horizon, because part of it predates what this ledger still retains.
        "coverage_complete": not configurations or horizon_start >= retained_from,
    }


def _parse(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        result = (
            value
            if isinstance(value, datetime)
            else datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        )
        if result.tzinfo is not None and result.utcoffset() is not None:
            return _utc(result)
    except (TypeError, ValueError):
        # Raise the closed timestamp error below, after parser context has cleared.
        pass
    raise LedgerConflictError("INVALID_PERIOD", "status timestamps must include a timezone")


def _period_to_wire(obligation: ReportingObligationRecord) -> dict[str, Any]:
    """A period is only unambiguous with its source calendar.

    "Local midnight to local midnight" is 25 hours in New York on the day DST
    ends and 24 in UTC, so a period without its timezone is two different
    windows depending on who reads it.
    """
    return {
        "start": _iso(obligation.period.start),
        "end": _iso(obligation.period.end),
        "source_timezone": obligation.period.source_timezone,
    }


def _obligation_coverage(obligation: ReportingObligationRecord) -> dict[str, Any]:
    """One obligation's frozen coverage, evaluated at the scope-resolution boundary.

    ``evaluated_at`` is ``scope_resolved_at``, not "now": the denominator froze
    at the period boundary, and a coverage claim carrying a later evaluation
    instant is describing a scope that did not exist when the period closed.
    """
    media_buy_ids = list(obligation.media_buy_ids)
    package_ids = list(obligation.package_ids)
    full = obligation.coverage_status == "full"
    return {
        "status": obligation.coverage_status,
        "evaluated_at": _iso(obligation.scope_resolved_at),
        "media_buy_ids": media_buy_ids,
        "fully_covered_media_buy_ids": media_buy_ids if full else [],
        "partially_covered_media_buy_ids": [],
        "unsupported_media_buy_ids": [],
        "unknown_media_buy_ids": [] if full else media_buy_ids,
        "package_ids": package_ids,
        "covered_package_ids": package_ids if full else [],
        "unsupported_package_ids": [],
        "unknown_package_ids": [] if full else package_ids,
        "limitations": [],
    }


def _coverage_roll_up(
    obligations: Sequence[ReportingObligationRecord], *, as_of: datetime
) -> dict[str, Any]:
    media_buy_ids = sorted(
        {item for obligation in obligations for item in obligation.media_buy_ids}
    )
    package_ids = sorted({item for obligation in obligations for item in obligation.package_ids})
    statuses = {obligation.coverage_status for obligation in obligations}
    status = (
        "full"
        if statuses <= {"full"}
        else ("partial" if statuses.intersection({"full", "partial"}) else "none")
    )
    if "unknown" in statuses:
        status = "unknown"
    evaluated = (
        max(obligation.scope_resolved_at for obligation in obligations) if obligations else as_of
    )
    return {
        "status": status,
        "evaluated_at": _iso(evaluated),
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
        "period": _period_to_wire(obligation),
        "expected_at": _iso(obligation.period.expected_at),
        "schedule": {
            "period_duration": obligation.schedule.period_duration,
            "alignment": obligation.schedule.alignment,
            "delivery_sla": obligation.schedule.delivery_sla,
        },
        "coverage": _obligation_coverage(obligation),
        "required_finality": obligation.required_finality,
        # Core is delivery_only by definition: no destination, no receipt.
        # managed_delivery and reconciled_billing are separately advertised.
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


def _revision_to_wire(
    revision: ReportingRevisionRecord, obligation: ReportingObligationRecord | None = None
) -> dict[str, Any]:
    """Project a retained revision onto the wire.

    A wire revision is self-describing: it names the exact report definition
    and row schema it was produced under, by URI and digest, plus the scope and
    period it covers.  Those come from its obligation, so a caller that omits
    ``obligation`` (or a configuration with no
    :class:`~adcp.reporting.ledger.models.ReportingDefinitionBinding`) gets a
    record that will not validate against ``reporting-revision.json`` -- which
    is the honest outcome, because the seller has not supplied what Core
    requires.
    """
    payload: dict[str, Any] = {
        "reporting_revision_id": revision.reporting_revision_id,
        "revision_content_sha256": revision.revision_content_sha256,
        "account_id": revision.account_id,
        "finality": revision.finality,
        "observed_at": _iso(revision.observed_at),
        "data_through": _iso(revision.data_through) if revision.data_through else None,
        "data_through_precision": "exact" if revision.data_through else "unknown",
        "row_count": revision.row_count,
        "control_totals": [
            _legacy_total_to_wire(name, value, obligation)
            for name, value in revision.control_totals
        ],
        "created_at": _iso(revision.created_at),
    }
    if revision.managed_control_totals is not None:
        payload["control_totals"] = [item.to_wire() for item in revision.managed_control_totals]
    if obligation is not None:
        payload.update(
            report_definition_id=obligation.report_definition_id,
            reporting_profile=obligation.reporting_profile,
            media_buy_ids=list(obligation.media_buy_ids),
            coverage=_obligation_coverage(obligation),
            period=_period_to_wire(obligation),
        )
        if obligation.definition is not None:
            payload.update(obligation.definition.to_wire())
    optional = {
        "supersedes_reporting_revision_id": revision.supersedes_reporting_revision_id,
        "finality_basis": revision.finality_basis,
        "finality_policy_id": revision.finality_policy_id,
        "finalized_at": _iso(revision.finalized_at) if revision.finalized_at else None,
    }
    payload.update({key: value for key, value in optional.items() if value is not None})
    return payload


def _legacy_total_to_wire(
    name: str, value: str, obligation: ReportingObligationRecord | None = None
) -> dict[str, str]:
    """Project retained Core totals without altering their immutable evidence.

    Legacy Core stores canonical numeric strings. Preserve that representation;
    monetary units, where known, come only from the frozen obligation.
    """
    units = {}
    if obligation is not None:
        if obligation.currency is not None:
            units["spend"] = obligation.currency
        if obligation.definition is not None:
            units.update(obligation.definition.monetary_control_total_units)
    return {
        "name": name,
        "value": value,
        "value_type": "decimal" if "." in value or name in units else "integer",
        **({"unit": units[name]} if name in units else {}),
    }


def _adjustment_to_wire(adjustment: ReportingAdjustmentRecord) -> dict[str, Any]:
    return {
        "reporting_adjustment_id": adjustment.reporting_adjustment_id,
        "adjusts_reporting_revision_id": adjustment.adjusts_reporting_revision_id,
        "reason_code": adjustment.reason_code,
        "accounting_period": {
            "start": _iso(adjustment.accounting_period_start),
            "end": _iso(adjustment.accounting_period_end),
        },
        "control_total_deltas": (
            [_legacy_total_to_wire(name, value) for name, value in adjustment.control_total_deltas]
            if adjustment.managed_control_total_deltas is None
            else [total.to_wire() for total in adjustment.managed_control_total_deltas]
        ),
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
        "mismatch_code": status.mismatch_code,
        "consumer_commit_ref": status.consumer_commit_ref,
        "seller_ledger_snapshot_id": status.seller_ledger_snapshot_id,
        "seller_ledger_as_of": (
            _iso(status.seller_ledger_as_of) if status.seller_ledger_as_of else None
        ),
    }
    payload.update({key: value for key, value in optional.items() if value is not None})
    return payload
