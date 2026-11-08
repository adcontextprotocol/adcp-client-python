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

-- An account horizon whose xid columns a release that does not maintain them has pruned past.
-- adcp-reporting-storage 0.1.0b5 orders the account feed by (xid, seq), hands out account
-- cursors that carry xid and records the account horizon in (xid, seq). 0.2.0b0 to 0.2.0b2 do
-- not: their prune advances pruned_through_seq only, so on a ledger 0.1.0b5 wrote, a cursor
-- 0.1.0b5 handed out could pass the stale (xid, seq) check and silently skip what 0.2.x
-- pruned. Pruned xids are gone, so each account horizon is raised to a position at or above
-- every change that can have been pruned: the deployment horizon, which every release keeps in
-- (xid, seq), or, when lower, the position just below the account's oldest retained change, so
-- a consumer starting over still reads every retained change. The affected cursors then fail
-- closed with CURSOR_EXPIRED. A cursor behind that position whose changes were all retained
-- expires too, once.
--
-- It runs on the first create_schema of this release on a ledger that already had the account
-- xid columns (0.1.0b5 ran on it) and that a migrations-aware 0.2 release upgraded in an
-- earlier transaction (0001 is recorded before this one began). A ledger upgraded straight from
-- 0.1.0b5, an install from before the (xid, seq) account feed (whose account cursors carry no
-- xid) and a new install change nothing. The table comment set below records that this
-- release has applied the file, so the step never runs again. Stop every older host's change
-- maintenance before this release starts, as for the (xid, seq) upgrade itself.
DO $adcp_account_horizon$
DECLARE
  account_horizon regclass :=
    to_regclass(quote_ident(current_schema()) || '.adcp_reporting_change_account_horizon');
  migrations regclass :=
    to_regclass(quote_ident(current_schema()) || '.adcp_schema_migrations');
  upgraded_earlier BOOLEAN := FALSE;
  deployment_xid xid8;
  deployment_seq BIGINT;
  account RECORD;
  bound_xid xid8;
  bound_seq BIGINT;
  oldest_xid xid8;
  oldest_seq BIGINT;
BEGIN
  IF account_horizon IS NULL
     OR obj_description(account_horizon, 'pg_class') IS NOT DISTINCT FROM
        'adcp: account horizon maintained in (xid, seq)'
     OR NOT EXISTS (
       SELECT 1 FROM pg_attribute
        WHERE attrelid = account_horizon AND attname = 'pruned_through_xid' AND NOT attisdropped
     )
     OR migrations IS NULL THEN
    RETURN;
  END IF;
  EXECUTE format(
    'SELECT EXISTS (SELECT 1 FROM %s WHERE version = %L AND applied_at < now())',
    migrations, '0001'
  ) INTO upgraded_earlier;
  IF NOT upgraded_earlier THEN
    RETURN;
  END IF;
  SELECT pruned_through_xid, pruned_through_seq INTO deployment_xid, deployment_seq
    FROM adcp_reporting_change_horizon WHERE singleton;
  IF NOT FOUND THEN
    RETURN;
  END IF;
  FOR account IN
    SELECT account_id, pruned_through_xid, pruned_through_xid_seq
      FROM adcp_reporting_change_account_horizon
  LOOP
    bound_xid := deployment_xid;
    bound_seq := deployment_seq;
    SELECT change.xid, change.seq - 1 INTO oldest_xid, oldest_seq
      FROM adcp_reporting_changes change
     WHERE change.account_id = account.account_id
     ORDER BY change.xid, change.seq LIMIT 1;
    IF FOUND AND (oldest_xid, oldest_seq) < (bound_xid, bound_seq) THEN
      bound_xid := oldest_xid;
      bound_seq := oldest_seq;
    END IF;
    IF (bound_xid, bound_seq) > (account.pruned_through_xid, account.pruned_through_xid_seq) THEN
      UPDATE adcp_reporting_change_account_horizon
         SET pruned_through_xid = bound_xid, pruned_through_xid_seq = bound_seq
       WHERE account_id = account.account_id;
    END IF;
  END LOOP;
END
$adcp_account_horizon$;

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
COMMENT ON TABLE adcp_reporting_change_account_horizon IS
  'adcp: account horizon maintained in (xid, seq)';

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
