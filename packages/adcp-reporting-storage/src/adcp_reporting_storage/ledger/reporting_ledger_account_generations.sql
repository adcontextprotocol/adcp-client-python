-- Verify the owned generation key on an empty/new SDK schema. This file no
-- longer upgrades retained legacy rows in place: use the stopped-worker
-- maintenance archive and authoritative backfill APIs in reporting.migration.
-- The check is atomic and refuses unexpected keys without rewriting evidence.

DO $account_generations$
DECLARE
    primary_key_columns TEXT[];
BEGIN
    PERFORM pg_advisory_xact_lock(hashtext('adcp.reporting.schema'), hashtext(current_schema()));
    -- Lock before inspecting the key, so concurrent migrations cannot both
    -- decide to replace it. This also excludes configuration writes/leases
    -- while the unique index is rebuilt. Other ledger rows are untouched.
    LOCK TABLE reporting_configurations IN ACCESS EXCLUSIVE MODE;

    SELECT ARRAY(
        SELECT attribute.attname::TEXT
        FROM unnest(pk.conkey) WITH ORDINALITY AS key_column(attnum, position)
        JOIN pg_attribute attribute
          ON attribute.attrelid = pk.conrelid AND attribute.attnum = key_column.attnum
        ORDER BY key_column.position
    )
    INTO primary_key_columns
    FROM pg_constraint pk
    WHERE pk.conrelid = 'reporting_configurations'::regclass AND pk.contype = 'p';

    IF primary_key_columns = ARRAY[
        'account_id', 'consumer_id', 'delivery_config_id', 'delivery_config_version'
    ] THEN
        RETURN;
    END IF;
    -- adcp-reporting-storage 0.2+ widens the key with offering_id (storage
    -- contract 2). This release cannot run on it, and the caller-ownership
    -- remedy below would be wrong advice.
    IF 'offering_id' = ANY(primary_key_columns) THEN
        RAISE EXCEPTION 'adcp-reporting-storage 0.2 or later has upgraded this reporting ledger to storage contract 2; this release implements storage contract 1'
            USING HINT = 'Run adcp>=10.0.0b7 with adcp-reporting-storage>=0.2.0b1 on every reporting process. Downgrading is unsupported: restore the backup taken before the upgrade. Do not run migrate_legacy_reporting.';
    END IF;
    RAISE EXCEPTION 'Reporting caller ownership requires a stopped-worker maintenance migration'
        USING HINT = 'Use migrate_legacy_reporting; retain legacy state in quarantine and backfill authoritative ownership.';

END
$account_generations$;
