-- Key status checkpoints, and the status events that reference them, by the
-- generation's resolved offering.
--
-- A reporting subscription keeps one generation per resolved offering of a
-- version (adcp-client-python#1729), so two offerings of one subscription
-- version, for one account and one owner, must not share a checkpoint. The
-- checkpoint key is (account_id, consumer_namespace, delivery_config_id,
-- version, scope_kind, obligation_namespace, offering_id), with offering_id ''
-- for every account-level generation, so existing rows keep their key and
-- their uniqueness. Each status event table carries its checkpoint's offering,
-- references the widened key and is unique per checkpoint generation under it.
--
-- Idempotent, and a no-op where status notifications are not installed. It
-- runs after reporting_status_selector_version.sql (and reporting_projection.sql
-- where the projection is installed) and before
-- reporting_projection_notifications.sql, which creates its event table on the
-- widened key. reporting_status_notifications.sql creates the checkpoint and
-- event tables on their published six-column key; this file widens them, an
-- earlier release's projection event table too, and defines the checkpoint and
-- event guards, which compare the offering. Constraints keep their names.
--
-- A schema still on the six-column key is widened under the schema lock. This
-- is a maintenance upgrade: stop every reporting worker first, because workers
-- of earlier releases write the six-column key. A foreign key onto the
-- checkpoint key that the SDK does not own refuses the upgrade instead of
-- being dropped.

DO $checkpoint_offerings$
DECLARE
    key_columns TEXT[];
    reference RECORD;
    events TEXT;
BEGIN
    PERFORM pg_advisory_xact_lock(hashtext('adcp.reporting.schema'), hashtext(current_schema()));
    IF to_regclass('reporting_status_scope_checkpoints') IS NULL THEN
        RETURN;
    END IF;
    SELECT ARRAY(
        SELECT attribute.attname::TEXT
        FROM unnest(pk.conkey) WITH ORDINALITY AS key_column(attnum, position)
        JOIN pg_attribute attribute
          ON attribute.attrelid = pk.conrelid AND attribute.attnum = key_column.attnum
        ORDER BY key_column.position
    )
    INTO key_columns
    FROM pg_constraint pk
    WHERE pk.conrelid = 'reporting_status_scope_checkpoints'::regclass AND pk.contype = 'p';

    IF key_columns IS DISTINCT FROM ARRAY[
        'account_id', 'consumer_namespace', 'delivery_config_id', 'version',
        'scope_kind', 'obligation_namespace', 'offering_id'] THEN
        IF key_columns IS DISTINCT FROM ARRAY[
            'account_id', 'consumer_namespace', 'delivery_config_id', 'version',
            'scope_kind', 'obligation_namespace'] THEN
            RAISE EXCEPTION 'Keying status checkpoints by offering requires a stopped-worker maintenance migration'
                USING HINT = 'reporting_status_scope_checkpoints has an unexpected primary key.';
        END IF;
        LOCK TABLE reporting_status_scope_checkpoints IN ACCESS EXCLUSIVE MODE;
        -- Only the SDK's own status event tables reference the key; each is
        -- re-pointed at the widened key below.
        FOR reference IN
            SELECT fk.conrelid::regclass::TEXT AS table_name, fk.conname::TEXT AS constraint_name
            FROM pg_constraint fk
            WHERE fk.contype = 'f' AND fk.confrelid = 'reporting_status_scope_checkpoints'::regclass
        LOOP
            IF reference.table_name NOT IN (
                'reporting_status_notification_events',
                'reporting_projection_notification_events'
            ) THEN
                RAISE EXCEPTION 'Keying status checkpoints by offering requires a stopped-worker maintenance migration'
                    USING HINT = 'A foreign key outside the SDK references reporting_status_scope_checkpoints; re-point it at the widened key first.';
            END IF;
            EXECUTE format('LOCK TABLE %I IN ACCESS EXCLUSIVE MODE', reference.table_name);
            EXECUTE format(
                'ALTER TABLE %I DROP CONSTRAINT %I', reference.table_name, reference.constraint_name
            );
        END LOOP;
        ALTER TABLE reporting_status_scope_checkpoints
            DROP CONSTRAINT reporting_status_scope_checkpoints_pkey,
            ADD CONSTRAINT reporting_status_scope_checkpoints_pkey PRIMARY KEY
                (account_id, consumer_namespace, delivery_config_id, version,
                 scope_kind, obligation_namespace, offering_id);
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint
        WHERE conrelid = 'reporting_status_scope_checkpoints'::regclass
          AND conname = 'reporting_status_scope_checkpoints_offering') THEN
        ALTER TABLE reporting_status_scope_checkpoints
            ADD CONSTRAINT reporting_status_scope_checkpoints_offering
                CHECK (coalesce(scope #>> '{generation_key,offering_id}', '') = offering_id);
    END IF;

    FOREACH events IN ARRAY ARRAY[
        'reporting_status_notification_events', 'reporting_projection_notification_events'] LOOP
        CONTINUE WHEN to_regclass(events) IS NULL;
        CONTINUE WHEN EXISTS (SELECT 1 FROM pg_constraint fk
            WHERE fk.conrelid = events::regclass AND fk.contype = 'f'
              AND fk.confrelid = 'reporting_status_scope_checkpoints'::regclass);
        EXECUTE format('LOCK TABLE %I IN ACCESS EXCLUSIVE MODE', events);
        IF NOT EXISTS (SELECT 1 FROM pg_attribute
            WHERE attrelid = events::regclass AND attname = 'offering_id' AND NOT attisdropped) THEN
            EXECUTE format(
                'ALTER TABLE %I ADD COLUMN offering_id TEXT COLLATE "C" NOT NULL DEFAULT %L',
                events, '');
            -- An event of an offering's checkpoint takes that offering. Events
            -- are otherwise immutable: only this new column is filled, once.
            IF EXISTS (SELECT 1 FROM reporting_status_scope_checkpoints WHERE offering_id <> '') THEN
                EXECUTE format('ALTER TABLE %I DISABLE TRIGGER USER', events);
                EXECUTE format(
                    'UPDATE %I e SET offering_id = c.offering_id'
                    ' FROM reporting_status_scope_checkpoints c'
                    ' WHERE c.account_id = e.account_id'
                    ' AND c.consumer_namespace = e.consumer_namespace'
                    ' AND c.delivery_config_id = e.delivery_config_id AND c.version = e.version'
                    ' AND c.scope_kind = e.scope_kind'
                    ' AND c.obligation_namespace = e.obligation_namespace'
                    ' AND c.offering_id <> %L', events, '');
                EXECUTE format('ALTER TABLE %I ENABLE TRIGGER USER', events);
            END IF;
        END IF;
        -- One event per checkpoint generation, under the widened key. The
        -- replacement takes the name PostgreSQL gave the six-column one.
        FOR reference IN
            SELECT unique_key.conname::TEXT AS constraint_name
            FROM pg_constraint unique_key
            WHERE unique_key.conrelid = events::regclass AND unique_key.contype = 'u'
              AND ARRAY(
                SELECT attribute.attname::TEXT
                FROM unnest(unique_key.conkey) WITH ORDINALITY AS key_column(attnum, position)
                JOIN pg_attribute attribute ON attribute.attrelid = unique_key.conrelid
                    AND attribute.attnum = key_column.attnum
                ORDER BY key_column.position) = ARRAY[
                    'account_id', 'consumer_namespace', 'delivery_config_id', 'version',
                    'scope_kind', 'obligation_namespace', 'cause_generation']
        LOOP
            EXECUTE format('ALTER TABLE %I DROP CONSTRAINT %I', events, reference.constraint_name);
            EXECUTE format(
                'ALTER TABLE %I ADD CONSTRAINT %I UNIQUE (account_id, consumer_namespace,'
                ' delivery_config_id, version, scope_kind, obligation_namespace, offering_id,'
                ' cause_generation)', events, reference.constraint_name);
        END LOOP;
        -- PostgreSQL names the reference as it named the six-column one.
        EXECUTE format(
            'ALTER TABLE %I ADD FOREIGN KEY (account_id, consumer_namespace,'
            ' delivery_config_id, version, scope_kind, obligation_namespace, offering_id)'
            ' REFERENCES reporting_status_scope_checkpoints', events);
        IF NOT EXISTS (SELECT 1 FROM pg_constraint
            WHERE conrelid = events::regclass AND conname = events || '_offering') THEN
            EXECUTE format(
                'ALTER TABLE %I ADD CONSTRAINT %I CHECK'
                ' (coalesce(snapshot #>> %L, %L) = offering_id)',
                events, events || '_offering', '{cause,scope,generation_key,offering_id}', '');
        END IF;
    END LOOP;

    -- A checkpoint never changes its key, offering included, and an obligation
    -- scope names an obligation of its own generation.
    CREATE OR REPLACE FUNCTION reporting_status_scope_guard()
    RETURNS TRIGGER LANGUAGE plpgsql AS $scope_guard$
    DECLARE owner reporting_obligations;
    BEGIN
        IF TG_OP='UPDATE' AND (
            ROW(NEW.account_id, NEW.consumer_namespace, NEW.delivery_config_id, NEW.version,
                NEW.scope_kind, NEW.obligation_namespace, NEW.offering_id) IS DISTINCT FROM
            ROW(OLD.account_id, OLD.consumer_namespace, OLD.delivery_config_id, OLD.version,
                OLD.scope_kind, OLD.obligation_namespace, OLD.offering_id)
            OR NEW.generation < OLD.generation OR NEW.generation > OLD.generation + 1
            OR NEW.source_sequence < OLD.source_sequence
            OR (OLD.initialized AND NOT NEW.initialized)
            OR (OLD.baseline AND NOT NEW.baseline)
            OR (OLD.initialized AND
                ((NEW.generation <> OLD.generation) <> (NEW.fingerprint <> OLD.fingerprint)))) THEN
            RAISE EXCEPTION 'invalid status checkpoint transition' USING ERRCODE='23514';
        END IF;
        IF NEW.scope_kind='obligation' THEN
            SELECT * INTO owner FROM reporting_obligations
                WHERE account_id=NEW.account_id AND consumer_id=NEW.consumer_namespace
                    AND reporting_obligation_id=NEW.obligation_namespace;
            IF NOT FOUND OR owner.delivery_config_id <> NEW.delivery_config_id
                OR owner.delivery_config_version <> NEW.version
                OR owner.offering_id <> NEW.offering_id THEN
                RAISE EXCEPTION 'invalid status scope owner' USING ERRCODE='23514';
            END IF;
        END IF;
        IF NEW.initialized AND (
            NOT NEW.publishable OR NEW.snapshot->>'health' IS NULL
            OR jsonb_typeof(NEW.snapshot->'issues') IS DISTINCT FROM 'array'
            OR CASE WHEN NEW.snapshot->>'health' IN ('delayed','action_required') THEN
                NOT EXISTS (SELECT 1 FROM jsonb_array_elements(NEW.snapshot->'issues') i
                            WHERE i->>'severity'=NEW.snapshot->>'health')
                ELSE jsonb_array_length(NEW.snapshot->'issues')<>0 END) THEN
            RAISE EXCEPTION 'invalid status issue invariant' USING ERRCODE='23514';
        END IF;
        RETURN NEW;
    END
    $scope_guard$;
    IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgrelid='reporting_status_scope_checkpoints'::regclass
                   AND tgname='reporting_status_scope_guard' AND NOT tgisinternal) THEN
        CREATE TRIGGER reporting_status_scope_guard BEFORE INSERT OR UPDATE
            ON reporting_status_scope_checkpoints FOR EACH ROW EXECUTE FUNCTION reporting_status_scope_guard();
    END IF;

    -- An event publishes exactly its checkpoint's current generation.
    CREATE OR REPLACE FUNCTION reporting_status_event_guard()
    RETURNS TRIGGER LANGUAGE plpgsql AS $event_guard$
    DECLARE checkpoint reporting_status_scope_checkpoints; ids jsonb;
    BEGIN
        SELECT * INTO checkpoint FROM reporting_status_scope_checkpoints
            WHERE account_id=NEW.account_id AND consumer_namespace=NEW.consumer_namespace
            AND delivery_config_id=NEW.delivery_config_id AND version=NEW.version
            AND scope_kind=NEW.scope_kind AND obligation_namespace=NEW.obligation_namespace
            AND offering_id=NEW.offering_id;
        IF NOT FOUND OR NOT checkpoint.initialized OR NOT checkpoint.publishable
            OR checkpoint.generation<>NEW.cause_generation OR checkpoint.fingerprint<>NEW.fingerprint
            OR checkpoint.snapshot->>'health' IS DISTINCT FROM NEW.snapshot #>> '{cause,health}' THEN
            RAISE EXCEPTION 'invalid status event checkpoint' USING ERRCODE='23514';
        END IF;
        SELECT coalesce(jsonb_agg(issue_id ORDER BY issue_id COLLATE "C"), '[]') INTO ids
            FROM (SELECT DISTINCT i->>'issue_id' COLLATE "C" AS issue_id
                  FROM jsonb_array_elements(checkpoint.snapshot->'issues') i
                  ORDER BY issue_id LIMIT 16) selected;
        IF ids IS DISTINCT FROM NEW.snapshot #> '{cause,issue_ids}' THEN
            RAISE EXCEPTION 'invalid status event issues' USING ERRCODE='23514';
        END IF;
        RETURN NEW;
    END
    $event_guard$;
    IF to_regclass('reporting_status_notification_events') IS NOT NULL AND NOT EXISTS (
        SELECT 1 FROM pg_trigger WHERE tgrelid='reporting_status_notification_events'::regclass
        AND tgname='reporting_status_event_guard' AND NOT tgisinternal) THEN
        CREATE TRIGGER reporting_status_event_guard BEFORE INSERT
            ON reporting_status_notification_events FOR EACH ROW EXECUTE FUNCTION reporting_status_event_guard();
    END IF;

    IF to_regclass('reporting_projection_accounts') IS NULL THEN
        RETURN;
    END IF;
    -- The projection's status events: the same checkpoint check, after
    -- activation. reporting_projection_notifications.sql installs its trigger.
    CREATE OR REPLACE FUNCTION reporting_projection_event_guard()
    RETURNS TRIGGER LANGUAGE plpgsql AS $projection_event_guard$
    DECLARE checkpoint reporting_status_scope_checkpoints; ids jsonb;
    BEGIN
        IF NOT EXISTS (SELECT 1 FROM reporting_projection_accounts
            WHERE account_id=NEW.account_id AND current_input IS NOT NULL
            AND notifications_enabled) THEN
            RAISE EXCEPTION 'status_projection_activation_required' USING ERRCODE='23514';
        END IF;
        SELECT * INTO checkpoint FROM reporting_status_scope_checkpoints
            WHERE account_id=NEW.account_id AND consumer_namespace=NEW.consumer_namespace
            AND delivery_config_id=NEW.delivery_config_id AND version=NEW.version
            AND scope_kind=NEW.scope_kind AND obligation_namespace=NEW.obligation_namespace
            AND offering_id=NEW.offering_id;
        IF NOT FOUND OR NOT checkpoint.initialized OR NOT checkpoint.publishable
            OR checkpoint.generation<>NEW.cause_generation OR checkpoint.fingerprint<>NEW.fingerprint
            OR checkpoint.snapshot->>'health' IS DISTINCT FROM NEW.snapshot #>> '{cause,health}' THEN
            RAISE EXCEPTION 'invalid status event checkpoint' USING ERRCODE='23514';
        END IF;
        SELECT coalesce(jsonb_agg(issue_id ORDER BY issue_id COLLATE "C"), '[]') INTO ids
            FROM (SELECT DISTINCT i->>'issue_id' COLLATE "C" AS issue_id
                  FROM jsonb_array_elements(checkpoint.snapshot->'issues') i
                  ORDER BY issue_id LIMIT 16) selected;
        IF ids IS DISTINCT FROM NEW.snapshot #> '{cause,issue_ids}' THEN
            RAISE EXCEPTION 'invalid status event issues' USING ERRCODE='23514';
        END IF;
        RETURN NEW;
    END
    $projection_event_guard$;
END
$checkpoint_offerings$;
