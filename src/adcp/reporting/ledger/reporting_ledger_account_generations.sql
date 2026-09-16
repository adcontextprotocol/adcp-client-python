-- Upgrade the 8.0.0-beta.15 configuration identity without rewriting evidence.
-- May also run on the current schema. This single DO statement is atomic even
-- in psql autocommit mode; create_schema() runs it with the bootstrap in one
-- transaction. Stop all older reporting workers before applying the upgrade.
--
-- Only the known old primary key is replaced. Its name may have been changed
-- by an adopter. No CASCADE: unexpected keys or dependent foreign keys fail
-- and roll back, leaving the original schema and rows intact for inspection.
-- The SDK's beta.15 schema has no foreign keys to reporting_configurations.

DO $account_generations$
DECLARE
    primary_key_name TEXT;
    primary_key_columns TEXT[];
BEGIN
    PERFORM pg_advisory_xact_lock(hashtext('adcp.reporting.schema'), hashtext(current_schema()));
    -- Lock before inspecting the key, so concurrent migrations cannot both
    -- decide to replace it. This also excludes configuration writes/leases
    -- while the unique index is rebuilt. Other ledger rows are untouched.
    LOCK TABLE reporting_configurations IN ACCESS EXCLUSIVE MODE;

    SELECT pk.conname, ARRAY(
        SELECT attribute.attname::TEXT
        FROM unnest(pk.conkey) WITH ORDINALITY AS key_column(attnum, position)
        JOIN pg_attribute attribute
          ON attribute.attrelid = pk.conrelid AND attribute.attnum = key_column.attnum
        ORDER BY key_column.position
    )
    INTO primary_key_name, primary_key_columns
    FROM pg_constraint pk
    WHERE pk.conrelid = 'reporting_configurations'::regclass AND pk.contype = 'p';

    IF primary_key_columns = ARRAY[
        'account_id', 'delivery_config_id', 'delivery_config_version'
    ] THEN
        RETURN;
    END IF;

    IF primary_key_columns IS DISTINCT FROM ARRAY[
        'delivery_config_id', 'delivery_config_version'
    ] THEN
        RAISE EXCEPTION 'Unexpected reporting_configurations primary key: %', primary_key_columns
            USING HINT = 'Expected the beta.15 or account-qualified key; inspect the schema before migrating.';
    END IF;

    EXECUTE format(
        'ALTER TABLE reporting_configurations DROP CONSTRAINT %I, '
        'ADD CONSTRAINT %I PRIMARY KEY (account_id, delivery_config_id, delivery_config_version)',
        primary_key_name, primary_key_name
    );
END
$account_generations$;
