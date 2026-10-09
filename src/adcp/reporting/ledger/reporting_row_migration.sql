-- Quarantine for legacy inline rows that cannot be migrated to verified chunks.
--
-- Applied with reporting_row_storage.sql when row storage is enabled. A revision
-- listed here had rows in reporting_revision_rows that do not reproduce its
-- committed revision_content_sha256 (or cannot be canonicalized). It is never
-- rewritten and never served: reads fail with ROWS_INTEGRITY_FAILED, so an
-- operator investigates the evidence instead of a buyer receiving unproven rows.
--
-- Python only. The JavaScript SDK keeps unverifiable rows inline in the ledger
-- document and reports them in the migration batch; the shared layout (persistence
-- Stage 3) replaces both with a single row-set state.

SELECT pg_advisory_xact_lock(hashtext('adcp.reporting.schema'), hashtext(current_schema()));

CREATE TABLE IF NOT EXISTS adcp_reporting_row_quarantine (
    reporting_revision_id TEXT COLLATE "C" NOT NULL PRIMARY KEY,
    account_id            TEXT COLLATE "C" NOT NULL,
    reason                TEXT NOT NULL
        CHECK (reason IN ('BINDING_MISMATCH', 'ROWS_NOT_PORTABLE', 'ROW_SEQUENCE_INVALID')),
    detected_at           TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
);

CREATE OR REPLACE FUNCTION adcp_reporting_row_quarantine_immutable() RETURNS trigger
LANGUAGE plpgsql AS $quarantine$
BEGIN
    RAISE EXCEPTION 'reporting row quarantine records are immutable' USING ERRCODE = '23514';
END
$quarantine$;

DO $triggers$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_trigger
         WHERE tgname = 'adcp_reporting_row_quarantine_immutable'
           AND tgrelid = to_regclass(quote_ident(current_schema()) || '.adcp_reporting_row_quarantine')
    ) THEN
        CREATE TRIGGER adcp_reporting_row_quarantine_immutable
            BEFORE UPDATE ON adcp_reporting_row_quarantine
            FOR EACH ROW EXECUTE FUNCTION adcp_reporting_row_quarantine_immutable();
    END IF;
END
$triggers$;
