-- Verify the owned generation key on an empty/new SDK schema. This file no
-- longer upgrades retained legacy rows in place: use the stopped-worker
-- maintenance archive and authoritative backfill APIs in reporting.migration.
-- The check is atomic and refuses unexpected keys without rewriting evidence.
--
-- AdCP 3.3 reporting subscriptions keep one generation per resolved offering
-- of a subscription version, so the owned key is (account_id, consumer_id,
-- delivery_config_id, delivery_config_version, offering_id), with
-- offering_id '' for every account-level generation. reporting_ledger.sql
-- creates a new ledger on that key, so this file changes nothing there.
--
-- A ledger created by an earlier release (adcp-reporting-storage 0.1.x or
-- 0.2.0b0, storage contract 1) is upgraded by EXPANDING it, so processes of
-- that release keep booting, reading and writing account-level generations
-- beside this one through a rolling deploy, and a rollback keeps working:
--
-- * everything the earlier release needs keeps its name and definition: the
--   four-column reporting_configurations_pkey, the six-column
--   reporting_obligations_period_key and the four-column references onto the
--   configuration key;
-- * the widened objects take new names: reporting_configurations_generation_key
--   (UNIQUE on the five columns, the target of this release's ON CONFLICT and
--   of the offering-aware references) and the seven-column
--   reporting_obligations_offering_period_key;
-- * CHECK (offering_id = '') holds on the widened tables, because an earlier
--   release would read a subscription generation as an account-level one.
--
-- Subscription generations need the CONTRACT step (migration 0007,
-- python -m adcp migrate --contract), run once every earlier-release
-- reporting process has stopped. It drops the expand-only objects and leaves
-- exactly the catalog of a new ledger. Expanding is lossless, under the schema
-- lock: existing rows keep offering_id '' and their uniqueness. A foreign key
-- onto the key that the SDK does not own refuses the upgrade.

DO $account_generations$
DECLARE
    primary_key_columns TEXT[];
    reference RECORD;
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
        'account_id', 'consumer_id', 'delivery_config_id', 'delivery_config_version', 'offering_id'
    ] THEN
        RETURN;  -- contracted: a new ledger, or one already widened in place
    END IF;
    IF primary_key_columns IS DISTINCT FROM ARRAY[
        'account_id', 'consumer_id', 'delivery_config_id', 'delivery_config_version'
    ] THEN
        RAISE EXCEPTION 'Reporting caller ownership requires a stopped-worker maintenance migration'
            USING HINT = 'Use migrate_legacy_reporting; retain legacy state in quarantine and backfill authoritative ownership.';
    END IF;
    IF EXISTS (SELECT 1 FROM pg_constraint
        WHERE conrelid = 'reporting_configurations'::regclass
          AND conname = 'reporting_configurations_generation_key') THEN
        RETURN;  -- expanded and not yet contracted
    END IF;

    -- Only the SDK's own tables reference the key.
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

    ALTER TABLE reporting_configurations
        ADD COLUMN offering_id TEXT COLLATE "C" NOT NULL DEFAULT '',
        ADD CONSTRAINT reporting_configurations_generation_key
            UNIQUE (account_id, consumer_id, delivery_config_id, delivery_config_version, offering_id),
        ADD CONSTRAINT reporting_configurations_uncontracted CHECK (offering_id = '');

    ALTER TABLE reporting_obligations
        ADD COLUMN offering_id TEXT COLLATE "C" NOT NULL DEFAULT '',
        ADD CONSTRAINT reporting_obligations_uncontracted CHECK (offering_id = '');
    CREATE UNIQUE INDEX reporting_obligations_offering_period_key
        ON reporting_obligations
           (account_id, consumer_id, delivery_config_id, delivery_config_version, offering_id,
            period_start, period_end);

    ALTER TABLE reporting_consumer_statuses
        ADD COLUMN offering_id TEXT COLLATE "C" NOT NULL DEFAULT '',
        ADD CONSTRAINT reporting_consumer_statuses_uncontracted CHECK (offering_id = '');

    -- Managed delivery records subscription generations under their offering
    -- (reconciliation records, materializer discovery and status checkpoints).
    -- Production generations serve account-level generations only; a
    -- subscription generation is refused by their CHECK. Each table keeps its
    -- four-column reference beside the offering-aware one until contract.
    IF to_regclass(quote_ident(current_schema()) || '.reporting_reconciliation_records') IS NOT NULL THEN
        ALTER TABLE reporting_reconciliation_records
            ADD COLUMN offering_id TEXT COLLATE "C" NOT NULL DEFAULT '',
            ADD CONSTRAINT reporting_reconciliation_records_generation_fkey
                FOREIGN KEY (account_id, consumer_id, delivery_config_id, delivery_config_version,
                             offering_id)
                REFERENCES reporting_configurations(account_id, consumer_id, delivery_config_id,
                                                    delivery_config_version, offering_id);
    END IF;
    IF to_regclass(quote_ident(current_schema()) || '.reporting_materializer_discovery') IS NOT NULL THEN
        ALTER TABLE reporting_materializer_discovery
            ADD COLUMN offering_id TEXT COLLATE "C" NOT NULL DEFAULT '',
            ADD CONSTRAINT reporting_materializer_discovery_generation_fkey
                FOREIGN KEY (account_id, consumer_id, delivery_config_id, delivery_config_version,
                             offering_id)
                REFERENCES reporting_configurations(account_id, consumer_id, delivery_config_id,
                                                    delivery_config_version, offering_id);
    END IF;
    IF to_regclass(quote_ident(current_schema()) || '.reporting_status_scope_checkpoints') IS NOT NULL THEN
        ALTER TABLE reporting_status_scope_checkpoints
            ADD COLUMN offering_id TEXT COLLATE "C" NOT NULL DEFAULT '',
            ADD CONSTRAINT reporting_status_scope_checkpoints_generation_fkey
                FOREIGN KEY (account_id, consumer_namespace, delivery_config_id, version,
                             offering_id)
                REFERENCES reporting_configurations(account_id, consumer_id, delivery_config_id,
                                                    delivery_config_version, offering_id);
    END IF;
    IF to_regclass(quote_ident(current_schema()) || '.reporting_production_generations') IS NOT NULL THEN
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
