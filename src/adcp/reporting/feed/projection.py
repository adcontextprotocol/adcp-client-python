"""Capture version 1 from committed inputs, with exact graph closure and no I/O."""

from __future__ import annotations

from dataclasses import replace
from typing import Any, NoReturn, cast
from uuid import uuid4

from pydantic import TypeAdapter

from adcp.reporting.canonical_json import canonical_json_utf8_v1
from adcp.reporting.feed.errors import ReportingFeedError
from adcp.reporting.feed.request import FeedRequest
from adcp.reporting.feed.snapshot import FeedKind, ReportingFeedRecord, ReportingFeedSnapshot
from adcp.reporting.ledger._delivery_state import (
    _verify_materialization,
    _verify_receipt,
    adjustment_sha256,
    current_receipt,
    decode_record,
    payload,
    principal,
    record_identity,
)
from adcp.reporting.ledger.delivery import (
    ReportingMaterializationView,
    materialization_to_wire,
    receipt_to_wire,
)
from adcp.reporting.ledger.delivery_changes import ReportingReconciliationChange
from adcp.reporting.ledger.delivery_models import (
    ReportingAdjustmentReceiptRecord,
    ReportingDeliveryPrincipal,
    ReportingDestinationBinding,
    ReportingMaterializationAttempt,
    ReportingMaterializationCheck,
    ReportingMaterializationRecord,
    ReportingObligationDeliveryRecord,
    ReportingRevisionReceiptRecord,
)
from adcp.reporting.ledger.notification_models import ReportingStatusScope
from adcp.reporting.ledger.status import (
    _adjustment_to_wire,
    _consumer_status_to_wire,
    _filters,
    _obligation_to_wire,
    _parse,
    _revision_to_wire,
    _scope_to_wire,
)
from adcp.reporting.ledger.status_projection import (
    ReportingStatusSnapshot,
    StatusProjectionInput,
    apply_intents_to_snapshot,
    lifecycle_intents,
    period_selected,
    project_status_scope,
)
from adcp.reporting.materializer.capture import ReportingMaterializerBoundary, private_snapshot
from adcp.reporting.receipts.capture import ReportingReceiptBoundary
from adcp.reporting.revision_selection import select_reporting_revision

_CORE = TypeAdapter(ReportingStatusSnapshot)
_PUBLIC = (
    ReportingMaterializationRecord,
    ReportingRevisionReceiptRecord,
    ReportingAdjustmentReceiptRecord,
)
Identity = tuple[FeedKind, str]


def _corrupt() -> NoReturn:
    raise ReportingFeedError("REPORTING_FEED_HISTORY_CORRUPT")


def capture_feed(
    core: ReportingStatusSnapshot,
    changes: tuple[ReportingReconciliationChange, ...],
    *,
    caller: ReportingDeliveryPrincipal,
    request: FeedRequest,
    after: tuple[int, int],
    consumer_status_enabled: bool,
    materializer_boundaries: tuple[ReportingMaterializerBoundary, ...] = (),
    receipt_boundaries: tuple[ReportingReceiptBoundary, ...] = (),
) -> ReportingFeedSnapshot:
    """Caller owns the account lock and connection until persistence completes.

    Domain rank, original domain sequence, kind, and wire ID form the total
    order. The two sequence spaces are never compared or collapsed with max().
    Closure records retain their original sort keys even below changes_after.
    """
    if core.account_id != caller.account_id:
        _corrupt()
    # Filter foreign statements before deriving maxima, projection or membership.
    # IDs must resolve unambiguously; scope metadata never supplies ownership.
    status_owners: dict[str, set[str]] = {}
    for status in core.statuses:
        status_owners.setdefault(status.reporting_status_id, set()).add(status.consumer_id)
    if any(caller.consumer_id in owners and len(owners) != 1 for owners in status_owners.values()):
        _corrupt()
    core = private_snapshot(core, caller)
    frozen_core = core
    # Derive pending lifecycle intents only in the captured value. Reads never
    # mutate the old issue tables, queues, captures, or notification boundaries.
    for _ in range(3):
        intents = lifecycle_intents(core)
        if not intents:
            break
        core = apply_intents_to_snapshot(core, intents)
    if lifecycle_intents(core):
        _corrupt()
    records = tuple(decode_record(payload(c.record)) for c in changes)
    if any(c.sequence != i or principal(c.record) != caller for i, c in enumerate(changes, 1)):
        _corrupt()
    if len({record_identity(r) for r in records}) != len(records):
        _corrupt()
    raw: dict[Identity, Any] = {}
    keys: dict[Identity, tuple[int, int, str, str]] = {}
    dependencies: dict[Identity, set[Identity]] = {}
    wires: dict[Identity, dict[str, Any]] = {}
    owners = {o.reporting_obligation_id: o for o in core.obligations}
    revisions = {r.reporting_revision_id: r for r in core.revisions}
    adjustments = {a.reporting_adjustment_id: a for a in core.adjustments}
    if any(
        len(mapping) != len(items)
        for mapping, items in (
            (owners, core.obligations),
            (revisions, core.revisions),
            (adjustments, core.adjustments),
        )
    ):
        _corrupt()
    # Captured mutations are immutable historical inputs, not permission to
    # infer today's owner from whichever artifact remains. Every retained
    # revision/owner and reconciliation payload must agree across boundaries.
    current_records = {record_identity(r): r for r in records}
    account_sequences: set[int] = set()
    captured_histories: tuple[
        tuple[ReportingMaterializerBoundary | ReportingReceiptBoundary, ...], ...
    ] = (materializer_boundaries, receipt_boundaries)
    for captured_history in captured_histories:
        for sequence, boundary in enumerate(captured_history, 1):
            if (
                boundary.caller != caller
                or boundary.sequence != sequence
                or boundary.account_sequence in account_sequences
            ):
                _corrupt()
            account_sequences.add(boundary.account_sequence)
            for historical_owner in boundary.core.obligations:
                if owners.get(historical_owner.reporting_obligation_id) != historical_owner:
                    _corrupt()
            for historical_revision in boundary.core.revisions:
                current = revisions.get(historical_revision.reporting_revision_id)
                if (
                    current is None
                    or replace(historical_revision, readable=current.readable) != current
                ):
                    _corrupt()
            for historical_record in boundary.reconciliation:
                if current_records.get(record_identity(historical_record)) != historical_record:
                    _corrupt()
    selections = {}
    for owning_obligation in core.obligations:
        history = tuple(
            r
            for r in core.revisions
            if r.reporting_obligation_id == owning_obligation.reporting_obligation_id
        )
        selection = select_reporting_revision(
            history,
            account_id=caller.account_id,
            reporting_obligation_id=owning_obligation.reporting_obligation_id,
            required_finality=owning_obligation.required_finality,
        )
        if selection.kind == "corrupt":
            _corrupt()
        selections[owning_obligation.reporting_obligation_id] = {
            "kind": selection.kind,
            "reporting_revision_id": (
                selection.revision.reporting_revision_id if selection.kind == "selected" else None
            ),
            "history": [r.reporting_revision_id for r in history],
        }
    for kind, items, field in (
        ("obligation", core.obligations, "reporting_obligation_id"),
        ("revision", core.revisions, "reporting_revision_id"),
        ("adjustment", core.adjustments, "reporting_adjustment_id"),
        ("consumer_status", core.statuses, "reporting_status_id"),
    ):
        for item in items:
            identity = (cast(FeedKind, kind), getattr(item, field))
            if identity in raw or item.account_id != caller.account_id:
                _corrupt()
            raw[identity] = item
    for seq, kind, record_id, _namespace in core.changes:
        if kind not in {"obligation", "revision", "adjustment", "consumer_status"}:
            continue
        identity = (cast(FeedKind, kind), record_id)
        if type(seq) is not int or seq < 1 or identity not in raw or identity in keys:
            _corrupt()
        keys[identity] = (0, seq, kind, record_id)
    if set(raw) != set(keys):
        _corrupt()
    for change in changes:
        record = change.record
        if isinstance(record, _PUBLIC):
            kind = cast(FeedKind, record.kind)
            record_id = (
                record.reporting_materialization_id
                if isinstance(record, ReportingMaterializationRecord)
                else record.reporting_receipt_id
            )
            identity = (kind, record_id)
            if identity in raw:
                _corrupt()
            raw[identity] = record
            keys[identity] = (1, change.sequence, kind, record_id)
    through = (
        max((k[1] for k in keys.values() if k[0] == 0), default=0),
        max((k[1] for k in keys.values() if k[0] == 1), default=0),
    )
    if any(a > t for a, t in zip(after, through)):
        _corrupt()
    filters = request.filters
    scoped = _filters(filters)
    projection = StatusProjectionInput(
        core,
        ReportingStatusScope(
            caller.account_id, consumer_id=caller.consumer_id if consumer_status_enabled else None
        ),
        delivery_config_ids=tuple(scoped["delivery_config_ids"] or ()),
        media_buy_ids=tuple(scoped["media_buy_ids"] or ()),
        feed_purposes=tuple(scoped["feed_purposes"] or ()),
        period_start=_parse(scoped["period_start"]),
        period_end=_parse(scoped["period_end"]),
    )
    scope_result = project_status_scope(projection)
    selected_owners = {p.obligation.reporting_obligation_id for p in scope_result.obligations}
    healths = set(filters["health"])
    finalities = set(filters["finality"])
    bindings = {r.generation_key: r for r in records if isinstance(r, ReportingDestinationBinding)}
    deliveries = {r.scope: r for r in records if isinstance(r, ReportingObligationDeliveryRecord)}
    attempts = {
        r.reporting_materialization_id: r
        for r in records
        if isinstance(r, ReportingMaterializationAttempt)
    }
    outcomes = {
        r.reporting_materialization_id: r
        for r in records
        if isinstance(r, ReportingMaterializationRecord)
    }
    selected: set[Identity] = set()
    readable: dict[str, bool] = {}
    owner_for: dict[Identity, str | None] = {}
    for identity, record in raw.items():
        kind, record_id = identity
        deps: set[Identity] = set()
        owner_id: str | None = None
        revision_id: str | None = None
        if kind == "obligation":
            owner_id = record_id
            # Retain the complete owning history, including an unlinked snapshot
            # plus official and all restatements. Buyer selectors require it.
            deps.update(
                ("revision", r.reporting_revision_id)
                for r in core.revisions
                if r.reporting_obligation_id == owner_id
            )
            result = project_status_scope(
                replace(
                    projection,
                    scope=ReportingStatusScope.for_obligation(record, projection.scope.consumer_id),
                    delivery_config_ids=(),
                    media_buy_ids=(),
                    feed_purposes=(),
                    period_start=None,
                    period_end=None,
                )
            )
            projected_obligation = result.obligations[0]
            wires[identity] = _obligation_to_wire(
                record,
                revisions=projected_obligation.revisions,
                health=result.health,
                production_status=projected_obligation.projection.production_status,
                issues=result.issues,
                statuses=projected_obligation.statuses,
            )
        elif kind == "revision":
            owner_id = record.reporting_obligation_id
            if owner_id not in owners:
                _corrupt()
            revision_id = record_id
            wires[identity] = _revision_to_wire(record, owners[owner_id])
            if record.supersedes_reporting_revision_id is not None:
                predecessor = revisions.get(record.supersedes_reporting_revision_id)
                if predecessor is None or predecessor.reporting_obligation_id != owner_id:
                    _corrupt()
                deps.add(("revision", predecessor.reporting_revision_id))
        elif kind == "adjustment":
            revision_id = record.adjusts_reporting_revision_id
            target = revisions.get(revision_id)
            if target is None or target.finality != "official":
                _corrupt()
            owner_id = target.reporting_obligation_id
            wires[identity] = _adjustment_to_wire(record)
        elif kind == "consumer_status":
            owner_id, revision_id = record.reporting_obligation_id, record.reporting_revision_id
            if owner_id is None and revision_id is not None:
                # Core permits a named revision without repeating its owner.
                # Resolve only that exact authenticated revision reference.
                target = revisions.get(revision_id)
                if target is None:
                    _corrupt()
                owner_id = target.reporting_obligation_id
            # A legacy obligation_missing statement may name no extant owner.
            # Keep its explicit identity; never attach a matching scope guess.
            if (
                owner_id not in owners
                and revision_id is None
                and record.consumer_status == "obligation_missing"
            ):
                owner_id = None
            wires[identity] = _consumer_status_to_wire(record)
            if record.supersedes_reporting_status_id is not None:
                deps.add(("consumer_status", record.supersedes_reporting_status_id))
        else:
            owner_id = record.scope.reporting_obligation_id
            revision_id = (
                record.adjusts_reporting_revision_id
                if isinstance(record, ReportingAdjustmentReceiptRecord)
                else record.reporting_revision_id
            )
            owner = owners.get(owner_id)
            if owner is None or owner.generation_key != record.scope.generation_key:
                _corrupt()
            if isinstance(record, ReportingMaterializationRecord):
                attempt, binding, delivery = (
                    attempts.get(record_id),
                    bindings.get(record.scope.generation_key),
                    deliveries.get(record.scope),
                )
                revision = revisions.get(revision_id)
                if (
                    attempt is None
                    or binding is None
                    or delivery is None
                    or revision is None
                    or attempt.scope != record.scope
                    or attempt.reporting_revision_id != revision_id
                ):
                    _corrupt()
                if record.status != "failed":
                    _verify_materialization(record, binding, delivery, revision, owner)
                view = ReportingMaterializationView(
                    attempt,
                    binding,
                    record,
                    tuple(
                        r
                        for r in records
                        if isinstance(r, ReportingMaterializationCheck)
                        and r.reporting_materialization_id == record_id
                    ),
                )
                wires[identity] = materialization_to_wire(view, obligation=owner)
                readable[record_id] = view.readable_at(core.as_of)
            else:
                current_receipt(records, record)
                if isinstance(record, ReportingRevisionReceiptRecord):
                    target = revisions.get(record.reporting_revision_id)
                    if target is None or record.reporting_materialization_id not in outcomes:
                        _corrupt()
                    # Admission is immutable. A later committed check can
                    # carry an earlier observation time; it changes current
                    # readability, never the evidence available at admission.
                    _verify_receipt(record, records[: keys[identity][1]], target)
                    deps.add(("materialization", record.reporting_materialization_id))
                else:
                    adjustment = adjustments.get(record.reporting_adjustment_id)
                    if (
                        adjustment is None
                        or adjustment.adjusts_reporting_revision_id != revision_id
                        or (
                            record.status == "accepted"
                            and record.observed_adjustment_sha256 != adjustment_sha256(adjustment)
                        )
                    ):
                        _corrupt()
                    deps.add(("adjustment", record.reporting_adjustment_id))
                if record.supersedes_reporting_receipt_id is not None:
                    deps.add((kind, record.supersedes_reporting_receipt_id))
                wires[identity] = receipt_to_wire(record)
        if revision_id is not None:
            revision = revisions.get(revision_id)
            if revision is None or revision.reporting_obligation_id != owner_id:
                _corrupt()
            deps.add(("revision", revision_id))
        if owner_id is not None:
            if owner_id not in owners:
                _corrupt()
            deps.add(("obligation", owner_id))
        owner_for[identity] = owner_id
        dependencies[identity] = deps - {identity}
    for identity, record in raw.items():
        owner_id = owner_for[identity]
        owner_wire = wires.get(("obligation", owner_id or ""))
        matches = owner_id in selected_owners
        if identity[0] == "consumer_status" and owner_id is None:
            matches = (
                (
                    not scoped["delivery_config_ids"]
                    or record.delivery_config_id in scoped["delivery_config_ids"]
                )
                and not scoped["media_buy_ids"]
                and not scoped["feed_purposes"]
                and period_selected(
                    record.period_start,
                    record.period_end,
                    projection.period_start,
                    projection.period_end,
                )
            )
        if healths and (owner_wire is None or owner_wire["health"] not in healths):
            matches = False
        if finalities:
            revision_id = (
                record.reporting_revision_id
                if identity[0] == "revision"
                else getattr(
                    record,
                    "reporting_revision_id",
                    getattr(record, "adjusts_reporting_revision_id", None),
                )
            )
            finality = (
                revisions[revision_id].finality
                if revision_id in revisions
                else owners[owner_id].required_finality if owner_id in owners else None
            )
            if finality not in finalities:
                matches = False
        key = keys[identity]
        if matches and key[1] > after[key[0]]:
            selected.add(identity)
    seeds = sorted(selected)
    pending = list(selected)
    while pending:
        identity = pending.pop()
        for dependency in dependencies[identity]:
            if dependency not in raw:
                _corrupt()
            if dependency not in selected:
                selected.add(dependency)
                pending.append(dependency)
    # Check every retained edge, including filtered-out records, before saving
    # the private dependency history needed for later representations.
    if any(d not in raw for deps in dependencies.values() for d in deps):
        _corrupt()
    inputs = {
        "version": 1,
        "projection_version": 1,
        "ownership_mode": "absent",
        "consumer_status_enabled": consumer_status_enabled,
        "core": _CORE.dump_python(frozen_core, mode="json"),
        "reconciliation": [payload(r) for r in records],
        "reconciliation_sequences": [c.sequence for c in changes],
        "materializer_boundaries": [b.to_storage() for b in materializer_boundaries],
        "receipt_boundaries": [b.to_storage() for b in receipt_boundaries],
        "revision_ownership": [
            {
                "reporting_revision_id": r.reporting_revision_id,
                "reporting_obligation_id": r.reporting_obligation_id,
            }
            for r in sorted(core.revisions, key=lambda r: r.reporting_revision_id)
        ],
        "selections": selections,
        "readable_materializations": readable,
        "seeds": [list(s) for s in seeds],
        "dependency_membership": [
            {"record": list(i), "dependencies": [list(d) for d in sorted(dependencies[i])]}
            for i in sorted(raw)
        ],
    }
    common = {
        "status": "completed",
        "view": "periods",
        "account_id": caller.account_id,
        "scope": _scope_to_wire(
            scope_result.configurations,
            ledger_as_of=core.as_of,
            request=filters,
            obligations=tuple(p.obligation for p in scope_result.obligations),
        ),
        "health": scope_result.health,
        "issues": [issue.to_wire() for issue in scope_result.issues],
    }
    return ReportingFeedSnapshot(
        caller,
        "rpfs_" + uuid4().hex,
        core.as_of,
        after,
        through,
        request.filters_json,
        canonical_json_utf8_v1(common),
        tuple(
            ReportingFeedRecord(i[0], i[1], keys[i], canonical_json_utf8_v1(wires[i]))
            for i in sorted(selected, key=keys.__getitem__)
        ),
        canonical_json_utf8_v1(inputs),
    )
