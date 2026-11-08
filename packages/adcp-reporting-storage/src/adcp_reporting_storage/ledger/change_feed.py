"""Host-facing reporting change feed (shared SDK persistence spec, sections 2.1 and 3.7).

``PgReportingLedgerStore(change_feed=True)`` appends one row to ``adcp_reporting_changes``
per committed obligation, revision, adjustment and retirement, in the transaction that
writes the record. The JavaScript SDK writes the same table, so a host reads one feed.

* **Every feed**, account-scoped or deployment-wide, orders by ``(xid, seq)`` and returns
  only changes whose transaction precedes every transaction still in flight
  (``xid < pg_snapshot_xmin(pg_current_snapshot())``), so a slow commit is never skipped.
  The order does not depend on the account lock, on the order a writer drew ``seq`` in, or
  on which lock key a writer took: a change with a lower ``seq`` and a higher ``xid`` that
  commits late is returned on a later page, after the cursor. A long-running transaction
  delays every feed; it never makes one lose a record.
* **Account cursors carry ``xid``.** A cursor from before that, which carries only ``seq``,
  is accepted and mapped to a position that skips nothing (see :func:`changes_after`).
* Cursors are opaque, versioned and stable across restarts, processes and SDKs. A cursor
  older than the retained history raises :class:`ReportingChangeCursorExpiredError`; the
  consumer then resynchronizes from ``list_current_revisions`` and resumes from the
  cursor that read returns. The horizon is read again after the page, so a prune that
  commits mid-read raises the same error (for a cursorless read too) instead of returning
  a page that skipped the pruned changes.
* A named consumer registered with ``save_feed_consumer_cursor`` holds back change pruning
  (and period retirement) until it has passed those changes in ``(xid, seq)`` order, up to
  ``max_feed_hold_days``
  after its last update, so a dead consumer cannot block retention indefinitely. Its
  checkpoint is forward-only within a feed scope; ``rewind=True`` replays deliberately and
  ``remove_feed_consumer`` unregisters it.

These are host APIs, not buyer surfaces. Buyers keep the protocol's ``changes_checkpoint``
and ``changes_after``, which project from ``reporting_ledger_changes`` until the shared
Core layout (persistence Stage 3).
"""

from __future__ import annotations

import base64
import binascii
import json
import re
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, NamedTuple

if TYPE_CHECKING:
    from adcp_reporting_storage.ledger.pg import PgReportingLedgerStore

__all__ = [
    "REPORTING_CHANGES_DDL_PATH",
    "ReportingChangeCursorExpiredError",
    "ReportingChangeKindV1",
    "ReportingChangeRecordV1",
    "ReportingChangesPageV1",
    "changes_after",
    "decode_reporting_change_cursor_v1",
    "encode_reporting_change_cursor_v1",
    "is_legacy_account_change_cursor",
    "prune_changes",
    "remove_feed_consumer",
    "save_feed_consumer_cursor",
]

REPORTING_CHANGES_DDL_PATH = Path(__file__).with_name("reporting_changes.sql")

ReportingChangeKindV1 = Literal["obligation", "revision", "adjustment", "retirement"]
_KINDS = ("obligation", "revision", "adjustment", "retirement")
_CONSUMER_NAME = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")


@dataclass(frozen=True, slots=True)
class ReportingChangeRecordV1:
    kind: ReportingChangeKindV1
    record_id: str
    reporting_obligation_id: str
    account_id: str
    #: RFC 3339 UTC with six fractional digits, from the committing database's clock.
    recorded_at: str


@dataclass(frozen=True, slots=True)
class ReportingChangesPageV1:
    records: tuple[ReportingChangeRecordV1, ...]
    #: Pass back to resume. Always present; equal to the input when nothing new committed.
    cursor: str


class ReportingChangeCursorExpiredError(RuntimeError):
    """A change cursor is older than the retained change history.

    Resynchronize from ``list_current_revisions`` and resume from the cursor it returns.
    """

    code = "CURSOR_EXPIRED"

    def __init__(self) -> None:
        super().__init__(
            "Reporting change cursor is older than the retained change history; "
            "resynchronize from current revisions"
        )


_INT64_MAX = 2**63 - 1
_UINT64_MAX = 2**64 - 1


def _fits_unsigned(value: object, maximum: int) -> bool:
    """A decimal string PostgreSQL's ``bigint`` / ``xid8`` cast would accept.

    A value it would reject must fail as ``ValueError`` here, not as a raw database error
    mid-query. ASCII digits only: ``\\d`` also matches other scripts' digits, which ``int``
    accepts and the database cast does not.
    """
    return (
        isinstance(value, str)
        and re.fullmatch(r"[0-9]{1,20}", value) is not None
        and int(value) <= maximum
    )


def encode_reporting_change_cursor_v1(
    *, account: str | None, seq: str, xid: str | None = None
) -> str:
    """Encode a cursor. Every cursor carries ``xid``; only a seq-only account cursor omits it.

    Omitting ``xid`` for an account is how a cursor from before the account feed ordered by
    ``(xid, seq)`` is spelled; :func:`changes_after` maps one to a position that skips nothing.
    """
    cursor: dict[str, Any] = {"v": 1, "account": account, "seq": seq}
    if account is None or xid is not None:
        cursor["xid"] = xid
    raw = json.dumps(cursor, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def decode_reporting_change_cursor_v1(value: str, account: str | None) -> dict[str, Any]:
    """Decode and validate a cursor for the feed it names (``account`` or deployment-wide)."""
    try:
        padded = value + "=" * (-len(value) % 4)
        parsed = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")))
    except (ValueError, binascii.Error, UnicodeError):
        raise ValueError("change cursor is malformed") from None
    if (
        not isinstance(parsed, dict)
        or parsed.get("v") != 1
        or parsed.get("account") != account
        or not _fits_unsigned(parsed.get("seq"), _INT64_MAX)
        or (
            account is None
            if "xid" not in parsed
            else not _fits_unsigned(parsed["xid"], _UINT64_MAX)
        )
    ):
        raise ValueError("change cursor does not belong to this feed")
    return parsed


def is_legacy_account_change_cursor(cursor: dict[str, Any]) -> bool:
    """True for an account cursor issued before account feeds ordered by ``(xid, seq)``.

    It carries a ``seq`` and no ``xid``. :func:`changes_after` accepts it and maps it to a
    position that cannot skip a change.
    """
    return cursor.get("account") is not None and "xid" not in cursor


def _positive(value: int, name: str, maximum: int) -> None:
    if type(value) is not int or not 1 <= value <= maximum:
        raise ValueError(f"{name} must be between 1 and {maximum}")


def _require_feed(store: PgReportingLedgerStore) -> None:
    if not store._change_feed:
        raise ValueError("this operation requires the store change_feed option")


def feed_consumer_behind_change(consumer: str, change: str) -> str:
    """SQL condition: the registered consumer row has not yet passed the change row.

    In the consumer's own feed order: every consumer reads ``(xid, seq)`` order, an account
    consumer only within its account. A row saved before account feeds carried an ``xid`` has
    a NULL ``cursor_xid`` and holds by ``seq`` until the consumer saves its next cursor.
    Shared by change pruning, period retirement and anything else that honours consumer holds.
    """
    return (
        f"(({consumer}.account_id IS NULL OR {consumer}.account_id = {change}.account_id)"
        f" AND CASE WHEN {consumer}.cursor_xid IS NULL"
        f" THEN {change}.seq > {consumer}.cursor_seq"
        f" ELSE ({change}.xid, {change}.seq) > ({consumer}.cursor_xid, {consumer}.cursor_seq)"
        " END)"
    )


def _page_query(*, account: bool) -> str:
    """The change page query for an account feed or the deployment feed.

    Both feeds order by ``(xid, seq)`` and stop below the reading snapshot's ``xmin``. Every
    change with a higher ``xid`` than a returned one either committed before it or is not
    returned yet, so a cursor never passes a change that commits late, with no assumption about
    the lock a writer took or the order it drew ``seq`` in. The account scan is served by the
    ``(account_id, xid, seq)`` index, in order.

    Every column is qualified with the table alias, in the projection and in ORDER BY.
    ``seq::text`` is *named* ``seq``, and PostgreSQL resolves a bare ORDER BY name to the output
    column first, so ``ORDER BY seq`` would sort 100 before 95 as text and a page boundary would
    silently skip changes. ``change.seq`` always means the bigint column.
    """
    columns = (
        "change.seq::text AS cursor_seq, change.xid::text AS cursor_xid, change.account_id,"
        " change.record_kind, change.record_id, change.obligation_id,"
        " to_char(change.recorded_at AT TIME ZONE 'UTC',"
        ' \'YYYY-MM-DD"T"HH24:MI:SS.US"Z"\')'
    )
    scope = " change.account_id = %s AND" if account else ""
    return (
        f"SELECT {columns} FROM adcp_reporting_changes change"  # noqa: S608  # nosec B608
        f" WHERE{scope} (change.xid, change.seq) > (%s::text::xid8, %s::bigint)"
        " AND change.xid < pg_snapshot_xmin(pg_current_snapshot())"
        " AND (%s::text[] IS NULL OR change.record_kind = ANY(%s::text[]))"
        " ORDER BY change.xid, change.seq LIMIT %s"
    )


class _Horizon(NamedTuple):
    xid: str
    #: The ``seq`` of the highest ``(xid, seq)`` pruned.
    seq: str
    #: The highest ``seq`` pruned whatever its ``xid``: the exact check for a seq-only cursor.
    max_seq: str


async def _read_horizon(connection: Any, account_id: str | None) -> _Horizon:
    """The pruned horizon in this feed's own order."""
    if account_id is not None:
        row = await (
            await connection.execute(
                "SELECT pruned_through_xid::text, pruned_through_xid_seq::text,"
                " pruned_through_seq::text FROM adcp_reporting_change_account_horizon"
                " WHERE account_id = %s",
                (account_id,),
            )
        ).fetchone()
    else:
        row = await (
            await connection.execute(
                "SELECT pruned_through_xid::text, pruned_through_seq::text,"
                " pruned_through_seq::text FROM adcp_reporting_change_horizon WHERE singleton"
            )
        ).fetchone()
    return _Horizon(*row) if row else _Horizon("0", "0", "0")


def _assert_retained(position: tuple[str | None, str], horizon: _Horizon) -> None:
    """Raise unless ``position`` ``(xid, seq)`` is at or past the pruned ``horizon``.

    A position without an ``xid`` is a seq-only account cursor, checked against the highest
    ``seq`` ever pruned: a tuple compare would miss a pruned change with a higher ``seq`` and
    a lower ``xid``.
    """
    xid, seq = position
    behind = (
        int(horizon.max_seq) > int(seq)
        if xid is None
        else (int(horizon.xid), int(horizon.seq)) > (int(xid), int(seq))
    )
    if behind:
        raise ReportingChangeCursorExpiredError()


async def _legacy_account_cursor_xid(connection: Any, account_id: str, seq: str) -> str:
    """Map a seq-only account cursor to a transaction id that cannot skip a change.

    Every change after the cursor's ``seq`` has an ``xid`` at or above both the oldest such
    change that is visible now and the reading snapshot's ``xmin`` (a change that is not
    visible yet belongs to a transaction that was in flight, or started after the snapshot,
    whose id is at or above ``xmin``), so the position ``(xid, seq)`` below the smaller of the
    two hands them all out.
    """
    row = await (
        await connection.execute(
            "SELECT LEAST("
            " pg_snapshot_xmin(pg_current_snapshot()),"
            " COALESCE((SELECT change.xid FROM adcp_reporting_changes change"
            "   WHERE change.account_id = %s AND change.seq > %s::bigint"
            "   ORDER BY change.xid LIMIT 1), pg_snapshot_xmin(pg_current_snapshot())))::text",
            (account_id, seq),
        )
    ).fetchone()
    return str(row[0])


async def changes_after(
    store: PgReportingLedgerStore,
    *,
    cursor: str | None = None,
    account_id: str | None = None,
    kinds: Sequence[ReportingChangeKindV1] | None = None,
    limit: int = 500,
) -> ReportingChangesPageV1:
    """Read the host change feed. Omit ``cursor`` to start at the oldest retained change.

    Every feed, account-scoped or deployment-wide, orders by ``(xid, seq)`` and returns only
    changes whose transaction precedes every transaction still in flight, so no commit is ever
    skipped and the order does not depend on the account lock. An empty page can therefore
    mean caught up or held back by an older open transaction (``xmin`` is cluster-wide).

    An account cursor issued before account feeds carried an ``xid`` holds only a ``seq``. It
    is accepted and mapped to a position that cannot skip a change: the lowest transaction id
    any change after its ``seq`` could have (the smaller of the oldest such change's ``xid``
    and the reading snapshot's ``xmin``). That can hand out a few changes the consumer already
    read, those with a lower ``seq`` and a higher ``xid``, so consumers must treat redelivery
    as normal. The page returns a cursor in the new format, even when it is empty. A seq-only
    cursor raises ``CURSOR_EXPIRED`` when any change with a higher ``seq`` was pruned.

    Raises :class:`ReportingChangeCursorExpiredError` (``code == "CURSOR_EXPIRED"``) when the
    starting position is older than the retained history. A read **without** a ``cursor`` can
    raise it too: the pruned horizon is read again after the page, so a prune that commits
    mid-read rejects the page instead of returning one that silently skips the pruned changes.
    Retrying starts from the new, later horizon.
    """
    _require_feed(store)
    _positive(limit, "limit", 10_000)
    if kinds is not None and any(kind not in _KINDS for kind in kinds):
        raise ValueError("unknown change kind")
    decoded = decode_reporting_change_cursor_v1(cursor, account_id) if cursor is not None else None
    legacy = decoded is not None and is_legacy_account_change_cursor(decoded)
    selected = list(kinds) if kinds else None
    async with store._pool.connection() as connection:
        # The pruned horizon in this feed's own order. A supplied cursor below it may have
        # missed a pruned change and fails closed; a consumer with no cursor begins at it.
        horizon = await _read_horizon(connection, account_id)
        checked: tuple[str | None, str] | None = None
        if decoded is not None:
            checked = (decoded.get("xid"), decoded["seq"])
            _assert_retained(checked, horizon)
            position_seq = decoded["seq"]
            position_xid = (
                await _legacy_account_cursor_xid(connection, account_id, position_seq)
                if legacy and account_id is not None
                else decoded["xid"]
            )
        else:
            position_xid, position_seq = horizon.xid, horizon.seq
        query = _page_query(account=account_id is not None)
        scope: tuple[Any, ...] = (account_id,) if account_id is not None else ()
        rows = await (
            await connection.execute(
                query, (*scope, position_xid, position_seq, selected, selected, limit)
            )
        ).fetchall()
        # Pruning can commit between the horizon read and the page read, and a prune deletes rows
        # and advances the horizon in one transaction. Read the horizon again, as a new statement
        # after the page, and compare it with the position this page started from: a page read
        # after such a prune lacks the pruned changes, and handing out a cursor past them would
        # skip them silently. This covers a read with no cursor too. Retrying starts from the
        # new, later horizon. A prune that commits after this read cannot have affected the page.
        _assert_retained(
            checked or (position_xid, position_seq), await _read_horizon(connection, account_id)
        )
    records = tuple(ReportingChangeRecordV1(row[3], row[4], row[5], row[2], row[6]) for row in rows)
    if rows:
        last = rows[-1]
        next_cursor = encode_reporting_change_cursor_v1(
            account=account_id, seq=last[0], xid=last[1]
        )
    elif cursor is not None and not legacy:
        next_cursor = cursor
    else:
        # A seq-only cursor is replaced by its mapped position even on an empty page, so the
        # consumer saves a cursor in the new format.
        next_cursor = encode_reporting_change_cursor_v1(
            account=account_id, seq=position_seq, xid=position_xid
        )
    return ReportingChangesPageV1(records, next_cursor)


async def save_feed_consumer_cursor(
    store: PgReportingLedgerStore,
    name: str,
    cursor: str,
    *,
    refresh_unchanged: bool = True,
    rewind: bool = False,
) -> bool:
    """Record a named durable consumer's position in its own feed order.

    A registered consumer holds back change pruning and period retirement until it has
    passed those changes, until ``max_feed_hold_days`` after its last update.

    Checkpoints are forward-only within a feed scope: a cursor behind the saved one is
    ignored, so a delayed or repeated acknowledgment cannot pull the retention hold back or
    make the consumer reprocess changes. Every consumer compares ``(xid, seq)``. Returns
    ``True`` when the checkpoint was written and
    ``False`` when it was ignored (behind the saved position, or equal with
    ``refresh_unchanged=False``).

    To rewind a consumer deliberately, for example to replay changes, pass ``rewind=True``:
    the saved position and retention hold then move to ``cursor`` and the write always
    applies. The cursor is not checked against the retention horizon, so it must still be
    inside the retained history or the next read raises
    :class:`ReportingChangeCursorExpiredError`. Any change of ``account_id`` (account to
    deployment, deployment to account, or between accounts) replaces the position. The scope
    is read from the cursor itself, so pass only cursors your own code obtained from
    :func:`changes_after`, never ones supplied by an untrusted caller.

    ``refresh_unchanged=False`` recovers an acknowledgment without renewing the retention
    hold when its position has not advanced.

    A seq-only account cursor (from before account feeds carried an ``xid``) is mapped to the
    position :func:`changes_after` would read it from, so the hold sits below every unread
    change. A consumer row saved before that upgrade has a NULL ``cursor_xid`` and holds by
    ``seq`` until its next save; a cursor in the new format replaces it unless its ``seq`` is
    behind.
    """
    _require_feed(store)
    if not isinstance(name, str) or not _CONSUMER_NAME.match(name):
        raise ValueError("feed consumer name is invalid")
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        account = json.loads(base64.urlsafe_b64decode(padded.encode("ascii"))).get("account")
    except (ValueError, binascii.Error, UnicodeError, AttributeError):
        raise ValueError("feed consumer cursor is invalid") from None
    if account is not None and not isinstance(account, str):
        raise ValueError("feed consumer cursor is invalid")
    decoded = decode_reporting_change_cursor_v1(cursor, account)
    # One atomic upsert: the WHERE of DO UPDATE is re-evaluated against the latest committed
    # row version under the row lock, so concurrent writers cannot interleave a stale position
    # over a newer one. No row is returned when the update is skipped. Forward-only in
    # (xid, seq) order for every scope. A row saved before account feeds carried an xid (NULL
    # cursor_xid) has only a seq to compare: a cursor in the new format replaces it unless its
    # seq is behind.
    async with store._pool.connection() as connection:
        xid = (
            await _legacy_account_cursor_xid(connection, account, decoded["seq"])
            if account is not None and is_legacy_account_change_cursor(decoded)
            else decoded["xid"]
        )
        result = await connection.execute(
            "INSERT INTO adcp_reporting_feed_consumers"
            " (consumer_name, account_id, cursor_xid, cursor_seq)"
            " VALUES (%(name)s, %(account)s, %(xid)s::text::xid8, %(seq)s::bigint)"
            " ON CONFLICT (consumer_name) DO UPDATE"
            "   SET account_id = EXCLUDED.account_id, cursor_xid = EXCLUDED.cursor_xid,"
            "       cursor_seq = EXCLUDED.cursor_seq, updated_at = clock_timestamp()"
            " WHERE %(rewind)s::boolean"
            "    OR adcp_reporting_feed_consumers.account_id IS DISTINCT FROM EXCLUDED.account_id"
            "    OR (adcp_reporting_feed_consumers.cursor_xid IS NULL"
            "        AND adcp_reporting_feed_consumers.cursor_seq <= EXCLUDED.cursor_seq)"
            "    OR (adcp_reporting_feed_consumers.cursor_xid,"
            "        adcp_reporting_feed_consumers.cursor_seq)"
            "       < (EXCLUDED.cursor_xid, EXCLUDED.cursor_seq)"
            "    OR (%(refresh)s::boolean AND"
            "        (adcp_reporting_feed_consumers.cursor_xid,"
            "         adcp_reporting_feed_consumers.cursor_seq)"
            "        = (EXCLUDED.cursor_xid, EXCLUDED.cursor_seq))"
            " RETURNING consumer_name",
            {
                "name": name,
                "account": account,
                "xid": xid,
                "seq": decoded["seq"],
                "refresh": refresh_unchanged,
                "rewind": rewind,
            },
        )
        return await result.fetchone() is not None


async def remove_feed_consumer(store: PgReportingLedgerStore, name: str) -> bool:
    """Unregister a durable consumer so it stops holding back pruning and period retention.

    Returns ``True`` when a registration was removed. Saving a cursor under the same name
    registers it again.
    """
    _require_feed(store)
    if not isinstance(name, str) or not _CONSUMER_NAME.match(name):
        raise ValueError("feed consumer name is invalid")
    async with store._pool.connection() as connection:
        result = await connection.execute(
            "DELETE FROM adcp_reporting_feed_consumers WHERE consumer_name = %s"
            " RETURNING consumer_name",
            (name,),
        )
        return await result.fetchone() is not None


async def prune_changes(
    store: PgReportingLedgerStore,
    *,
    change_retention_days: int = 30,
    max_feed_hold_days: int = 7,
    limit: int = 10_000,
) -> int:
    """Delete change rows past retention that every live consumer has passed.

    A consumer updated within ``max_feed_hold_days`` holds back every change it has not
    passed, in its own feed order. The horizons advance in the same transaction, so a
    cursor that missed a pruned change always fails closed. Returns the rows deleted.
    """
    _require_feed(store)
    _positive(change_retention_days, "change_retention_days", 36_500)
    _positive(max_feed_hold_days, "max_feed_hold_days", 36_500)
    _positive(limit, "limit", 1_000_000)
    async with store._pool.connection() as connection, connection.transaction():
        victims = await (
            await connection.execute(
                "WITH victims AS ("  # noqa: S608  # nosec B608
                " SELECT change.seq FROM adcp_reporting_changes change"
                "  WHERE change.recorded_at < clock_timestamp() - (%s::integer * INTERVAL '1 day')"
                "    AND NOT EXISTS (SELECT 1 FROM adcp_reporting_feed_consumers consumer"
                "      WHERE consumer.updated_at >"
                "            clock_timestamp() - (%s::integer * INTERVAL '1 day')"
                f"        AND {feed_consumer_behind_change('consumer', 'change')})"
                # Victims in (xid, seq) order, so a batch cut by the limit leaves nothing
                # retained below the per-account (xid, seq) horizon recorded below.
                "  ORDER BY change.xid, change.seq LIMIT %s)"
                " DELETE FROM adcp_reporting_changes change USING victims"
                "  WHERE change.seq = victims.seq"
                " RETURNING change.seq::text, change.xid::text, change.account_id",
                (change_retention_days, max_feed_hold_days, limit),
            )
        ).fetchall()
        if not victims:
            return 0
        highest = max(victims, key=lambda row: (int(row[1]), int(row[0])))
        await connection.execute(
            "UPDATE adcp_reporting_change_horizon"
            " SET (pruned_through_xid, pruned_through_seq) = (%s::text::xid8, %s::bigint)"
            " WHERE singleton AND (%s::text::xid8, %s::bigint) >"
            "       (pruned_through_xid, pruned_through_seq)",
            (highest[1], highest[0], highest[1], highest[0]),
        )
        # Per account: the highest (xid, seq) pruned, which a cursor is compared with, and the
        # highest seq pruned whatever its xid, which is the exact check for a seq-only cursor.
        per_account: dict[str, tuple[int, int, int]] = {}
        for seq_text, xid_text, account in victims:
            seq, xid = int(seq_text), int(xid_text)
            top_xid, top_seq, max_seq = per_account.get(account, (xid, seq, seq))
            if (xid, seq) > (top_xid, top_seq):
                top_xid, top_seq = xid, seq
            per_account[account] = (top_xid, top_seq, max(max_seq, seq))
        for account, (top_xid, top_seq, max_seq) in per_account.items():
            await connection.execute(
                "INSERT INTO adcp_reporting_change_account_horizon AS horizon"
                " (account_id, pruned_through_seq, pruned_through_xid, pruned_through_xid_seq)"
                " VALUES (%(account)s, %(max_seq)s::bigint, %(xid)s::text::xid8,"
                " %(xid_seq)s::bigint)"
                " ON CONFLICT (account_id) DO UPDATE"
                "   SET pruned_through_seq = GREATEST(horizon.pruned_through_seq,"
                "                                     EXCLUDED.pruned_through_seq),"
                "       pruned_through_xid = CASE"
                "         WHEN (EXCLUDED.pruned_through_xid, EXCLUDED.pruned_through_xid_seq)"
                "              > (horizon.pruned_through_xid, horizon.pruned_through_xid_seq)"
                "         THEN EXCLUDED.pruned_through_xid ELSE horizon.pruned_through_xid END,"
                "       pruned_through_xid_seq = CASE"
                "         WHEN (EXCLUDED.pruned_through_xid, EXCLUDED.pruned_through_xid_seq)"
                "              > (horizon.pruned_through_xid, horizon.pruned_through_xid_seq)"
                "         THEN EXCLUDED.pruned_through_xid_seq"
                "         ELSE horizon.pruned_through_xid_seq END",
                {
                    "account": account,
                    "max_seq": str(max_seq),
                    "xid": str(top_xid),
                    "xid_seq": str(top_seq),
                },
            )
        return len(victims)
