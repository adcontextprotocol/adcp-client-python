-- Verify the owned generation key on an empty/new SDK schema. This file no
-- longer upgrades retained legacy rows in place: use the stopped-worker
-- maintenance archive and authoritative backfill APIs in reporting.migration.
-- The check is atomic and refuses unexpected keys without rewriting evidence.
--
-- AdCP 3.3 reporting subscriptions keep one generation per resolved offering
-- of a subscription version, so the owned key is (account_id, consumer_id,
-- delivery_config_id, delivery_config_version, offering_id), with
-- offering_id '' for every account-level generation. An owned schema still on
-- the four-column key is widened here, losslessly and under the schema lock:
-- existing rows keep offering_id '' and their uniqueness. This is a
-- maintenance upgrade: stop every reporting worker first, because workers of
-- earlier releases refuse the widened schema. A foreign key onto the key that
-- the SDK does not own refuses the upgrade instead of being dropped.

DO $account_generations$
DECLARE
    primary_key_name TEXT;
    primary_key_columns TEXT[];
    reference RECORD;
BEGIN
    PERFORM pg_advisory_xact_lock(hashtext('adcp.reporting.schema'), hashtext(current_schema()));
    -- Lock before inspecting the key, so concurrent migrations cannot both
    -- decide to replace it. This also excludes configuration writes/leases
    -- while the unique index is rebuilt. Other ledger rows are untouched.
    LOCK TABLE reporting_configurations IN ACCESS EXCLUSIVE MODE;

    SELECT pk.conname::TEXT, ARRAY(
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
        'account_id', 'consumer_id', 'delivery_config_id', 'delivery_config_version', 'offering_id'
    ] THEN
        RETURN;
    END IF;
    IF primary_key_columns IS DISTINCT FROM ARRAY[
        'account_id', 'consumer_id', 'delivery_config_id', 'delivery_config_version'
    ] THEN
        RAISE EXCEPTION 'Reporting caller ownership requires a stopped-worker maintenance migration'
            USING HINT = 'Use migrate_legacy_reporting; retain legacy state in quarantine and backfill authoritative ownership.';
    END IF;

    -- Only the SDK's own references are re-pointed at the widened key.
    FOR reference IN
        SELECT referencing.relname::TEXT AS table_name
        FROM pg_constraint fk
        JOIN pg_class referencing ON referencing.oid = fk.conrelid
        WHERE fk.contype = 'f' AND fk.confrelid = 'reporting_configurations'::regclass
    LOOP
        IF reference.table_name NOT IN (
            'reporting_reconciliation_records',
            'reporting_materializer_discovery',
            'reporting_status_scope_checkpoints',
            'reporting_production_generations'
        ) THEN
            RAISE EXCEPTION 'Widening the reporting generation key requires a stopped-worker maintenance migration'
                USING HINT = 'A foreign key outside the SDK references reporting_configurations; re-point it at the widened key first.';
        END IF;
    END LOOP;
    LOCK TABLE reporting_obligations, reporting_consumer_statuses IN ACCESS EXCLUSIVE MODE;
    FOR reference IN
        SELECT fk.conrelid::regclass AS table_name, fk.conname::TEXT AS constraint_name
        FROM pg_constraint fk
        WHERE fk.contype = 'f' AND fk.confrelid = 'reporting_configurations'::regclass
    LOOP
        EXECUTE format(
            'ALTER TABLE %s DROP CONSTRAINT %I', reference.table_name, reference.constraint_name
        );
    END LOOP;

    ALTER TABLE reporting_configurations ADD COLUMN offering_id TEXT COLLATE "C" NOT NULL DEFAULT '';
    EXECUTE format('ALTER TABLE reporting_configurations DROP CONSTRAINT %I', primary_key_name);
    ALTER TABLE reporting_configurations ADD CONSTRAINT reporting_configurations_pkey
        PRIMARY KEY (account_id, consumer_id, delivery_config_id, delivery_config_version, offering_id);

    ALTER TABLE reporting_obligations ADD COLUMN offering_id TEXT COLLATE "C" NOT NULL DEFAULT '';
    DROP INDEX reporting_obligations_period_key;
    CREATE UNIQUE INDEX reporting_obligations_period_key
        ON reporting_obligations
           (account_id, consumer_id, delivery_config_id, delivery_config_version, offering_id,
            period_start, period_end);

    ALTER TABLE reporting_consumer_statuses
        ADD COLUMN offering_id TEXT COLLATE "C" NOT NULL DEFAULT '';

    -- These tables serve account-level generations only; a subscription
    -- generation is refused by their CHECK until each feature serves it.
    IF to_regclass('reporting_reconciliation_records') IS NOT NULL THEN
        ALTER TABLE reporting_reconciliation_records
            ADD COLUMN offering_id TEXT COLLATE "C" NOT NULL DEFAULT ''
                CONSTRAINT reporting_reconciliation_records_account_generation
                CHECK (offering_id = ''),
            ADD CONSTRAINT reporting_reconciliation_records_generation_fkey
                FOREIGN KEY (account_id, consumer_id, delivery_config_id, delivery_config_version,
                             offering_id)
                REFERENCES reporting_configurations(account_id, consumer_id, delivery_config_id,
                                                    delivery_config_version, offering_id);
    END IF;
    IF to_regclass('reporting_materializer_discovery') IS NOT NULL THEN
        ALTER TABLE reporting_materializer_discovery
            ADD COLUMN offering_id TEXT COLLATE "C" NOT NULL DEFAULT ''
                CONSTRAINT reporting_materializer_discovery_account_generation
                CHECK (offering_id = ''),
            ADD CONSTRAINT reporting_materializer_discovery_generation_fkey
                FOREIGN KEY (account_id, consumer_id, delivery_config_id, delivery_config_version,
                             offering_id)
                REFERENCES reporting_configurations(account_id, consumer_id, delivery_config_id,
                                                    delivery_config_version, offering_id);
    END IF;
    IF to_regclass('reporting_status_scope_checkpoints') IS NOT NULL THEN
        ALTER TABLE reporting_status_scope_checkpoints
            ADD COLUMN offering_id TEXT COLLATE "C" NOT NULL DEFAULT ''
                CONSTRAINT reporting_status_scope_checkpoints_account_generation
                CHECK (offering_id = ''),
            ADD CONSTRAINT reporting_status_scope_checkpoints_generation_fkey
                FOREIGN KEY (account_id, consumer_namespace, delivery_config_id, version,
                             offering_id)
                REFERENCES reporting_configurations(account_id, consumer_id, delivery_config_id,
                                                    delivery_config_version, offering_id);
    END IF;
    IF to_regclass('reporting_production_generations') IS NOT NULL THEN
        ALTER TABLE reporting_production_generations
            ADD COLUMN offering_id TEXT COLLATE "C" NOT NULL DEFAULT ''
                CONSTRAINT reporting_production_generations_account_generation
                CHECK (offering_id = ''),
            ADD CONSTRAINT reporting_production_generations_generation_fkey
                FOREIGN KEY (account_id, consumer_id, delivery_config_id, delivery_config_version,
                             offering_id)
                REFERENCES reporting_configurations(account_id, consumer_id, delivery_config_id,
                                                    delivery_config_version, offering_id);
    END IF;
END
$account_generations$;

-- A reporting subscription's owed-set completeness horizon, per generation:
-- obligations are written only for periods ending by it. Private SDK state
-- (the adcp_ prefix keeps it outside the reporting_* catalog contract), with
-- no foreign key, like the lease turns.
CREATE TABLE IF NOT EXISTS adcp_reporting_owed_set_progress (
    account_id              TEXT COLLATE "C" NOT NULL,
    consumer_id             TEXT COLLATE "C" NOT NULL,
    delivery_config_id      TEXT COLLATE "C" NOT NULL,
    delivery_config_version INTEGER          NOT NULL,
    offering_id             TEXT COLLATE "C" NOT NULL CHECK (offering_id <> ''),
    owed_through            TIMESTAMPTZ      NOT NULL,
    PRIMARY KEY (account_id, consumer_id, delivery_config_id, delivery_config_version, offering_id)
);
