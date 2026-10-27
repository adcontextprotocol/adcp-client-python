-- B2.4 v2 capture and activation. All earlier manifests/SQL remain immutable.
-- create_schema() installs this inside the same serialized schema transaction.
DO $migration$
DECLARE source_name TEXT;
BEGIN
    PERFORM pg_advisory_xact_lock(hashtext('adcp.reporting.schema'), hashtext(current_schema()));
    PERFORM account_id FROM reporting_status_scope_checkpoints LIMIT 0;

    CREATE TABLE IF NOT EXISTS reporting_projection_accounts (
        account_id TEXT COLLATE "C" PRIMARY KEY,
        version INTEGER NOT NULL DEFAULT 2 CHECK (version = 2),
        activated_at TIMESTAMPTZ NOT NULL,
        notifications_enabled BOOLEAN NOT NULL,
        consumer_status_enabled BOOLEAN NOT NULL,
        ownership_enabled BOOLEAN NOT NULL,
        policy JSONB NOT NULL,
        legacy_through BIGINT NOT NULL CHECK (legacy_through >= 0),
        legacy_capture_through BIGINT NOT NULL CHECK (legacy_capture_through >= 0),
        legacy_capture_cursor BIGINT NOT NULL DEFAULT 0
            CHECK (legacy_capture_cursor BETWEEN 0 AND legacy_capture_through),
        legacy_generation_floor BIGINT NOT NULL CHECK (legacy_generation_floor >= 0),
        max_sequence BIGINT NOT NULL DEFAULT 0 CHECK (max_sequence >= 0),
        cursor BIGINT NOT NULL DEFAULT 0 CHECK (cursor BETWEEN 0 AND max_sequence),
        checkpoint_floor BIGINT NOT NULL DEFAULT 0 CHECK (checkpoint_floor >= 0),
        current_input JSONB,
        current_as_of TIMESTAMPTZ,
        CHECK ((current_input IS NULL) = (current_as_of IS NULL))
    );
    CREATE INDEX IF NOT EXISTS reporting_projection_pending
        ON reporting_projection_accounts (account_id) WHERE cursor < max_sequence;
    CREATE INDEX IF NOT EXISTS reporting_projection_activation_pending
        ON reporting_projection_accounts (account_id) WHERE current_input IS NULL;
    CREATE TABLE IF NOT EXISTS reporting_projection_writes (
        account_id TEXT COLLATE "C" NOT NULL REFERENCES reporting_projection_accounts,
        transaction_id TEXT COLLATE "C" NOT NULL,
        PRIMARY KEY (account_id, transaction_id)
    );
    CREATE TABLE IF NOT EXISTS reporting_projection_inputs (
        account_id TEXT COLLATE "C" NOT NULL REFERENCES reporting_projection_accounts,
        sequence BIGINT NOT NULL CHECK (sequence > 0),
        transaction_id TEXT COLLATE "C" NOT NULL,
        as_of TIMESTAMPTZ NOT NULL,
        input JSONB NOT NULL,
        content_sha256 TEXT COLLATE "C" NOT NULL,
        PRIMARY KEY (account_id, sequence),
        UNIQUE (account_id, transaction_id),
        CHECK (content_sha256 = reporting_receipt_ingestion_sha256(input)),
        CHECK ((input->>'version')::integer IS NOT DISTINCT FROM 2),
        CHECK ((input->>'account_id') IS NOT DISTINCT FROM account_id),
        CHECK ((input->>'as_of')::timestamptz IS NOT DISTINCT FROM as_of)
    );
    IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgrelid='reporting_projection_inputs'::regclass
        AND tgname='reporting_projection_input_immutable') THEN
        CREATE TRIGGER reporting_projection_input_immutable BEFORE UPDATE OR DELETE
            ON reporting_projection_inputs FOR EACH ROW
            EXECUTE FUNCTION reporting_receipt_ingestion_immutable();
    END IF;
    CREATE TABLE IF NOT EXISTS reporting_projection_legacy_baselines (
        account_id TEXT COLLATE "C" NOT NULL REFERENCES reporting_projection_accounts,
        scope_key TEXT COLLATE "C" NOT NULL,
        checkpoint JSONB NOT NULL,
        content_sha256 TEXT COLLATE "C" NOT NULL,
        PRIMARY KEY(account_id,scope_key),
        CHECK (content_sha256=reporting_receipt_ingestion_sha256(checkpoint))
    );
    CREATE TABLE IF NOT EXISTS reporting_projection_legacy_inputs (
        account_id TEXT COLLATE "C" NOT NULL REFERENCES reporting_projection_accounts,
        account_sequence BIGINT NOT NULL CHECK (account_sequence>0),
        consumer_id TEXT COLLATE "C" NOT NULL,
        kind TEXT COLLATE "C" NOT NULL CHECK (kind IN ('materializer','receipt')),
        input JSONB NOT NULL,
        content_sha256 TEXT COLLATE "C" NOT NULL,
        PRIMARY KEY(account_id,account_sequence),
        CHECK (content_sha256=reporting_receipt_ingestion_sha256(input)),
        CHECK (input->>'account_id' IS NOT DISTINCT FROM account_id),
        CHECK (input->>'consumer_id' IS NOT DISTINCT FROM consumer_id),
        CHECK ((input->>'account_sequence')::bigint IS NOT DISTINCT FROM account_sequence)
    );
    CREATE TABLE IF NOT EXISTS reporting_projection_legacy_steps (
        account_id TEXT COLLATE "C" NOT NULL,
        account_sequence BIGINT NOT NULL,
        scope_key TEXT COLLATE "C" NOT NULL,
        input JSONB NOT NULL,
        content_sha256 TEXT COLLATE "C" NOT NULL,
        PRIMARY KEY(account_id,account_sequence,scope_key),
        FOREIGN KEY(account_id,account_sequence)
            REFERENCES reporting_projection_legacy_inputs,
        CHECK (content_sha256=reporting_receipt_ingestion_sha256(input)),
        CHECK ((input->>'admission_epoch')::integer IS NOT DISTINCT FROM 0)
    );
    CREATE TABLE IF NOT EXISTS reporting_projection_legacy_checkpoints (
        account_id TEXT COLLATE "C" NOT NULL REFERENCES reporting_projection_accounts,
        consumer_id TEXT COLLATE "C" NOT NULL,
        scope_key TEXT COLLATE "C" NOT NULL,
        checkpoint JSONB NOT NULL,
        content_sha256 TEXT COLLATE "C" NOT NULL,
        CHECK (content_sha256=reporting_receipt_ingestion_sha256(checkpoint)),
        PRIMARY KEY(account_id,scope_key)
    );
    CREATE INDEX IF NOT EXISTS reporting_projection_legacy_consumer
        ON reporting_projection_legacy_checkpoints(account_id,consumer_id,scope_key);
    FOREACH source_name IN ARRAY ARRAY['reporting_projection_legacy_baselines',
        'reporting_projection_legacy_inputs','reporting_projection_legacy_steps'] LOOP
        IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgrelid=source_name::regclass
            AND tgname='reporting_projection_legacy_immutable') THEN
            EXECUTE format('CREATE TRIGGER reporting_projection_legacy_immutable'
                ' BEFORE UPDATE OR DELETE ON %I FOR EACH ROW'
                ' EXECUTE FUNCTION reporting_receipt_ingestion_immutable()',source_name);
        END IF;
    END LOOP;
END
$migration$;

DO $migration$
DECLARE source_name TEXT;
BEGIN
    PERFORM pg_advisory_xact_lock(hashtext('adcp.reporting.schema'), hashtext(current_schema()));
    ALTER TABLE reporting_status_scope_checkpoints
        ADD COLUMN IF NOT EXISTS projection_writer_floor INTEGER NOT NULL DEFAULT 1;
    CREATE INDEX IF NOT EXISTS reporting_projection_due_clock
        ON reporting_status_scope_checkpoints (next_due_at,account_id)
        WHERE projection_writer_floor=2 AND next_due_at IS NOT NULL;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint
        WHERE conrelid='reporting_status_scope_checkpoints'::regclass
        AND conname='reporting_projection_writer_floor') THEN
        ALTER TABLE reporting_status_scope_checkpoints
            ADD CONSTRAINT reporting_projection_writer_floor CHECK (projection_writer_floor IN (1,2));
    END IF;

    CREATE OR REPLACE FUNCTION reporting_projection_checkpoint_guard() RETURNS trigger
    LANGUAGE plpgsql AS $function$
    DECLARE floor INTEGER;
    BEGIN
        floor := CASE WHEN TG_OP='INSERT' THEN NEW.projection_writer_floor
                      ELSE OLD.projection_writer_floor END;
        IF floor=2 OR EXISTS (SELECT 1 FROM reporting_projection_accounts
                             WHERE account_id=NEW.account_id) THEN
            IF current_setting('adcp.reporting.projection_version',true) IS DISTINCT FROM '2' THEN
                RAISE EXCEPTION 'status_projection_writer_fenced' USING ERRCODE='23514';
            END IF;
            NEW.projection_writer_floor := 2;
        END IF;
        RETURN NEW;
    END
    $function$;
    IF NOT EXISTS (SELECT 1 FROM pg_trigger
        WHERE tgrelid='reporting_status_scope_checkpoints'::regclass
        AND tgname='reporting_projection_checkpoint_guard') THEN
        CREATE TRIGGER reporting_projection_checkpoint_guard BEFORE INSERT OR UPDATE
            ON reporting_status_scope_checkpoints FOR EACH ROW
            EXECUTE FUNCTION reporting_projection_checkpoint_guard();
    END IF;

    CREATE OR REPLACE FUNCTION reporting_projection_document(owner TEXT, at_time TIMESTAMPTZ)
    RETURNS JSONB LANGUAGE plpgsql STABLE AS $function$
    DECLARE core JSONB; records JSONB; counts JSONB := '{}'; name TEXT;
    BEGIN
        core := reporting_status_projection_input(owner, at_time);
        SELECT coalesce(jsonb_agg(jsonb_build_object(
            'consumer_id', c.consumer_id, 'sequence', c.seq, 'record', r.payload)
            ORDER BY c.consumer_id COLLATE "C", c.seq), '[]') INTO records
        FROM reporting_reconciliation_changes c JOIN reporting_reconciliation_records r
            USING(account_id,consumer_id,namespace,record_id)
        WHERE c.account_id=owner;
        FOREACH name IN ARRAY ARRAY['configurations','obligations','revisions','statuses',
            'lifecycles','issue_scopes','adjustments','changes'] LOOP
            counts := counts || jsonb_build_object(name, jsonb_array_length(core->name));
        END LOOP;
        RETURN jsonb_build_object('version',2,'account_id',owner,'as_of',at_time,
            'core_format','sql-v1','core',core,'reconciliation',records,
            'counts',counts || jsonb_build_object('reconciliation',jsonb_array_length(records)));
    END
    $function$;

    CREATE OR REPLACE FUNCTION reporting_projection_capture(owner TEXT) RETURNS BIGINT
    LANGUAGE plpgsql AS $function$
    DECLARE sequence BIGINT; document JSONB; at_time TIMESTAMPTZ; existing BIGINT;
    BEGIN
        PERFORM pg_advisory_xact_lock(hashtext('adcp.reporting:' || owner));
        SELECT i.sequence INTO existing FROM reporting_projection_inputs i
            WHERE account_id=owner AND transaction_id=pg_current_xact_id()::text;
        IF FOUND THEN RETURN existing; END IF;
        at_time := nullif(current_setting('adcp.reporting.projection_clock',true),'')::timestamptz;
        at_time := coalesce(at_time,clock_timestamp());
        UPDATE reporting_projection_accounts SET max_sequence=max_sequence+1
            WHERE account_id=owner RETURNING max_sequence INTO sequence;
        IF NOT FOUND THEN RAISE EXCEPTION 'status_projection_activation_required' USING ERRCODE='23514'; END IF;
        document := reporting_projection_document(owner,at_time);
        INSERT INTO reporting_projection_inputs
            (account_id,sequence,transaction_id,as_of,input,content_sha256)
            VALUES(owner,sequence,pg_current_xact_id()::text,at_time,document,
                   reporting_receipt_ingestion_sha256(document));
        RETURN sequence;
    END
    $function$;
    CREATE OR REPLACE FUNCTION reporting_projection_mark() RETURNS trigger
    LANGUAGE plpgsql AS $function$
    BEGIN
        IF current_setting('adcp.reporting.projection_internal',true)='on' OR NOT EXISTS
            (SELECT 1 FROM reporting_projection_accounts WHERE account_id=NEW.account_id) THEN
            RETURN NEW;
        END IF;
        IF TG_OP='UPDATE' AND to_jsonb(NEW)=to_jsonb(OLD) THEN RETURN NEW; END IF;
        IF TG_TABLE_NAME='reporting_configurations' AND TG_OP='UPDATE' AND
            (to_jsonb(NEW)-ARRAY['lease_worker_id','lease_expires_at']) =
            (to_jsonb(OLD)-ARRAY['lease_worker_id','lease_expires_at']) THEN
            RETURN NEW;
        END IF;
        PERFORM pg_advisory_xact_lock(hashtext('adcp.reporting:' || NEW.account_id));
        IF EXISTS (SELECT 1 FROM reporting_projection_inputs
            WHERE account_id=NEW.account_id AND transaction_id=pg_current_xact_id()::text) THEN
            RAISE EXCEPTION 'status_projection_boundary_already_captured' USING ERRCODE='23514';
        END IF;
        INSERT INTO reporting_projection_writes VALUES(NEW.account_id,pg_current_xact_id()::text)
            ON CONFLICT DO NOTHING;
        RETURN NEW;
    END
    $function$;
    CREATE OR REPLACE FUNCTION reporting_projection_commit() RETURNS trigger
    LANGUAGE plpgsql AS $function$
    BEGIN
        PERFORM reporting_projection_capture(NEW.account_id);
        RETURN NEW;
    END
    $function$;
    IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgrelid='reporting_projection_writes'::regclass
        AND tgname='reporting_projection_commit') THEN
        CREATE CONSTRAINT TRIGGER reporting_projection_commit AFTER INSERT
            ON reporting_projection_writes DEFERRABLE INITIALLY DEFERRED
            FOR EACH ROW EXECUTE FUNCTION reporting_projection_commit();
    END IF;
    FOREACH source_name IN ARRAY ARRAY['reporting_configurations','reporting_obligations',
        'reporting_revisions','reporting_adjustments','reporting_consumer_statuses',
        'reporting_issue_lifecycle','reporting_issue_status_scopes','reporting_reconciliation_changes'] LOOP
        IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgrelid=source_name::regclass
            AND tgname='reporting_projection_mark') THEN
            EXECUTE format('CREATE TRIGGER reporting_projection_mark AFTER INSERT OR UPDATE ON %I'
                ' FOR EACH ROW EXECUTE FUNCTION reporting_projection_mark()',source_name);
        END IF;
    END LOOP;
END
$migration$;
