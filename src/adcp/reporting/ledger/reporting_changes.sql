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
-- and retirement, taking nextval only after the account lock, so per-account sequence
-- order is commit order. Deployment-wide readers order by (xid, seq) and stop below
-- the reading snapshot's xmin, so a slow commit is never skipped.
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
CREATE INDEX IF NOT EXISTS adcp_reporting_changes_account
  ON adcp_reporting_changes (account_id, seq);
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

-- Highest seq ever pruned per account: an account cursor below it fails closed.
CREATE TABLE IF NOT EXISTS adcp_reporting_change_account_horizon (
  account_id TEXT PRIMARY KEY,
  pruned_through_seq BIGINT NOT NULL
);

-- Registered consumers hold back pruning in their own feed order: account
-- consumers by seq within their account, deployment consumers by (xid, seq).
CREATE TABLE IF NOT EXISTS adcp_reporting_feed_consumers (
  consumer_name TEXT PRIMARY KEY CHECK (consumer_name ~ '^[A-Za-z0-9_.:-]{1,128}$'),
  account_id TEXT,
  cursor_xid xid8,
  cursor_seq BIGINT NOT NULL,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
  CHECK ((account_id IS NULL) = (cursor_xid IS NOT NULL))
);
