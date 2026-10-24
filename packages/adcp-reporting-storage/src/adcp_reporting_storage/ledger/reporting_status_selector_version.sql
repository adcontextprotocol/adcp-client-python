-- #1167B1: C checkpoint semantics only. No materializer persistence.
-- Drain old C projectors/sweepers before cutover. A/B/C source and outbox
-- writers do not update checkpoints and remain compatible with this guard.
DO $selector$
BEGIN
    PERFORM pg_advisory_xact_lock(hashtext('adcp.reporting.schema'), hashtext(current_schema()));
    ALTER TABLE reporting_status_scope_checkpoints
        ADD COLUMN IF NOT EXISTS selector_semantics_version INTEGER NOT NULL DEFAULT 1;
    ALTER TABLE reporting_status_scope_checkpoints
        ADD COLUMN IF NOT EXISTS selector_writer_floor INTEGER NOT NULL DEFAULT 1;
    ALTER TABLE reporting_status_accounts
        ADD COLUMN IF NOT EXISTS selector_target_version INTEGER NOT NULL DEFAULT 1;
    ALTER TABLE reporting_status_accounts
        ADD COLUMN IF NOT EXISTS selector_transition TEXT NOT NULL DEFAULT 'pending';
    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                   WHERE conrelid = 'reporting_status_scope_checkpoints'::regclass
                     AND conname = 'reporting_status_selector_versions') THEN
        ALTER TABLE reporting_status_scope_checkpoints
            ADD CONSTRAINT reporting_status_selector_versions CHECK (
                selector_semantics_version IN (1,2) AND selector_writer_floor IN (1,2)
                AND selector_semantics_version <= selector_writer_floor);
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                   WHERE conrelid = 'reporting_status_accounts'::regclass
                     AND conname = 'reporting_status_selector_account_transition') THEN
        ALTER TABLE reporting_status_accounts
            ADD CONSTRAINT reporting_status_selector_account_transition CHECK (
                (selector_target_version = 1 AND selector_transition = 'pending') OR
                (selector_target_version = 2 AND selector_transition IN ('transitioning','complete')));
    END IF;
    CREATE INDEX IF NOT EXISTS reporting_status_selector_rebuild
        ON reporting_status_scope_checkpoints (account_id)
        WHERE selector_semantics_version <> 2 OR selector_writer_floor <> 2;
    CREATE INDEX IF NOT EXISTS reporting_status_selector_accounts
        ON reporting_status_accounts (account_id)
        WHERE baseline_complete AND (selector_target_version <> 2 OR selector_transition <> 'complete');
END
$selector$;

CREATE OR REPLACE FUNCTION reporting_status_selector_writer_guard_v2() RETURNS trigger
LANGUAGE plpgsql AS $guard$
BEGIN
    -- Check only this checkpoint. An account lookup here would invert the
    -- account -> checkpoint lock order against old row-lock-only due claims.
    IF (NEW.selector_writer_floor >= 2 OR
        (TG_OP = 'UPDATE' AND OLD.selector_writer_floor >= 2)) AND
       (current_setting('adcp.reporting.selector_semantics_version', true) IS DISTINCT FROM '2'
        OR NEW.selector_writer_floor < 2
        OR (TG_OP = 'UPDATE' AND NEW.selector_semantics_version < OLD.selector_semantics_version)) THEN
        RAISE EXCEPTION 'reporting_status_selector_writer_drain_required';
    END IF;
    RETURN NEW;
END
$guard$;

DO $selector_guard$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_trigger
                   WHERE tgrelid = 'reporting_status_scope_checkpoints'::regclass
                     AND tgname = 'reporting_status_selector_writer_v2') THEN
        CREATE TRIGGER reporting_status_selector_writer_v2
            BEFORE INSERT OR UPDATE ON reporting_status_scope_checkpoints
            FOR EACH ROW EXECUTE FUNCTION reporting_status_selector_writer_guard_v2();
    END IF;
END
$selector_guard$;
