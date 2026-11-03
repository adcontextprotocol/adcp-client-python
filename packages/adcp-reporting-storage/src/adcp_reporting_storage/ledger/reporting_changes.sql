-- AdCP Reliable Reporting host change feed: the shared change table, its pruning
-- horizons and registered feed consumers.
--
-- These are the shared SDK persistence tables (adcontextprotocol/adcp#7996, sections
-- 2.1 and 3.7). The JavaScript SDK ships the same tables and columns
-- (REPORTING_LEDGER_CHANGES_MIGRATION), so a host reads one feed regardless of which SDK
-- wrote a record. Apply after reporting_ledger.sql; PostgreSQL 13 or later (xid8).
-- PgReportingLedgerStore(change_feed=True) applies it from create_schema.
--
-- A row is written in the same transaction as each obligation, revision, adjustment
-- and retirement. Every reader, account-scoped or deployment-wide, orders by (xid, seq)
-- and stops below the reading snapshot's xmin, so a slow commit is never skipped and no
-- feed depends on the account lock or on the order a writer drew seq in. Account cursors
-- carry xid; a seq-only cursor from before that is mapped to a position that skips
-- nothing (adcp-client#3289, adcp-client-python#1825).
--
-- Re-applying this file upgrades an install from before the account feed ordered by
-- (xid, seq): it adds the account horizon's xid columns, replaces the consumer table's
-- check constraint and creates the (account_id, xid, seq) index. On a large changes table
-- create that index first with CREATE INDEX CONCURRENTLY; this file then skips it.
--
-- This table is separate from reporting_ledger_changes, the buyer-facing feed behind
-- the protocol's changes_checkpoint and changes_after. They converge with the shared
-- Core layout (persistence Stage 3).

SELECT pg_advisory_xact_lock(hashtext('adcp.reporting.schema'), hashtext(current_schema()));

-- Backs list_current_revisions: an account's obligations in (period_start, id) order. Kept out of
-- the base ledger DDL, whose parent catalog objects are pinned; extra indexes are ignored by the
-- catalog checks.
CREATE INDEX IF NOT EXISTS reporting_obligations_account_period_idx
  ON reporting_obligations (account_id, period_start, reporting_obligation_id);

CREATE TABLE IF NOT EXISTS adcp_reporting_changes (
  seq BIGSERIAL PRIMARY KEY,
  xid xid8 NOT NULL DEFAULT pg_current_xact_id(),
  account_id TEXT NOT NULL,
  record_kind TEXT NOT NULL
    CHECK (record_kind IN ('obligation', 'revision', 'adjustment', 'retirement')),
  record_id TEXT NOT NULL,
  obligation_id TEXT NOT NULL,
  recorded_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
);
-- Superseded by adcp_reporting_changes_account_xid for the account feed; kept so
-- re-applying this file never drops an index, and because it serves the mapping of a
-- seq-only account cursor. Drop it once no older host runs and no such cursor remains.
CREATE INDEX IF NOT EXISTS adcp_reporting_changes_account
  ON adcp_reporting_changes (account_id, seq);
CREATE INDEX IF NOT EXISTS adcp_reporting_changes_account_xid
  ON adcp_reporting_changes (account_id, xid, seq);
CREATE INDEX IF NOT EXISTS adcp_reporting_changes_deployment
  ON adcp_reporting_changes (xid, seq);
CREATE INDEX IF NOT EXISTS adcp_reporting_changes_recorded
  ON adcp_reporting_changes (recorded_at, seq);

-- Highest (xid, seq) ever pruned: a deployment-wide cursor below it may have
-- missed a deleted change and fails closed.
CREATE TABLE IF NOT EXISTS adcp_reporting_change_horizon (
  singleton BOOLEAN PRIMARY KEY DEFAULT TRUE CHECK (singleton),
  pruned_through_xid xid8 NOT NULL DEFAULT '0',
  pruned_through_seq BIGINT NOT NULL DEFAULT 0
);
INSERT INTO adcp_reporting_change_horizon (singleton) VALUES (TRUE) ON CONFLICT DO NOTHING;

-- Highest (xid, seq) ever pruned per account: an account cursor below it fails closed.
-- pruned_through_seq is the highest seq pruned whatever its xid; account feeds no longer
-- compare it, it exists so a seq-only cursor from before the (xid, seq) order can still be
-- checked exactly (a tuple compare alone would miss a pruned change with a higher seq and
-- a lower xid).
CREATE TABLE IF NOT EXISTS adcp_reporting_change_account_horizon (
  account_id TEXT PRIMARY KEY,
  pruned_through_seq BIGINT NOT NULL,
  pruned_through_xid xid8 NOT NULL DEFAULT '0',
  pruned_through_xid_seq BIGINT NOT NULL DEFAULT 0
);
ALTER TABLE adcp_reporting_change_account_horizon
  ADD COLUMN IF NOT EXISTS pruned_through_xid xid8 NOT NULL DEFAULT '0';
ALTER TABLE adcp_reporting_change_account_horizon
  ADD COLUMN IF NOT EXISTS pruned_through_xid_seq BIGINT NOT NULL DEFAULT 0;

-- Registered consumers hold back pruning in (xid, seq) order, per account for an account
-- consumer. A row saved before the account feed carried an xid has a NULL cursor_xid and holds
-- by seq until the consumer saves its next cursor.
CREATE TABLE IF NOT EXISTS adcp_reporting_feed_consumers (
  consumer_name TEXT PRIMARY KEY CHECK (consumer_name ~ '^[A-Za-z0-9_.:-]{1,128}$'),
  account_id TEXT,
  cursor_xid xid8,
  cursor_seq BIGINT NOT NULL,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
  CONSTRAINT adcp_reporting_feed_consumers_cursor_scope
    CHECK (account_id IS NOT NULL OR cursor_xid IS NOT NULL)
);
-- An install from before the account feed carried an xid has the anonymous check
-- (account_id IS NULL) = (cursor_xid IS NOT NULL), which rejects an account consumer that has
-- an xid. Drop it and add the named one; both steps are guarded, so re-applying is a no-op.
DO $adcp_feed_consumers$
DECLARE stale TEXT;
BEGIN
  FOR stale IN
    SELECT conname FROM pg_constraint
     WHERE conrelid = 'adcp_reporting_feed_consumers'::regclass AND contype = 'c'
       AND conname <> 'adcp_reporting_feed_consumers_cursor_scope'
       AND strpos(pg_get_constraintdef(oid), 'account_id IS NULL') > 0
  LOOP
    EXECUTE format('ALTER TABLE adcp_reporting_feed_consumers DROP CONSTRAINT %I', stale);
  END LOOP;
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint
     WHERE conrelid = 'adcp_reporting_feed_consumers'::regclass
       AND conname = 'adcp_reporting_feed_consumers_cursor_scope'
  ) THEN
    ALTER TABLE adcp_reporting_feed_consumers
      ADD CONSTRAINT adcp_reporting_feed_consumers_cursor_scope
      CHECK (account_id IS NOT NULL OR cursor_xid IS NOT NULL);
  END IF;
END
$adcp_feed_consumers$;
