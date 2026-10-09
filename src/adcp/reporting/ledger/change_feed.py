"""Host-facing reporting change feed (shared SDK persistence spec, sections 2.1 and 3.7).

``PgReportingLedgerStore(change_feed=True)`` appends one row to ``adcp_reporting_changes``
per committed obligation, revision, adjustment and retirement, in the transaction that
writes the record and after the account lock, so per-account sequence order is commit
order. The JavaScript SDK writes the same table, so a host reads one feed.

* **Account feeds** order by ``seq``.
* **The deployment-wide feed** orders by ``(xid, seq)`` and returns only changes whose
  transaction precedes every transaction still in flight, so a slow commit is never
  skipped. A long-running transaction delays this feed; it never makes it lose a record.
* Cursors are opaque, versioned and stable across restarts, processes and SDKs. A cursor
  older than the retained history raises :class:`ReportingChangeCursorExpiredError`; the
  consumer then resynchronizes from ``list_current_revisions`` and resumes from the
  cursor that read returns.
* A named consumer registered with ``save_feed_consumer_cursor`` holds back change pruning
  (and period retirement) until it has passed those changes, up to ``max_feed_hold_days``
  after its last update, so a dead consumer cannot block retention indefinitely.

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
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    from adcp.reporting.ledger.pg import PgReportingLedgerStore

__all__ = [
    "REPORTING_CHANGES_DDL_PATH",
    "ReportingChangeCursorExpiredError",
    "ReportingChangeKindV1",
    "ReportingChangeRecordV1",
    "ReportingChangesPageV1",
    "changes_after",
    "decode_reporting_change_cursor_v1",
    "encode_reporting_change_cursor_v1",
    "prune_changes",
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


def encode_reporting_change_cursor_v1(
    *, account: str | None, seq: str, xid: str | None = None
) -> str:
    cursor: dict[str, Any] = {"v": 1, "account": account, "seq": seq}
    if account is None:
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
        or not isinstance(parsed.get("seq"), str)
        or not re.fullmatch(r"\d{1,19}", parsed["seq"])
        or (
            not isinstance(parsed.get("xid"), str) or not re.fullmatch(r"\d{1,20}", parsed["xid"])
            if account is None
            else "xid" in parsed
        )
    ):
        raise ValueError("change cursor does not belong to this feed")
    return parsed


def _positive(value: int, name: str, maximum: int) -> None:
    if type(value) is not int or not 1 <= value <= maximum:
        raise ValueError(f"{name} must be between 1 and {maximum}")


def _require_feed(store: PgReportingLedgerStore) -> None:
    if not store._change_feed:
        raise ValueError("this operation requires the store change_feed option")


async def changes_after(
    store: PgReportingLedgerStore,
    *,
    cursor: str | None = None,
    account_id: str | None = None,
    kinds: Sequence[ReportingChangeKindV1] | None = None,
    limit: int = 500,
) -> ReportingChangesPageV1:
    """Read the host change feed. Omit ``cursor`` to start at the oldest retained change."""
    _require_feed(store)
    _positive(limit, "limit", 10_000)
    if kinds is not None and any(kind not in _KINDS for kind in kinds):
        raise ValueError("unknown change kind")
    decoded = decode_reporting_change_cursor_v1(cursor, account_id) if cursor is not None else None
    selected = list(kinds) if kinds else None
    async with store._pool.connection() as connection:
        # The pruned horizon in this feed's own order. A supplied cursor below it may have
        # missed a pruned change and fails closed; a consumer with no cursor begins at it.
        if account_id is not None:
            row = await (
                await connection.execute(
                    "SELECT pruned_through_seq::text FROM adcp_reporting_change_account_horizon"
                    " WHERE account_id = %s",
                    (account_id,),
                )
            ).fetchone()
            horizon_seq, horizon_xid = (row[0] if row else "0"), "0"
        else:
            row = await (
                await connection.execute(
                    "SELECT pruned_through_xid::text, pruned_through_seq::text"
                    " FROM adcp_reporting_change_horizon WHERE singleton"
                )
            ).fetchone()
            horizon_xid, horizon_seq = (row[0], row[1]) if row else ("0", "0")
        if decoded is not None:
            behind = (
                int(horizon_seq) > int(decoded["seq"])
                if account_id is not None
                else (int(horizon_xid), int(horizon_seq))
                > (int(decoded["xid"]), int(decoded["seq"]))
            )
            if behind:
                raise ReportingChangeCursorExpiredError()
        position_seq = decoded["seq"] if decoded else horizon_seq
        position_xid = decoded.get("xid", "0") if decoded else horizon_xid
        # Every column is qualified with the table alias, in the projection and in ORDER BY.
        # ``seq::text`` is *named* ``seq``, and PostgreSQL resolves a bare ORDER BY name to the
        # output column first, so ``ORDER BY seq`` would sort 100 before 95 as text and a page
        # boundary would silently skip changes. ``change.seq`` always means the bigint column.
        columns = (
            "change.seq::text AS cursor_seq, change.xid::text AS cursor_xid, change.account_id,"
            " change.record_kind, change.record_id, change.obligation_id,"
            " to_char(change.recorded_at AT TIME ZONE 'UTC',"
            ' \'YYYY-MM-DD"T"HH24:MI:SS.US"Z"\')'
        )
        if account_id is not None:
            query = (
                f"SELECT {columns} FROM adcp_reporting_changes change"  # noqa: S608  # nosec B608
                " WHERE change.account_id = %s AND change.seq > %s::bigint"
                " AND (%s::text[] IS NULL OR change.record_kind = ANY(%s::text[]))"
                " ORDER BY change.seq LIMIT %s"
            )
            params: tuple[Any, ...] = (account_id, position_seq, selected, selected, limit)
        else:
            query = (
                f"SELECT {columns} FROM adcp_reporting_changes change"  # noqa: S608  # nosec B608
                " WHERE (change.xid, change.seq) > (%s::text::xid8, %s::bigint)"
                " AND change.xid < pg_snapshot_xmin(pg_current_snapshot())"
                " AND (%s::text[] IS NULL OR change.record_kind = ANY(%s::text[]))"
                " ORDER BY change.xid, change.seq LIMIT %s"
            )
            params = (position_xid, position_seq, selected, selected, limit)
        rows = await (await connection.execute(query, params)).fetchall()
    records = tuple(ReportingChangeRecordV1(row[3], row[4], row[5], row[2], row[6]) for row in rows)
    if rows:
        last = rows[-1]
        next_cursor = encode_reporting_change_cursor_v1(
            account=account_id, seq=last[0], xid=last[1] if account_id is None else None
        )
    else:
        next_cursor = cursor or encode_reporting_change_cursor_v1(
            account=account_id, seq=position_seq, xid=position_xid if account_id is None else None
        )
    return ReportingChangesPageV1(records, next_cursor)


async def save_feed_consumer_cursor(store: PgReportingLedgerStore, name: str, cursor: str) -> None:
    """Record a named durable consumer's position in its own feed order.

    A registered consumer holds back change pruning and period retirement until it has
    passed those changes, until ``max_feed_hold_days`` after its last update.
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
    async with store._pool.connection() as connection:
        await connection.execute(
            "INSERT INTO adcp_reporting_feed_consumers"
            " (consumer_name, account_id, cursor_xid, cursor_seq)"
            " VALUES (%s, %s, %s::text::xid8, %s::bigint)"
            " ON CONFLICT (consumer_name) DO UPDATE"
            "   SET account_id = EXCLUDED.account_id, cursor_xid = EXCLUDED.cursor_xid,"
            "       cursor_seq = EXCLUDED.cursor_seq, updated_at = clock_timestamp()",
            (name, account, decoded.get("xid") if account is None else None, decoded["seq"]),
        )


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
                "WITH victims AS ("
                " SELECT change.seq FROM adcp_reporting_changes change"
                "  WHERE change.recorded_at < clock_timestamp() - (%s::integer * INTERVAL '1 day')"
                "    AND NOT EXISTS (SELECT 1 FROM adcp_reporting_feed_consumers consumer"
                "      WHERE consumer.updated_at >"
                "            clock_timestamp() - (%s::integer * INTERVAL '1 day')"
                "        AND ((consumer.account_id IS NULL"
                "              AND (change.xid, change.seq) >"
                "                  (consumer.cursor_xid, consumer.cursor_seq))"
                "          OR (consumer.account_id = change.account_id"
                "              AND change.seq > consumer.cursor_seq)))"
                "  ORDER BY change.seq LIMIT %s)"
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
        per_account: dict[str, int] = {}
        for seq, _, account in victims:
            per_account[account] = max(per_account.get(account, 0), int(seq))
        for account, seq in per_account.items():
            await connection.execute(
                "INSERT INTO adcp_reporting_change_account_horizon (account_id, pruned_through_seq)"
                " VALUES (%s, %s::bigint) ON CONFLICT (account_id) DO UPDATE"
                "   SET pruned_through_seq = GREATEST("
                "       adcp_reporting_change_account_horizon.pruned_through_seq,"
                "       EXCLUDED.pruned_through_seq)",
                (account, seq),
            )
        return len(victims)
