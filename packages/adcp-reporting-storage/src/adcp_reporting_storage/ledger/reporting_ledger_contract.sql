-- CONTRACT the generation keys of a ledger that
-- reporting_ledger_account_generations.sql and
-- reporting_status_checkpoint_offerings.sql EXPANDED.
--
-- An expanded ledger keeps everything adcp-reporting-storage 0.1.x/0.2.0b0
-- (storage contract 1) needs under its original names, so processes of that
-- release can keep running and restarting through a rolling deploy. This file
-- removes it, after which the ledger is exactly a new ledger's catalog:
--
-- * reporting_configurations: drops the four-column primary key, the
--   references onto it and CHECK (offering_id = ''); the five-column
--   reporting_configurations_generation_key becomes reporting_configurations_pkey
--   and the offering-aware references are re-created onto it, by name;
-- * reporting_obligations: drops the six-column period key and CHECK
--   (offering_id = ''); reporting_obligations_offering_period_key takes the name
--   reporting_obligations_period_key;
-- * reporting_consumer_statuses: drops CHECK (offering_id = '');
-- * status checkpoints (where installed): the seven-column
--   reporting_status_scope_checkpoints_offering_key becomes the primary key, each
--   status event table's widened reference and key take the names of the
--   six-column ones they replace.
--
-- This is a STOPPED-WORKER step: earlier-release processes cannot write the
-- contracted keys, and swapping primary keys takes exclusive locks. Apply it
-- with python -m adcp migrate --contract once every earlier-release reporting
-- process has stopped. It changes nothing on a contracted ledger. Migration
-- 0007 then re-runs reporting_ledger_reconciliation.sql and
-- reporting_status_checkpoint_offerings.sql, which restore this release's
-- function bodies.

DO $contract$
DECLARE
    reference RECORD;
    restored TEXT[] := ARRAY[]::TEXT[];
    canonical JSONB := '{}'::JSONB;
    item TEXT;
BEGIN
    PERFORM pg_advisory_xact_lock(hashtext('adcp.reporting.schema'), hashtext(current_schema()));

    -- (Nested tests: PostgreSQL resolves a ::regclass literal while planning.)
    IF to_regclass(quote_ident(current_schema()) || '.reporting_configurations') IS NULL THEN
        RETURN;
    END IF;
    IF EXISTS (SELECT 1 FROM pg_constraint
        WHERE conrelid = 'reporting_configurations'::regclass
          AND conname = 'reporting_configurations_generation_key') THEN
        LOCK TABLE reporting_configurations, reporting_obligations, reporting_consumer_statuses
            IN ACCESS EXCLUSIVE MODE;
        -- Every reference onto the configuration key goes: the four-column ones
        -- for good, the offering-aware ones to be re-created onto the new key.
        FOR reference IN
            SELECT fk.conrelid::regclass::TEXT AS table_name, fk.conname::TEXT AS constraint_name,
                   pg_get_constraintdef(fk.oid) AS definition, array_length(fk.conkey, 1) AS width
            FROM pg_constraint fk
            WHERE fk.contype = 'f' AND fk.confrelid = 'reporting_configurations'::regclass
            ORDER BY 1, 2
        LOOP
            IF reference.table_name NOT IN (
                'reporting_reconciliation_records',
                'reporting_materializer_discovery',
                'reporting_status_scope_checkpoints',
                'reporting_production_generations'
            ) THEN
                RAISE EXCEPTION 'Contracting the reporting generation key requires a stopped-worker maintenance migration'
                    USING HINT = 'A foreign key outside the SDK references reporting_configurations; drop it first.';
            END IF;
            EXECUTE format('LOCK TABLE %s IN ACCESS EXCLUSIVE MODE', reference.table_name);
            EXECUTE format('ALTER TABLE %s DROP CONSTRAINT %I',
                reference.table_name, reference.constraint_name);
            IF reference.width = 5 THEN
                restored := restored || format('ALTER TABLE %s ADD CONSTRAINT %I %s',
                    reference.table_name, reference.constraint_name, reference.definition);
            END IF;
        END LOOP;
        ALTER TABLE reporting_configurations
            DROP CONSTRAINT reporting_configurations_uncontracted,
            DROP CONSTRAINT reporting_configurations_pkey,
            DROP CONSTRAINT reporting_configurations_generation_key,
            ADD CONSTRAINT reporting_configurations_pkey PRIMARY KEY
                (account_id, consumer_id, delivery_config_id, delivery_config_version, offering_id);
        FOREACH item IN ARRAY restored LOOP
            EXECUTE item;
        END LOOP;

        ALTER TABLE reporting_obligations DROP CONSTRAINT reporting_obligations_uncontracted;
        DROP INDEX reporting_obligations_period_key;
        ALTER INDEX reporting_obligations_offering_period_key RENAME TO reporting_obligations_period_key;
        ALTER TABLE reporting_consumer_statuses
            DROP CONSTRAINT reporting_consumer_statuses_uncontracted;
    END IF;

    IF to_regclass(quote_ident(current_schema()) || '.reporting_status_scope_checkpoints') IS NULL THEN
        RETURN;
    END IF;
    IF EXISTS (SELECT 1 FROM pg_constraint
        WHERE conrelid = 'reporting_status_scope_checkpoints'::regclass
          AND conname = 'reporting_status_scope_checkpoints_offering_key') THEN
        LOCK TABLE reporting_status_scope_checkpoints IN ACCESS EXCLUSIVE MODE;
        restored := ARRAY[]::TEXT[];
        -- A table's six-column reference names the widened one that replaces it.
        FOR reference IN
            SELECT fk.conrelid::regclass::TEXT AS table_name, fk.conname::TEXT AS constraint_name
            FROM pg_constraint fk
            WHERE fk.contype = 'f' AND fk.confrelid = 'reporting_status_scope_checkpoints'::regclass
              AND array_length(fk.conkey, 1) = 6
        LOOP
            canonical := canonical || jsonb_build_object(reference.table_name, reference.constraint_name);
        END LOOP;
        FOR reference IN
            SELECT fk.conrelid::regclass::TEXT AS table_name, fk.conname::TEXT AS constraint_name,
                   pg_get_constraintdef(fk.oid) AS definition, array_length(fk.conkey, 1) AS width
            FROM pg_constraint fk
            WHERE fk.contype = 'f' AND fk.confrelid = 'reporting_status_scope_checkpoints'::regclass
            ORDER BY 1, 2
        LOOP
            IF reference.table_name NOT IN (
                'reporting_status_notification_events',
                'reporting_projection_notification_events'
            ) THEN
                RAISE EXCEPTION 'Contracting the status checkpoint key requires a stopped-worker maintenance migration'
                    USING HINT = 'A foreign key outside the SDK references reporting_status_scope_checkpoints; drop it first.';
            END IF;
            EXECUTE format('LOCK TABLE %s IN ACCESS EXCLUSIVE MODE', reference.table_name);
            EXECUTE format('ALTER TABLE %s DROP CONSTRAINT %I',
                reference.table_name, reference.constraint_name);
            IF reference.width = 7 THEN
                restored := restored || format('ALTER TABLE %s ADD CONSTRAINT %I %s',
                    reference.table_name,
                    coalesce(canonical ->> reference.table_name, reference.constraint_name),
                    reference.definition);
            END IF;
        END LOOP;
        ALTER TABLE reporting_status_scope_checkpoints
            DROP CONSTRAINT reporting_status_scope_checkpoints_pkey,
            DROP CONSTRAINT reporting_status_scope_checkpoints_offering_key,
            ADD CONSTRAINT reporting_status_scope_checkpoints_pkey PRIMARY KEY
                (account_id, consumer_namespace, delivery_config_id, version,
                 scope_kind, obligation_namespace, offering_id);
        FOREACH item IN ARRAY restored LOOP
            EXECUTE item;
        END LOOP;

        -- The widened event key takes the name of the seven-column one.
        FOR reference IN
            SELECT widened.conrelid::regclass::TEXT AS table_name,
                   widened.conname::TEXT AS widened_name, earlier.conname::TEXT AS earlier_name
            FROM pg_constraint widened
            JOIN pg_constraint earlier ON earlier.conrelid = widened.conrelid
                AND earlier.contype = 'u' AND earlier.conname <> widened.conname
            WHERE widened.contype = 'u'
              AND widened.conname = widened.conrelid::regclass::TEXT || '_offering_key'
              AND ARRAY(
                SELECT attribute.attname::TEXT
                FROM unnest(earlier.conkey) WITH ORDINALITY AS key_column(attnum, position)
                JOIN pg_attribute attribute ON attribute.attrelid = earlier.conrelid
                    AND attribute.attnum = key_column.attnum
                ORDER BY key_column.position) = ARRAY[
                    'account_id', 'consumer_namespace', 'delivery_config_id', 'version',
                    'scope_kind', 'obligation_namespace', 'cause_generation']
        LOOP
            EXECUTE format('ALTER TABLE %s DROP CONSTRAINT %I',
                reference.table_name, reference.earlier_name);
            EXECUTE format('ALTER TABLE %s RENAME CONSTRAINT %I TO %I',
                reference.table_name, reference.widened_name, reference.earlier_name);
        END LOOP;
    END IF;
END
$contract$;
