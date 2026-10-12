"""Write path for revision rows: prepare, stage external uploads, persist.

Shared SDK persistence spec, adcontextprotocol/adcp#7996, section 3.5. For an
``object`` binding the order is:

1. **Prepare.** Canonicalize and chunk the rows, prove they reproduce the revision's
   committed digest, and pick the binding. Pure CPU, no database, no lock.
2. **Stage** (:func:`stage_revision_rows`), outside any transaction and without the
   account lock:

   * return at once when the revision is already committed (a replay), or when a
     live row set in the same obligation already holds byte-identical rows (a
     header-only revision references those chunks instead of uploading again);
   * check the lease budget: the generation's worker lease must outlive the upload
     deadline plus a margin, or the write is refused;
   * open, or take over an *expired*, write intent. The intent's expiry derives from
     the database lease, never a host clock;
   * upload each chunk create-only, recording every object **this call created** in
     the intent before the next upload. Identical bytes already at a key are adopted
     and never recorded, so a sweep never deletes them.
3. **Persist** (:func:`persist_revision_row_set`), inside the ledger transaction under
   the account lock: write the row set and chunk manifests and delete the intent
   ``WHERE state = 'open' AND owner_token = <ours>``. If that matches nothing the
   transaction fails and the header does not commit, because a sweep or a takeover
   got there first.

A crash, a lost lease or a numbering conflict leaves an intent that the sweep
resolves. A header never commits without its bytes. The ``postgres`` kind skips
staging and writes bodies in the commit transaction.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from adcp.reporting.ledger.row_storage import (
    ReportingObjectRowBindingV1,
    ReportingPreparedRowSetV1,
    ReportingRowChunkLocatorV1,
    ReportingRowStoreError,
)
from adcp.reporting.ledger.row_storage_errors import ReportingRowBindingMismatchError
from adcp.reporting.ledger.store import LedgerConflictError

if TYPE_CHECKING:
    from adcp.reporting.ledger.models import ReportingRevisionRecord
    from adcp.reporting.ledger.pg import PgReportingLedgerStore

__all__ = [
    "StagedRevisionRows",
    "persist_revision_row_set",
    "prepare_revision_row_set",
    "stage_revision_rows",
]

#: Margin the lease must hold beyond the upload deadline, in milliseconds.
_LEASE_MARGIN_MILLISECONDS = 5_000


@dataclass(slots=True)
class StagedRevisionRows:
    """Rows prepared (and, for an object binding, uploaded) ahead of the ledger commit."""

    prepared: ReportingPreparedRowSetV1
    locators: list[ReportingRowChunkLocatorV1] | None = None
    #: Owner of the open write intent, when this stage uploaded.
    owner_token: str | None = None
    #: Row set whose chunks this revision references instead of uploading.
    shared_from: str | None = None


async def prepare_revision_row_set(
    store: PgReportingLedgerStore,
    revision: ReportingRevisionRecord,
    rows: Sequence[dict[str, Any]],
    *,
    binding_id: str | None = None,
    period_start: str | None = None,
    source_timezone: str | None = None,
) -> ReportingPreparedRowSetV1:
    """Chunk a revision's rows and prove them against its committed binding.

    A protocol-shape revision's chunks must reproduce ``revision_content_sha256``
    exactly. A retained pair-hashed revision cannot: its binding hashes
    ``{name, value}`` pairs, so its row set records the rows-only digest and the
    pair binding is proven here from the supplied rows instead.
    """
    from adcp.reporting.ledger.producer import (
        recompute_revision_content_sha256,
        revision_binding_algorithm,
    )

    storage = store._row_storage
    assert storage is not None
    legacy = revision_binding_algorithm(revision) == "legacy_py_core_pairs_v0"
    if legacy:
        try:
            bound = recompute_revision_content_sha256(revision, rows)
        except Exception:
            raise LedgerConflictError(
                "ROWS_NOT_PORTABLE", "revision rows cannot be canonicalized for storage"
            ) from None
        if bound != revision.revision_content_sha256.lower():
            raise LedgerConflictError(
                "REVISION_CONTENT_MISMATCH",
                "revision row content does not match its immutable binding",
            )
    try:
        return await storage.prepare(
            row_set_id=revision.reporting_revision_id,
            row_set_kind="revision",
            account_id=revision.account_id,
            obligation_id=revision.reporting_obligation_id,
            rows=rows,
            digest_profile="rows_v1" if legacy else "revision_envelope_v1",
            control_totals=[item.to_wire() for item in revision.managed_control_totals or ()],
            expected_sha256=None if legacy else revision.revision_content_sha256,
            finality=revision.finality,
            period_start=period_start,
            source_timezone=source_timezone,
            binding_id=binding_id,
        )
    except ReportingRowStoreError as error:
        if error.code != "INVALID_INPUT":
            raise
        if isinstance(error, ReportingRowBindingMismatchError):
            raise LedgerConflictError(
                "REVISION_CONTENT_MISMATCH",
                "revision row content does not match its immutable binding",
            ) from None
        raise LedgerConflictError(
            "ROWS_NOT_PORTABLE", "revision rows cannot be canonicalized for storage"
        ) from None


async def stage_revision_rows(
    store: PgReportingLedgerStore,
    revision: ReportingRevisionRecord,
    rows: Sequence[dict[str, Any]],
) -> StagedRevisionRows | None:
    """Prepare a revision's rows and upload them before the ledger commit.

    Returns ``None`` for a replay: a revision that is already committed needs no
    upload. Never call this inside a store transaction: it takes the account lock
    briefly on its own connection and uploads without holding it.
    """
    from adcp.reporting.ledger.pg import _BOUND_CONNECTION

    storage = store._row_storage
    if storage is None:
        return None
    if not storage.is_ready:
        raise ReportingRowStoreError(
            "STATE_UNAVAILABLE", "row storage is not ready; call ready_row_storage()"
        )
    async with store._connection() as connection:
        row = await (
            await connection.execute(
                "SELECT to_char(period_start AT TIME ZONE 'UTC',"
                ' \'YYYY-MM-DD"T"HH24:MI:SS"Z"\'), source_timezone FROM reporting_obligations'
                " WHERE reporting_obligation_id = %s AND account_id = %s",
                (revision.reporting_obligation_id, revision.account_id),
            )
        ).fetchone()
        committed = await (
            await connection.execute(
                "SELECT 1 FROM reporting_revisions WHERE reporting_revision_id = %s"
                " AND account_id = %s",
                (revision.reporting_revision_id, revision.account_id),
            )
        ).fetchone()
    if committed is not None:
        # A replay needs no upload, and nothing is staged for it.
        return None
    prepared = await prepare_revision_row_set(
        store,
        revision,
        rows,
        period_start=row[0] if row else None,
        source_timezone=row[1] if row else None,
    )
    staged = StagedRevisionRows(prepared)
    if not isinstance(prepared.binding, ReportingObjectRowBindingV1):
        return staged
    bound = _BOUND_CONNECTION.get()
    if bound is not None and bound[0] is store._pool:
        raise ReportingRowStoreError(
            "STATE_UNAVAILABLE",
            "stage revision rows before opening a ledger transaction; uploads never hold the"
            " account lock",
        )
    async with store._pool.connection() as connection:
        shared = await storage.find_shareable(connection, prepared)
    if shared is not None:
        staged.shared_from, staged.locators = shared[0], shared[1]
        return staged
    owner_token = str(uuid.uuid4())
    await _open_intent(store, prepared, owner_token)
    staged.owner_token = owner_token

    async def record(object_ref: str, native_version_ref: str) -> None:
        async with store._pool.connection() as connection:
            cursor = await connection.execute(
                "UPDATE adcp_reporting_row_write_intents"
                " SET created_objects = created_objects || %s::jsonb"
                " WHERE row_binding_id = %s AND row_set_id = %s AND content_sha256 = %s"
                " AND owner_token = %s AND state = 'open'",
                (
                    json.dumps(
                        [{"object_ref": object_ref, "native_version_ref": native_version_ref}]
                    ),
                    prepared.binding_id,
                    prepared.row_set_id,
                    prepared.content_sha256,
                    owner_token,
                ),
            )
            if cursor.rowcount != 1:
                raise ReportingRowStoreError(
                    "STATE_UNAVAILABLE", "the row upload intent was taken over or swept"
                )

    staged.locators = await storage.upload(prepared, owner_token, record)
    return staged


async def _open_intent(
    store: PgReportingLedgerStore, prepared: ReportingPreparedRowSetV1, owner_token: str
) -> None:
    """Open or take over an expired write intent, under the lease-budget check."""
    storage = store._row_storage
    assert storage is not None
    budget = storage.write_deadline_milliseconds
    async with store._pool.connection() as connection, connection.transaction():
        await store._lock_account(connection, prepared.account_id)
        opened = await (
            await connection.execute(
                "INSERT INTO adcp_reporting_row_write_intents"
                " (row_binding_id, account_id, row_set_id, content_sha256, state, owner_token,"
                "  expires_at)"
                " SELECT %(binding)s, %(account)s, %(row_set)s, %(sha)s, 'open', %(owner)s,"
                "        c.lease_expires_at + (%(budget)s::bigint * INTERVAL '1 millisecond')"
                " FROM reporting_obligations o JOIN reporting_configurations c"
                "   ON c.account_id = o.account_id AND c.consumer_id = o.consumer_id"
                "  AND c.delivery_config_id = o.delivery_config_id"
                "  AND c.delivery_config_version = o.delivery_config_version"
                " WHERE o.reporting_obligation_id = %(obligation)s AND o.account_id = %(account)s"
                "   AND c.lease_expires_at - clock_timestamp()"
                "       >= ((%(budget)s::bigint + %(margin)s) * INTERVAL '1 millisecond')"
                " ON CONFLICT (row_binding_id, row_set_id, content_sha256) DO UPDATE"
                "   SET owner_token = EXCLUDED.owner_token, expires_at = EXCLUDED.expires_at"
                " WHERE adcp_reporting_row_write_intents.state = 'open'"
                "   AND adcp_reporting_row_write_intents.expires_at <= clock_timestamp()"
                " RETURNING owner_token",
                {
                    "binding": prepared.binding_id,
                    "account": prepared.account_id,
                    "row_set": prepared.row_set_id,
                    "sha": prepared.content_sha256,
                    "owner": owner_token,
                    "budget": budget,
                    "margin": _LEASE_MARGIN_MILLISECONDS,
                    "obligation": prepared.obligation_id,
                },
            )
        ).fetchone()
        if opened is not None:
            return
        held = await (
            await connection.execute(
                "SELECT c.lease_expires_at - clock_timestamp()"
                "       >= ((%(budget)s::bigint + %(margin)s) * INTERVAL '1 millisecond')"
                " FROM reporting_obligations o JOIN reporting_configurations c"
                "   ON c.account_id = o.account_id AND c.consumer_id = o.consumer_id"
                "  AND c.delivery_config_id = o.delivery_config_id"
                "  AND c.delivery_config_version = o.delivery_config_version"
                " WHERE o.reporting_obligation_id = %(obligation)s AND o.account_id = %(account)s"
                "   AND c.lease_expires_at > clock_timestamp()",
                {
                    "budget": budget,
                    "margin": _LEASE_MARGIN_MILLISECONDS,
                    "obligation": prepared.obligation_id,
                    "account": prepared.account_id,
                },
            )
        ).fetchone()
    if held is None:
        raise ReportingRowStoreError(
            "STATE_UNAVAILABLE", "no live worker lease covers this row upload"
        )
    if not held[0]:
        raise ReportingRowStoreError(
            "DEADLINE_EXCEEDED", "the lease does not cover the row upload deadline"
        )
    raise ReportingRowStoreError("STATE_UNAVAILABLE", "another writer holds this row upload")


async def persist_revision_row_set(
    store: PgReportingLedgerStore,
    connection: Any,
    revision: ReportingRevisionRecord,
    rows: Sequence[dict[str, Any]],
    staged: StagedRevisionRows | None,
) -> None:
    """Write the row set inside the ledger transaction, closing its upload intent."""
    storage = store._row_storage
    assert storage is not None
    prepared = staged.prepared if staged is not None else None
    if prepared is None:
        prepared = await prepare_revision_row_set(store, revision, rows)
    if not isinstance(prepared.binding, ReportingObjectRowBindingV1):
        shared = await storage.find_shareable(connection, prepared)
        await storage.write_in_transaction(
            connection, prepared, None, shared[0] if shared is not None else None
        )
        return
    if staged is None or staged.locators is None:
        raise ReportingRowStoreError(
            "STATE_UNAVAILABLE",
            "object rows were not uploaded; stage them before the ledger transaction",
        )
    if staged.shared_from is not None:
        # The shared row set must still be live when this commit lands. FOR SHARE
        # keeps retention from retiring it before the transaction ends.
        live = await (
            await connection.execute(
                "SELECT 1 FROM adcp_reporting_row_sets WHERE row_set_id = %s"
                " AND rows_state = 'live' FOR SHARE",
                (staged.shared_from,),
            )
        ).fetchone()
        if live is None:
            raise ReportingRowStoreError(
                "STATE_UNAVAILABLE", "the shared row set is no longer live"
            )
    else:
        closed = await connection.execute(
            "DELETE FROM adcp_reporting_row_write_intents"
            " WHERE row_binding_id = %s AND row_set_id = %s AND content_sha256 = %s"
            " AND owner_token = %s AND state = 'open'",
            (prepared.binding_id, prepared.row_set_id, prepared.content_sha256, staged.owner_token),
        )
        if closed.rowcount != 1:
            raise ReportingRowStoreError(
                "STATE_UNAVAILABLE", "the row upload intent was taken over or swept"
            )
    if staged.shared_from is None:
        await storage.lock_and_verify_adopted(connection, prepared.binding_id, staged.locators)
    await storage.write_in_transaction(connection, prepared, staged.locators, staged.shared_from)
