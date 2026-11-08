"""Opt-in collector for orphaned row objects, fenced against a cloned or restored database.

Shared SDK persistence spec, adcontextprotocol/adcp#7996 section 4.2; mirrors the JavaScript SDK's
``collectOrphanedRowObjects`` (adcp-client#3291) and its ownership fence (adcp-client#3593).

The write-intent sweep (:mod:`adcp_reporting_storage.ledger.row_retention`) deletes only what an
intent lists. It cannot see an object whose ``PUT`` succeeded before the intent recorded it, an
intent for a binding no running host configures, or an intent lost to a database restore. This
collector lists each configured object binding under its owned prefix,
``{prefix}/{namespace_key}/``, and deletes an object only when all of these hold:

* its key is a well-formed row object (``.jsonl`` or ``.jsonl.gz``) under that prefix;
* it was written more than ``min_age_seconds`` ago (default 7 days, at least 1 day), so no writer
  that still holds a lease can be mid-upload;
* no row set that is not already ``pruned`` has a chunk at its key, and no write intent lists it,
  at any version and under any binding (a pre-check that errs towards leaking, because two bindings
  can share a bucket and prefix);
* under the per-object advisory lock of **every** binding (taken only here and by a commit that
  adopts an object its own intent did not record), no ``live`` or ``unavailable`` row set
  under any binding references that exact version and no open intent created it (the check every
  other delete makes, widened to every binding), and the provider then deletes that exact version.

A clone or point-in-time restore of the ledger database shares the live database's installation
identity, namespace key and configuration, so it would also share the opt-in. A real pass therefore
first claims the namespace in an ownership fence
(:mod:`adcp_reporting_storage.ledger.orphan_fence`) and refuses to delete while another database
holds a newer claim. See ``docs/reporting-row-storage.md``.
"""

from __future__ import annotations

import asyncio
import math
import time
import uuid
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any, Literal

from adcp_reporting_storage.ledger.orphan_fence import (
    OrphanFenceDecision,
    OrphanFenceEntry,
    OrphanFenceRecord,
    OrphanFenceRow,
    ReportingOrphanFenceRefusalV1,
    decide_orphan_fence,
)
from adcp_reporting_storage.ledger.row_storage import ReportingRowStoreError
from adcp_reporting_storage.ledger.row_storage_object import (
    format_row_instant,
    parse_row_instant,
)

if TYPE_CHECKING:
    from adcp_reporting_storage.ledger.pg import PgReportingLedgerStore

__all__ = [
    "ReportingOrphanBindingResultV1",
    "ReportingOrphanCandidateV1",
    "ReportingOrphanCollectionOptionsV1",
    "ReportingOrphanCollectionResultV1",
    "ReportingOrphanCollectionStateV1",
    "ReportingOrphanFenceTakeoverV1",
    "collect_orphaned_row_objects",
    "run_orphan_collection_pass",
    "take_over_orphan_collector_fence",
]

DEFAULT_ORPHAN_MIN_AGE_SECONDS = 7 * 24 * 3600
MIN_ORPHAN_MIN_AGE_SECONDS = 24 * 3600
DEFAULT_ORPHAN_INTERVAL_SECONDS = 86_400
#: A fenced namespace looks again within the hour, so applying the migration or taking the
#: namespace over does not leave collection idle for the rest of a day.
FENCED_RECHECK_SECONDS = 3_600
MAX_ORPHAN_CANDIDATES = 1_000
#: Deletes between reads of the collector fence within one listing page.
ORPHAN_FENCE_RECHECK_DELETES = 25
_FENCE_LOCK_TIMEOUT = "120s"
#: The suffixes ``render_reporting_row_object_key_v1`` gives every key: nothing else under the
#: prefix is a row object.
_ROW_OBJECT_SUFFIXES = (".jsonl", ".jsonl.gz")

_BindingStatus = Literal["complete", "partial", "failed", "unsupported", "read_only", "fenced"]


@dataclass(frozen=True, slots=True)
class ReportingOrphanCandidateV1:
    """An object a dry run would delete."""

    binding_id: str
    object_ref: str
    native_version_ref: str
    size_bytes: int
    created_at: str


@dataclass(slots=True)
class ReportingOrphanBindingResultV1:
    """What one binding's listing did.

    ``status``: ``complete`` (the listing reached its end), ``partial`` (stopped at ``limit``;
    resume from ``cursors``), ``failed`` (the listing or its provider failed; ``error`` is the
    stable code), ``unsupported`` (the provider has no ``list``), ``read_only`` (skipped because
    the binding is ``read_only``) or ``fenced`` (another database holds the namespace's collector
    fence, or this database cannot prove it holds it, so no further object was deleted; ``deleted``
    counts any that went before a mid-pass refusal and ``fence`` says why).
    """

    binding_id: str
    status: _BindingStatus = "complete"
    #: Objects the provider listed, including ones dropped as foreign.
    listed: int = 0
    #: Old enough, but named by a ledger record or open upload; left alone.
    referenced: int = 0
    #: Younger than ``min_age_seconds``, or with no readable write time.
    too_young: int = 0
    #: Outside the owned prefix or not shaped like a row object; never touched.
    foreign: int = 0
    deleted: int = 0
    #: Dry run only: orphans that a real pass would delete.
    would_delete: int = 0
    failed: int = 0
    error: str | None = None
    fence: ReportingOrphanFenceRefusalV1 | None = None


@dataclass(slots=True)
class ReportingOrphanCollectionResultV1:
    """A collection pass. ``deleted`` counts delete requests that found nothing referenced; an
    object another host removed first still counts."""

    dry_run: bool
    listed: int = 0
    referenced: int = 0
    too_young: int = 0
    foreign: int = 0
    deleted: int = 0
    would_delete: int = 0
    #: Delete failures plus bindings whose listing failed.
    failed: int = 0
    #: Bindings whose provider cannot list: not covered by this collector.
    unsupported: tuple[str, ...] = ()
    #: ``read_only`` bindings skipped.
    skipped_read_only: tuple[str, ...] = ()
    #: Bindings whose collector fence another database holds: no further object was deleted there.
    #: See each binding's ``fence``.
    fenced: tuple[str, ...] = ()
    #: Dry run only: the orphans a real pass would delete, at most 1000.
    candidates: tuple[ReportingOrphanCandidateV1, ...] = ()
    #: Resume tokens for bindings whose listing is not finished. Pass back as ``cursors``.
    cursors: Mapping[str, str] = field(default_factory=dict)
    #: Bindings whose listing reached its end this pass. Pass them back as ``skip_bindings`` until
    #: every binding has.
    completed: tuple[str, ...] = ()
    bindings: tuple[ReportingOrphanBindingResultV1, ...] = ()


@dataclass(frozen=True, slots=True)
class ReportingOrphanFenceTakeoverV1:
    """One fence a takeover claimed."""

    #: The bindings that share the fence.
    binding_ids: tuple[str, ...]
    #: The epoch now held.
    epoch: int
    #: Highest epoch found in the object store before the takeover; ``None`` when the namespace
    #: was never claimed.
    previous_epoch: int | None


@dataclass(frozen=True, slots=True)
class ReportingOrphanCollectionOptionsV1:
    """Opt-in scheduling of :func:`collect_orphaned_row_objects` (``PgReportingLedgerStore(
    orphan_collection=...)``).

    Enable it only on the ledger database that owns the namespace. A clone or restore of that
    database pointed at the same bucket shares its namespace, so a real pass is fenced: it claims
    the namespace and refuses, reporting the binding ``fenced``, when the object store holds a newer
    claim than this database recorded. After a restore, or when the original is gone, call
    ``take_over_orphan_collector_fence``. Start with ``dry_run=True`` and read the candidates.
    """

    #: Report what would be deleted and delete nothing.
    dry_run: bool = False
    #: Required unless ``dry_run``: your statement that this ledger database is the only one that
    #: owns the namespace, and not a clone or restore pointed at the same bucket. A copy of the
    #: configuration carries this flag too; the ownership fence is what stops such a copy.
    acknowledge_exclusive_namespace: bool = False
    #: Minimum time between full listings, counted from when the previous one finished. Passes in
    #: between do nothing; a listing that has not finished (it stops at ``limit``) continues on
    #: every pass.
    interval_seconds: int = DEFAULT_ORPHAN_INTERVAL_SECONDS
    #: Objects younger than this are never deleted. At least 1 day; keep it well above the longest
    #: write deadline plus intent expiry.
    min_age_seconds: int = DEFAULT_ORPHAN_MIN_AGE_SECONDS
    #: Objects examined per binding per pass.
    limit: int = 1_000
    #: Also collect from ``read_only`` bindings, which are skipped by default.
    include_read_only: bool = False

    def __post_init__(self) -> None:
        for flag in ("dry_run", "acknowledge_exclusive_namespace", "include_read_only"):
            if type(getattr(self, flag)) is not bool:
                raise ValueError(f"orphan_collection.{flag} must be a boolean")
        if not self.dry_run and not self.acknowledge_exclusive_namespace:
            raise ValueError(
                "orphan_collection deletes objects: set acknowledge_exclusive_namespace=True to "
                "confirm this ledger database is the only one that owns the namespace (not a "
                "clone or restore pointed at the same bucket), or set dry_run=True"
            )
        for name in ("interval_seconds", "limit"):
            value = getattr(self, name)
            if type(value) is not int or value < 1:
                raise ValueError(f"orphan_collection.{name} must be a positive integer")
        if (
            type(self.min_age_seconds) is not int
            or self.min_age_seconds < MIN_ORPHAN_MIN_AGE_SECONDS
        ):
            raise ValueError(
                "orphan_collection.min_age_seconds must be an integer of at least 86400 (1 day): "
                "a younger object may belong to an upload still in flight"
            )


@dataclass(slots=True)
class ReportingOrphanCollectionStateV1:
    """Progress of the collector between passes, kept in the process.

    It is not persisted, so a restart begins a new listing.
    """

    cursors: dict[str, str] = field(default_factory=dict)
    #: Bindings whose listing finished in the current cycle; not listed again until every binding
    #: has.
    completed: list[str] = field(default_factory=list)
    #: ``time.monotonic()`` seconds before which no new full listing starts.
    next_cycle_at: float | None = None


@dataclass(frozen=True, slots=True)
class _Gate:
    refusal: ReportingOrphanFenceRefusalV1 | None = None

    @property
    def ok(self) -> bool:
        return self.refusal is None


# -- the collector ---------------------------------------------------------------------------


def _positive(value: int, name: str, maximum: int = 1_000_000) -> None:
    if type(value) is not int or not 1 <= value <= maximum:
        raise ValueError(f"{name} must be between 1 and {maximum}")


def _names(value: Sequence[str] | None, name: str) -> tuple[str, ...] | None:
    """Binding IDs as a tuple. A bare string would otherwise act as a substring test."""
    if value is None:
        return None
    if isinstance(value, (str, bytes)) or not all(isinstance(item, str) for item in value):
        raise ValueError(f"{name} must be a sequence of binding IDs")
    return tuple(value)


def _code(error: Exception) -> str:
    return error.code if isinstance(error, ReportingRowStoreError) else "INTERNAL_ERROR"


async def _present_tables(store: PgReportingLedgerStore) -> set[str]:
    async with store._pool.connection() as connection:
        rows = await (
            await connection.execute(
                "SELECT table_name FROM information_schema.tables"
                " WHERE table_schema = current_schema() AND table_name = ANY(%s::text[])",
                (
                    [
                        "adcp_reporting_row_sets",
                        "adcp_reporting_row_write_intents",
                        "adcp_reporting_orphan_collector_fences",
                    ],
                ),
            )
        ).fetchall()
    return {row[0] for row in rows}


async def _keys_named_by_records(store: PgReportingLedgerStore, keys: Sequence[str]) -> set[str]:
    """Of these object keys, the ones a ledger record names at any version, under any binding.

    A chunk of a row set that is not yet ``pruned``, or an object any write intent lists. Version-
    and binding-blind on purpose: two bindings can share a bucket and prefix (a retired binding kept
    ``read_only`` next to its successor), and the orphan collector would rather leak than delete
    near a record.
    """
    if not keys:
        return set()
    async with store._pool.connection() as connection:
        rows = await (
            await connection.execute(
                "SELECT chunk.object_ref FROM adcp_reporting_row_chunks chunk"
                " JOIN adcp_reporting_row_sets row_set ON row_set.row_set_id = chunk.row_set_id"
                " WHERE chunk.object_ref = ANY(%(keys)s::text[]) AND row_set.rows_state <> 'pruned'"
                " UNION"
                " SELECT COALESCE(item ->> 'object_ref', item ->> 'objectKey')"
                " FROM adcp_reporting_row_write_intents intent,"
                " jsonb_array_elements(intent.created_objects) item"
                " WHERE COALESCE(item ->> 'object_ref', item ->> 'objectKey')"
                " = ANY(%(keys)s::text[])",
                {"keys": list(keys)},
            )
        ).fetchall()
    return {row[0] for row in rows}


async def collect_orphaned_row_objects(
    store: PgReportingLedgerStore,
    *,
    min_age_seconds: int | None = None,
    limit: int = 1_000,
    page_size: int | None = None,
    dry_run: bool = False,
    include_read_only: bool = False,
    bindings: Sequence[str] | None = None,
    cursors: Mapping[str, str] | None = None,
    skip_bindings: Sequence[str] | None = None,
    acknowledge_exclusive_namespace: bool = False,
    now: datetime | None = None,
) -> ReportingOrphanCollectionResultV1:
    """Delete row objects that no ledger record or open upload names.

    Opt-in, listing-based and read-only unless it deletes. See the module docstring for the full
    list of conditions an object must meet. ``read_only`` bindings are skipped unless
    ``include_read_only``. A binding whose provider has no ``list`` is reported in
    ``unsupported``. ``dry_run`` deletes nothing and reports what would go in ``candidates``.
    ``limit`` bounds the objects examined per binding per pass; pass ``result.cursors`` back as
    ``cursors`` to resume where the last pass stopped (a binding absent from ``cursors`` starts from
    the beginning, which is also where a binding that finished its listing starts next time), and
    ``result.completed`` as ``skip_bindings`` so a cycle that spans several passes does not rescan
    bindings that already finished.

    Deleting needs ``acknowledge_exclusive_namespace=True``: enable it only on the ledger database
    that owns the namespace. A clone or restore of that database that still points at the same
    bucket shares its ``namespace_key``, and would delete the objects of every row set the original
    wrote after the clone was taken. That flag is copied with the configuration, so it is backed by
    a fence: a real pass first claims the namespace (a create-only object under
    ``{prefix}/{namespace_key}/_collector-fence/`` and a row in
    ``adcp_reporting_orphan_collector_fences``) and deletes only while the object store's highest
    claim is exactly the one this database recorded. A database copied before the original's last
    pass sees a newer claim and refuses: its binding is reported ``fenced`` with a ``cause``, and
    nothing more is deleted (a fence that advances mid-pass stops the pass at the next check, within
    25 deletes, and ``deleted`` keeps what went before). A ``dry_run`` checks the fence without
    claiming it and reports fenced bindings the same way. After a restore, or when the original is
    gone, take the namespace over deliberately with ``take_over_orphan_collector_fence``. Everything
    the listing finds under the owned prefix with a row-object key is treated as written by this
    SDK, so do not store other ``.jsonl`` files there.
    """
    storage = store._row_storage
    if storage is None:
        raise ReportingRowStoreError(
            "INVALID_INPUT", "collect_orphaned_row_objects requires row storage"
        )
    bindings = _names(bindings, "bindings")
    skip_bindings = _names(skip_bindings, "skip_bindings")
    if min_age_seconds is None:
        min_age_seconds = DEFAULT_ORPHAN_MIN_AGE_SECONDS
    if type(min_age_seconds) is not int or min_age_seconds < MIN_ORPHAN_MIN_AGE_SECONDS:
        raise ValueError(
            f"min_age_seconds must be an integer of at least {MIN_ORPHAN_MIN_AGE_SECONDS} "
            "(1 day): an object younger than the longest write deadline plus intent expiry may "
            "belong to an upload still in flight"
        )
    _positive(limit, "limit")
    page_size = min(limit, 500) if page_size is None else page_size
    _positive(page_size, "page_size")
    if dry_run is not True:
        dry_run = False
    if not dry_run and acknowledge_exclusive_namespace is not True:
        raise ValueError(
            "collect_orphaned_row_objects deletes objects: pass "
            "acknowledge_exclusive_namespace=True to confirm this ledger database is the only one "
            "that owns the namespace (not a clone or restore pointed at the same bucket), or pass "
            "dry_run=True"
        )
    clock = now if now is not None else datetime.now(timezone.utc)
    if not isinstance(clock, datetime) or clock.tzinfo is None:
        raise ValueError("now must be a timezone-aware datetime")
    await store.ready_row_storage()
    summaries = storage.object_binding_summaries()
    for binding_id in bindings or ():
        if not any(summary.id == binding_id for summary in summaries):
            raise ValueError(
                f"bindings names {binding_id}, which is not a configured object binding"
            )

    result = ReportingOrphanCollectionResultV1(dry_run=dry_run)
    outcomes: list[ReportingOrphanBindingResultV1] = []
    candidates: list[ReportingOrphanCandidateV1] = []
    completed: list[str] = []
    unsupported: list[str] = []
    skipped_read_only: list[str] = []
    fenced: list[str] = []
    next_cursors: dict[str, str] = {}

    def finish() -> ReportingOrphanCollectionResultV1:
        for outcome in outcomes:
            result.listed += outcome.listed
            result.referenced += outcome.referenced
            result.too_young += outcome.too_young
            result.foreign += outcome.foreign
            result.deleted += outcome.deleted
            result.would_delete += outcome.would_delete
            result.failed += outcome.failed
        result.unsupported = tuple(unsupported)
        result.skipped_read_only = tuple(skipped_read_only)
        result.fenced = tuple(fenced)
        result.candidates = tuple(candidates)
        result.cursors = next_cursors
        result.completed = tuple(completed)
        result.bindings = tuple(outcomes)
        return result

    tables = await _present_tables(store)
    if "adcp_reporting_row_sets" not in tables or "adcp_reporting_row_write_intents" not in tables:
        return finish()
    has_fence_table = "adcp_reporting_orphan_collector_fences" in tables
    # One fence check or claim per physical fence per pass: bindings that share a bucket and prefix
    # share it.
    gates: dict[str, _Gate] = {}

    async def gate(binding_id: str, mode: Literal["claim", "check"]) -> _Gate:
        scope, _, _ = await storage.collector_fence_location(binding_id)
        if mode == "claim" and scope in gates:
            return gates[scope]
        outcome = await _orphan_fence_gate(
            store,
            binding_id,
            scope,
            "check" if mode == "claim" and dry_run else mode,
            has_fence_table,
        )
        if mode == "claim":
            gates[scope] = outcome
        return outcome

    cutoff = clock - timedelta(seconds=min_age_seconds)
    for summary in summaries:
        if bindings is not None and summary.id not in bindings:
            continue
        if skip_bindings is not None and summary.id in skip_bindings:
            continue
        outcome = ReportingOrphanBindingResultV1(summary.id)
        outcomes.append(outcome)
        if summary.read_only and not include_read_only:
            outcome.status = "read_only"
            skipped_read_only.append(summary.id)
            continue
        if not summary.listable:
            outcome.status = "unsupported"
            unsupported.append(summary.id)
            continue
        token = cursors.get(summary.id) if cursors is not None else None
        # The ownership fence comes before any listing: a clone or restore must not even enumerate
        # for deletion.
        try:
            claim = await gate(summary.id, "claim")
        except Exception as error:
            outcome.status = "failed"
            outcome.error = _code(error)
            outcome.failed += 1
            # The listing never started, so the position it had stays where it was.
            if token is not None:
                next_cursors[summary.id] = token
            continue
        if not claim.ok:
            outcome.status = "fenced"
            outcome.fence = claim.refusal
            fenced.append(summary.id)
            continue
        # A failure on the first page of a resumed listing drops the cursor: a stale or invalid
        # token must not wedge the binding, and restarting from the beginning is always safe.
        resumed = token is not None
        drop_cursor = False
        examined = 0
        pages = 0
        # Pages that carry no objects (S3 delete markers, unversioned entries) do not move
        # ``examined``, so the pass also stops after a bounded number of requests.
        max_pages = math.ceil(limit / page_size) * 4
        try:
            while True:
                page = await storage.list_objects(
                    summary.id, page_size=min(page_size, limit - examined), page_token=token
                )
                resumed = False
                pages += 1
                examined += len(page.objects) + page.foreign
                outcome.listed += len(page.objects) + page.foreign
                outcome.foreign += page.foreign
                old = []
                for entry in page.objects:
                    if not entry.object_ref.endswith(_ROW_OBJECT_SUFFIXES):
                        outcome.foreign += 1
                        continue
                    written = parse_row_instant(entry.created_at)
                    # An unreadable write time is treated as young: never delete on a guess.
                    if written is None or written > cutoff:
                        outcome.too_young += 1
                        continue
                    old.append(entry)
                named = await _keys_named_by_records(store, [entry.object_ref for entry in old])
                # Another database may claim the namespace while this pass runs: read the fence
                # again before the first delete of every page and then every
                # ORPHAN_FENCE_RECHECK_DELETES deletes after it.
                since_fence_check = ORPHAN_FENCE_RECHECK_DELETES
                fenced_mid_pass = False
                for entry in old:
                    if entry.object_ref in named:
                        outcome.referenced += 1
                        continue
                    if dry_run:
                        outcome.would_delete += 1
                        if len(candidates) < MAX_ORPHAN_CANDIDATES:
                            candidates.append(
                                ReportingOrphanCandidateV1(
                                    summary.id,
                                    entry.object_ref,
                                    entry.native_version_ref,
                                    entry.size_bytes,
                                    entry.created_at,
                                )
                            )
                        continue
                    if since_fence_check >= ORPHAN_FENCE_RECHECK_DELETES:
                        again = await gate(summary.id, "check")
                        if not again.ok:
                            outcome.status = "fenced"
                            outcome.fence = again.refusal
                            fenced.append(summary.id)
                            fenced_mid_pass = True
                            break
                        since_fence_check = 0
                    since_fence_check += 1
                    try:
                        # The only delete that takes the per-object lock: no record names this
                        # object, so a commit may be adopting it unrecorded. The reference check
                        # across every binding runs under the lock of every binding.
                        removed = await storage.delete_orphaned_object(
                            store._pool, summary.id, entry.object_ref, entry.native_version_ref
                        )
                        if removed:
                            outcome.deleted += 1
                        else:
                            outcome.referenced += 1
                    except Exception:
                        outcome.failed += 1
                if fenced_mid_pass:
                    break
                if page.next_page_token is not None and page.next_page_token == token:
                    drop_cursor = True
                    raise ReportingRowStoreError(
                        "PROVIDER_UNAVAILABLE", "provider returned the same page token twice"
                    )
                token = page.next_page_token
                if token is None:
                    completed.append(summary.id)
                    break
                if examined >= limit or pages >= max_pages:
                    outcome.status = "partial"
                    next_cursors[summary.id] = token
                    break
        except Exception as error:
            outcome.status = "failed"
            outcome.error = _code(error)
            outcome.failed += 1
            # A listing failure retries the same page next pass. (A delete that fails is counted
            # and the pass moves on, so one stuck object cannot hold up the rest; it is retried on
            # the next full listing.)
            if token is not None and not resumed and not drop_cursor:
                next_cursors[summary.id] = token
    return finish()


# -- the fence: claim, check, take over -----------------------------------------------------


async def _fence_row(connection: Any, scope: str) -> tuple[OrphanFenceRow, str] | None:
    row = await (
        await connection.execute(
            "SELECT epoch, token, previous_token, claimed_at"
            " FROM adcp_reporting_orphan_collector_fences WHERE fence_scope = %s",
            (scope,),
        )
    ).fetchone()
    if row is None:
        return None
    return OrphanFenceRow(int(row[0]), row[1], row[2]), format_row_instant(row[3])


async def _latest_orphan_fence(
    store: PgReportingLedgerStore, binding_id: str
) -> tuple[list[OrphanFenceEntry], tuple[int, str] | None]:
    """The object store's current claim: the highest epoch listed, read and verified."""
    storage = store._row_storage
    assert storage is not None
    entries = await storage.list_collector_fence(binding_id)
    if not entries:
        return entries, None
    record = await storage.read_collector_fence(binding_id, entries[-1])
    return entries, (record.epoch, record.token)


def _unreadable() -> _Gate:
    return _Gate(
        ReportingOrphanFenceRefusalV1(
            "fence_unreadable",
            "the collector fence objects under this namespace are malformed, oversized, "
            "duplicated or name another namespace. Inspect them; once the namespace is known to "
            "be owned by this database alone, call "
            "take_over_orphan_collector_fence(acknowledge_previous_owner_gone=True).",
        )
    )


async def _orphan_fence_gate(
    store: PgReportingLedgerStore,
    binding_id: str,
    scope: str,
    mode: Literal["claim", "check"],
    fences_table: bool,
) -> _Gate:
    """Whether this database may collect orphans under a binding's namespace.

    For a real pass this is also the claim that makes it so. ``check`` only reads: it is what
    ``dry_run`` and the per-page recheck use. ``claim`` runs under a per-fence advisory lock: it
    verifies the object store's highest epoch is exactly the claim this database recorded, records
    the next epoch and a fresh token in the database, and only then writes that epoch's object
    create-only. The intent is committed before the object is written, so a crash between the two
    is finished by the next pass instead of looking like another database's claim.

    Each real pass claims a new epoch, so a cycle spread over many partial passes writes and prunes
    one tiny object per pass. The claim holds a pool connection and the advisory lock across the
    object-store listing and read, so a slow bucket pins one connection for up to the read deadline.
    """
    if not fences_table:
        return _Gate(
            ReportingOrphanFenceRefusalV1(
                "migration_required",
                "the orphan collector fence table does not exist: apply "
                "reporting_row_storage.sql (create_schema does), then orphan collection can "
                "claim its namespace",
            )
        )
    storage = store._row_storage
    assert storage is not None
    try:
        if mode == "check":
            # The bucket first, then the row. A claim commits its intent to the row before it
            # writes the object, so a row read after the bucket is never behind a claim of this
            # database that the bucket already shows (a sibling host claiming between the two
            # reads would otherwise look like another database's claim).
            _, latest = await _latest_orphan_fence(store, binding_id)
            async with store._pool.connection() as connection:
                stored = await _fence_row(connection, scope)
            decision = decide_orphan_fence(stored[0] if stored else None, latest)
            if decision.action == "fenced":
                return _Gate(decision.refusal)
            return _Gate()

        async def plan(stored: tuple[OrphanFenceRow, str] | None) -> _Plan:
            entries, latest = await _latest_orphan_fence(store, binding_id)
            decision = decide_orphan_fence(stored[0] if stored else None, latest)
            if decision.action == "fenced":
                return _Plan(refusal=decision.refusal)
            # An unfinished claim is finished under a fresh token, never the recorded one: a copy
            # of this database taken while the claim was unfinished carries the same token, and
            # both could then "adopt" the same object. With distinct tokens, create-only leaves
            # exactly one of them the owner.
            completing = decision.action == "complete"
            assert stored is not None or not completing
            return _Plan(
                record=OrphanFenceRecord(
                    stored[0].epoch if completing and stored else _epoch(decision),
                    str(uuid.uuid4()),
                    stored[0].previous_token if completing and stored else decision.previous_token,
                    format_row_instant(datetime.now(timezone.utc)),
                ),
                entries=entries,
                previous_epoch=latest[0] if latest else None,
            )

        claimed = await _claim_orphan_fence(store, binding_id, scope, True, plan)
        if claimed.kind == "fenced":
            return _Gate(claimed.refusal)
        if claimed.kind == "lost":
            assert claimed.record is not None and claimed.found is not None
            return _Gate(
                ReportingOrphanFenceRefusalV1(
                    "claim_lost",
                    f"another database claimed epoch {claimed.record.epoch} first. If this "
                    "database is the only one that owns this namespace, call "
                    "take_over_orphan_collector_fence(acknowledge_previous_owner_gone=True); "
                    "otherwise leave orphan collection off on this copy.",
                    claimed.found.epoch,
                    claimed.record.epoch,
                )
            )
        return _Gate()
    except ReportingRowStoreError as error:
        # Fence objects that are not exactly what this collector writes fail closed.
        if error.code == "ROWS_INTEGRITY_FAILED":
            return _unreadable()
        raise


def _epoch(decision: OrphanFenceDecision) -> int:
    assert decision.epoch is not None
    return decision.epoch


@dataclass(slots=True)
class _Plan:
    refusal: ReportingOrphanFenceRefusalV1 | None = None
    record: OrphanFenceRecord | None = None
    entries: list[OrphanFenceEntry] = field(default_factory=list)
    previous_epoch: int | None = None


@dataclass(slots=True)
class _Claim:
    kind: Literal["fenced", "lost", "claimed"]
    refusal: ReportingOrphanFenceRefusalV1 | None = None
    record: OrphanFenceRecord | None = None
    found: OrphanFenceRecord | None = None
    previous_epoch: int | None = None


async def _claim_orphan_fence(
    store: PgReportingLedgerStore,
    binding_id: str,
    scope: str,
    keep_predecessor: bool,
    plan: Callable[[tuple[OrphanFenceRow, str] | None], Awaitable[_Plan]],
) -> _Claim:
    """Claim one fence epoch for this database, under a session-level advisory lock held from the
    decision to the object write.

    ``plan`` runs inside the transaction with the stored row, reads the bucket and returns either a
    refusal or the claim to make (and the entries it supersedes). The claim's intent (epoch, token)
    is committed before the object is written, so a crash between the two is finished by the next
    pass instead of looking like another database's claim; holding the lock through the write keeps
    a sibling host of this database from finishing that claim under another token while the first
    write is still in flight. A clone has its own database and its own lock, which is exactly why
    the object store's create-only write decides between them.
    """
    storage = store._row_storage
    assert storage is not None
    lock = f"adcp.orphan_fence:{scope}"
    async with store._pool.connection() as connection:
        original_autocommit = connection.autocommit
        await connection.set_autocommit(True)
        locked = False
        try:
            # A sibling holds the lock across its object-store write, which has its own deadline.
            await connection.execute(f"SET lock_timeout = '{_FENCE_LOCK_TIMEOUT}'")
            # Marked before the call: a cancellation after the server grants the lock but before
            # the client reads the reply must still unlock (unlocking a lock never taken only
            # returns false), or the connection would go back to the pool holding it.
            locked = True
            await connection.execute("SELECT pg_advisory_lock(hashtextextended(%s, 0))", (lock,))
            await connection.execute("RESET lock_timeout")
            async with connection.transaction():
                planned = await plan(await _fence_row(connection, scope))
                if planned.record is not None:
                    await connection.execute(
                        "INSERT INTO adcp_reporting_orphan_collector_fences"
                        " (fence_scope, epoch, token, previous_token, claimed_at)"
                        " VALUES (%(scope)s, %(epoch)s, %(token)s, %(previous)s, %(claimed)s)"
                        " ON CONFLICT (fence_scope) DO UPDATE"
                        "   SET epoch = EXCLUDED.epoch, token = EXCLUDED.token,"
                        "       previous_token = EXCLUDED.previous_token,"
                        "       claimed_at = EXCLUDED.claimed_at",
                        {
                            "scope": scope,
                            "epoch": planned.record.epoch,
                            "token": planned.record.token,
                            "previous": planned.record.previous_token,
                            "claimed": parse_row_instant(planned.record.claimed_at),
                        },
                    )
            if planned.refusal is not None:
                return _Claim("fenced", refusal=planned.refusal)
            assert planned.record is not None
            _, written = await storage.put_collector_fence(binding_id, planned.record)
            if written.token != planned.record.token:
                return _Claim("lost", record=planned.record, found=written)
            # A claim keeps its predecessor (an unfinished claim is recognised by it) and prunes
            # everything older. A takeover has just written and confirmed its object, so it
            # supersedes every object and version it saw.
            await storage.delete_collector_fence(
                binding_id,
                [
                    entry
                    for entry in planned.entries
                    if entry.epoch < planned.record.epoch - (1 if keep_predecessor else 0)
                ],
            )
            return _Claim("claimed", record=planned.record, previous_epoch=planned.previous_epoch)
        finally:
            try:
                await connection.execute("RESET lock_timeout")
                if locked:
                    await connection.execute(
                        "SELECT pg_advisory_unlock(hashtextextended(%s, 0))", (lock,)
                    )
                await connection.set_autocommit(original_autocommit)
            except BaseException:
                # A connection that may still hold the session lock must not go back to the pool.
                await asyncio.shield(connection.close())
                raise


async def take_over_orphan_collector_fence(
    store: PgReportingLedgerStore,
    *,
    acknowledge_previous_owner_gone: bool,
    bindings: Sequence[str] | None = None,
    include_read_only: bool = False,
) -> list[ReportingOrphanFenceTakeoverV1]:
    """Take over the orphan collector's namespace fence.

    The deliberate step after a restore, or when the database that held the fence is gone for
    good. It claims the next epoch for this database, so the previous holder (and every other copy
    of its state) is fenced out, and this database's collector can delete again.

    Do this only when this database is the one that should own the namespace. If the original is
    still running, taking over from a clone fences the original and lets the clone delete its
    objects: the acknowledgement exists because nothing else can tell them apart.
    ``acknowledge_previous_owner_gone=True`` is required.

    A takeover does not stop a collector that is already mid-pass: that pass notices the new epoch
    at its next fence check, which is at most 25 deletes later. Stop the previous owner's
    collector before you take over.

    Takes over every writable (or, with ``include_read_only``, every) object binding whose provider
    can list, or only the ``bindings`` named; bindings that share a bucket, prefix and installation
    share one fence and are reported together. Requires the fence table
    (``reporting_row_storage.sql``).
    """
    if acknowledge_previous_owner_gone is not True:
        raise ValueError(
            "take_over_orphan_collector_fence fences out the namespace's current owner: pass "
            "acknowledge_previous_owner_gone=True to confirm that database is gone or must never "
            "collect again"
        )
    storage = store._row_storage
    if storage is None:
        raise ReportingRowStoreError(
            "INVALID_INPUT", "take_over_orphan_collector_fence requires row storage"
        )
    bindings = _names(bindings, "bindings")
    await store.ready_row_storage()
    summaries = storage.object_binding_summaries()
    for binding_id in bindings or ():
        summary = next((item for item in summaries if item.id == binding_id), None)
        if summary is None:
            raise ValueError(
                f"bindings names {binding_id}, which is not a configured object binding"
            )
        if not summary.listable:
            raise ValueError(f"bindings names {binding_id}, whose provider cannot list objects")
    if "adcp_reporting_orphan_collector_fences" not in await _present_tables(store):
        raise ReportingRowStoreError(
            "STATE_UNAVAILABLE",
            "the orphan collector fence table does not exist: apply reporting_row_storage.sql",
        )
    selected = [
        summary
        for summary in summaries
        if summary.listable
        and (
            summary.id in bindings
            if bindings is not None
            else include_read_only or not summary.read_only
        )
    ]
    by_scope: dict[str, list[str]] = {}
    for summary in selected:
        scope, _, _ = await storage.collector_fence_location(summary.id)
        by_scope.setdefault(scope, []).append(summary.id)
    taken: list[ReportingOrphanFenceTakeoverV1] = []
    for scope, binding_ids in by_scope.items():
        binding_id = binding_ids[0]

        async def plan(
            stored: tuple[OrphanFenceRow, str] | None, binding_id: str = binding_id
        ) -> _Plan:
            entries = await storage.list_collector_fence(binding_id, tolerant=True)
            newest = entries[-1] if entries else None
            # A malformed newest object is exactly what a takeover is for: its key still gives the
            # epoch to supersede. Several versions of the newest epoch are ambiguous, so none of
            # them names the previous token.
            previous: OrphanFenceRecord | None = None
            if newest is not None and sum(1 for item in entries if item.epoch == newest.epoch) == 1:
                try:
                    previous = await storage.read_collector_fence(binding_id, newest)
                except ReportingRowStoreError as error:
                    if error.code != "ROWS_INTEGRITY_FAILED":
                        raise
            return _Plan(
                record=OrphanFenceRecord(
                    max(newest.epoch if newest else 0, stored[0].epoch if stored else 0) + 1,
                    str(uuid.uuid4()),
                    previous.token if previous else None,
                    format_row_instant(datetime.now(timezone.utc)),
                ),
                entries=entries,
                previous_epoch=newest.epoch if newest else None,
            )

        claimed = await _claim_orphan_fence(store, binding_id, scope, False, plan)
        if claimed.kind != "claimed" or claimed.record is None:
            lost = claimed.kind == "lost" and claimed.record is not None
            raise ReportingRowStoreError(
                "STATE_UNAVAILABLE",
                (
                    f"another database claimed epoch {claimed.record.epoch} while this takeover "
                    "ran; run it again"
                    if lost and claimed.record is not None
                    else "the takeover was refused"
                ),
            )
        taken.append(
            ReportingOrphanFenceTakeoverV1(
                tuple(binding_ids), claimed.record.epoch, claimed.previous_epoch
            )
        )
    return taken


# -- scheduling ------------------------------------------------------------------------------


def _idle_result(options: ReportingOrphanCollectionOptionsV1) -> ReportingOrphanCollectionResultV1:
    return ReportingOrphanCollectionResultV1(dry_run=options.dry_run)


async def run_orphan_collection_pass(
    store: PgReportingLedgerStore,
    options: ReportingOrphanCollectionOptionsV1,
    state: ReportingOrphanCollectionStateV1,
) -> ReportingOrphanCollectionResultV1:
    """One scheduled pass: resumes the listing where the last one stopped and honours the interval.

    ``state`` is owned by the caller and updated in place, so a large listing resumes where the
    last pass stopped and ``interval_seconds`` is honoured.
    """
    if state.next_cycle_at is not None and time.monotonic() < state.next_cycle_at:
        # Between full listings: nothing to do this pass.
        return _idle_result(options)
    outcome = await collect_orphaned_row_objects(
        store,
        dry_run=options.dry_run,
        acknowledge_exclusive_namespace=options.acknowledge_exclusive_namespace,
        min_age_seconds=options.min_age_seconds,
        limit=options.limit,
        include_read_only=options.include_read_only,
        cursors=dict(state.cursors),
        skip_bindings=list(state.completed),
    )
    state.cursors = dict(outcome.cursors)
    # A finished, clean cycle rests for the interval; anything else carries on next pass, without
    # listing again the bindings that already finished. (A delete that keeps failing does not stop
    # the rest: it is tried again on the next listing.)
    if not outcome.cursors and not any(item.status == "failed" for item in outcome.bindings):
        state.completed = []
        # A fenced binding looks again within the hour, so applying the migration or taking the
        # namespace over does not leave collection idle for the rest of a day.
        rest = options.interval_seconds
        if any(item.status == "fenced" for item in outcome.bindings):
            rest = min(rest, FENCED_RECHECK_SECONDS)
        state.next_cycle_at = time.monotonic() + rest
    else:
        state.completed = list(dict.fromkeys([*state.completed, *outcome.completed]))
        state.next_cycle_at = None
    return outcome
