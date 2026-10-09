# Reporting row storage

By default `PgReportingLedgerStore` keeps every revision row in `reporting_revision_rows`, one JSONB value
per row. At pulse cadence that keeps bulk report bytes in the transactional database, and a JSONB value does
not preserve the canonical bytes a digest was computed over.

Row storage is the shared SDK persistence layout from
[adcontextprotocol/adcp#7996](https://github.com/adcontextprotocol/adcp/pull/7996), tracked here in
[#1457](https://github.com/adcontextprotocol/adcp-client-python/issues/1457). The JavaScript SDK reads and
writes the same tables and the same bytes, so chunks written by either SDK verify in the other.

## Enable it

```python
from adcp.reporting.ledger import PgReportingLedgerStore, ReportingRowStorageOptionsV1

store = PgReportingLedgerStore(
    pool=pool,
    row_storage=ReportingRowStorageOptionsV1(deployment_namespace="prod-us"),  # or row_storage=True
)
await store.create_schema()  # applies reporting_row_storage.sql and registers the binding
```

If you manage DDL outside the SDK, apply `adcp/reporting/ledger/reporting_row_storage.sql` after the ledger
migration (PostgreSQL 13 or later) and call `await store.ready_row_storage()` on boot. `ready_row_storage` checks
the installed objects against `required_row_storage_schema.json` and refuses drift with
`STATE_UNAVAILABLE`.

Two SDKs sharing one database must use the same `deployment_namespace`; a binding registered under another
namespace is refused with `UNSAFE_BINDING`.

## What changes

- New revisions store their rows as canonical JSONL chunks: `JCS(row)` followed by `\n`, in 500-row segments
  and chunks of at most 10,000 rows or 8 MiB. Chunk bodies are `BYTEA` in `adcp_reporting_chunk_bodies`,
  content-addressed per obligation, and the database checks each body's digest.
- `read_revision_rows` verifies every segment it releases against digests committed in PostgreSQL, and verifies
  the chunk manifest against the row set's committed `row_manifest_sha256`. It reads only the segments a page
  needs. A missing body is `ROWS_UNAVAILABLE`, a mismatch is `ROWS_INTEGRITY_FAILED`, and neither is ever
  reported as an empty page. Rows retired by retention read like an unknown revision.
- Reads work on both forms, with or without the write option: a revision with a row set reads from its chunks, and
  one without reads from the legacy table.
- Rows must stay inside the portable number domain: finite numbers and integers within +-(2^53 - 1). Put
  larger integers in strings. Reading accepts any RFC 8785 number.

## Layout notes

`adcp_reporting_row_sets` is the side table that records where a revision's rows live until the shared Core layout
(persistence Stage 3). It matches the JavaScript table column for column except that `obligation_id` has no
foreign key: Python obligations live in `reporting_obligations`, not in the JavaScript
`adcp_reporting_obligations` table. If the JavaScript migration created the table first, `ready_row_storage`
refuses with the constraint name; apply this migration first, or drop that constraint.

A revision written before protocol-shape control totals (see the release notes) has a binding over `{name, value}`
pairs. Its row set records the rows-only digest profile `rows_v1`, and its pair binding is verified against the
stored bytes on a full read.

## Migrating legacy inline rows

Revisions written before you enabled row storage keep their rows in `reporting_revision_rows` and keep reading
through it. To move them into verified chunks, run the resumable migration in bounded batches. Each revision
migrates in its own transaction under the account lock, so a batch never holds the lock longer than one revision.

```python
cursor = None
while True:
    batch = await store.migrate_inline_rows(limit=100, cursor=cursor)
    if batch.quarantined:
        log.warning("unverifiable inline rows: %s", batch.quarantined)
    cursor = batch.cursor
    if cursor is None:
        break
```

A revision is migrated only if its inline rows, re-canonicalized, reproduce its committed
`revision_content_sha256` (a retained pair-hashed revision is proven under the algorithm it was written with), the
ordinals are contiguous and the row count matches. Anything else is **quarantined**: recorded in
`adcp_reporting_row_quarantine`, reported in `batch.quarantined`, never rewritten, and refused by every read with
`ROWS_INTEGRITY_FAILED`. Investigate it as damaged evidence; the inline rows are left exactly as found.
Inline rows are not deleted by migration.

Once a pass finishes with no cursor, stop every writer that predates chunked rows and retire the legacy table:

```python
await store.retire_legacy_inline_rows()  # ROWS_MIGRATION_INCOMPLETE while any revision is unaccounted for
```

This **renames** `reporting_revision_rows` to `adcp_reporting_legacy_revision_rows_v0`. It never truncates the
table, so a binary that predates chunked rows fails closed (its insert hits a missing table) instead of writing rows
nobody reads. `create_schema` does not recreate the retired table, and the schema checks for notifications and the
materializer accept the renamed table. The archive keeps every inline row, including quarantined revisions' rows;
drop it yourself once you no longer need it. Reclaiming the space of the original table needs the drop.

Upgrade note: the pre-ownership `migrate_legacy_reporting` and `backfill_legacy_reporting` operator APIs write to
`reporting_revision_rows`, so run them before retiring it.
