-- #1167B2. Isolated, additive work objects; no A/B/C/B1 guard is replaced.
-- A single statement also makes autocommit/repeated/concurrent installation atomic.
DO $materializer$
BEGIN
    PERFORM pg_advisory_xact_lock(hashtext('adcp.reporting.schema'), hashtext(current_schema()));
    PERFORM account_id, consumer_id FROM reporting_reconciliation_records LIMIT 0;

    CREATE TABLE IF NOT EXISTS reporting_materializer_accounts (
        account_id TEXT COLLATE "C" PRIMARY KEY,
        due_at TIMESTAMPTZ,
        served_at TIMESTAMPTZ NOT NULL DEFAULT '-infinity',
        captured_sequence BIGINT NOT NULL DEFAULT 0 CHECK (captured_sequence >= 0)
    );
    CREATE INDEX IF NOT EXISTS reporting_materializer_account_due
        ON reporting_materializer_accounts (served_at, account_id) WHERE due_at IS NOT NULL;
    CREATE INDEX IF NOT EXISTS reporting_materializer_account_wakeup
        ON reporting_materializer_accounts (due_at, served_at, account_id) WHERE due_at IS NOT NULL;

    CREATE TABLE IF NOT EXISTS reporting_materializer_discovery (
        account_id TEXT COLLATE "C" NOT NULL,
        consumer_id TEXT COLLATE "C" NOT NULL,
        delivery_config_id TEXT COLLATE "C" NOT NULL,
        delivery_config_version BIGINT NOT NULL CHECK (delivery_config_version > 0),
        after_obligation_id TEXT COLLATE "C" NOT NULL DEFAULT '',
        complete BOOLEAN NOT NULL DEFAULT FALSE,
        PRIMARY KEY (account_id, consumer_id, delivery_config_id, delivery_config_version),
        FOREIGN KEY (account_id, delivery_config_id, delivery_config_version)
            REFERENCES reporting_configurations(account_id, delivery_config_id, delivery_config_version)
    );
    CREATE INDEX IF NOT EXISTS reporting_materializer_discovery_pending
        ON reporting_materializer_discovery (account_id, consumer_id, delivery_config_id,
            delivery_config_version) WHERE NOT complete;
    CREATE INDEX IF NOT EXISTS reporting_materializer_obligation_discovery
        ON reporting_obligations (account_id, delivery_config_id, delivery_config_version,
            reporting_obligation_id);
    CREATE INDEX IF NOT EXISTS reporting_materializer_binding_discovery
        ON reporting_reconciliation_records (account_id, consumer_id, delivery_config_id,
            delivery_config_version) WHERE record_kind='destination_binding';

    CREATE TABLE IF NOT EXISTS reporting_materializer_candidates (
        account_id TEXT COLLATE "C" NOT NULL REFERENCES reporting_materializer_accounts,
        consumer_id TEXT COLLATE "C" NOT NULL,
        delivery_config_id TEXT COLLATE "C" NOT NULL,
        delivery_config_version BIGINT NOT NULL CHECK (delivery_config_version > 0),
        reporting_obligation_id TEXT COLLATE "C" NOT NULL,
        generation BIGINT NOT NULL DEFAULT 1 CHECK (generation > 0),
        due_at TIMESTAMPTZ,
        reason TEXT COLLATE "C" NOT NULL DEFAULT 'ready' CHECK (reason IN (
            'ready','verified','retry','inactive','revision_not_ready','revision_unreadable',
            'target_changed','history_corrupt','legacy_pending','legacy_terminal',
            'component_unavailable','binding_changed','effect_unknown','operator_required')),
        served_at TIMESTAMPTZ NOT NULL DEFAULT '-infinity',
        PRIMARY KEY (account_id, consumer_id, delivery_config_id, delivery_config_version,
            reporting_obligation_id),
        FOREIGN KEY (account_id, delivery_config_id, delivery_config_version, reporting_obligation_id)
            REFERENCES reporting_obligations(account_id, delivery_config_id,
                delivery_config_version, reporting_obligation_id)
    );
    CREATE INDEX IF NOT EXISTS reporting_materializer_candidate_due
        ON reporting_materializer_candidates (account_id, due_at, served_at,
            consumer_id, reporting_obligation_id) WHERE due_at IS NOT NULL;
    CREATE INDEX IF NOT EXISTS reporting_materializer_candidate_publication
        ON reporting_materializer_candidates (account_id, reporting_obligation_id);

    CREATE TABLE IF NOT EXISTS reporting_materializer_work (
        account_id TEXT COLLATE "C" NOT NULL,
        consumer_id TEXT COLLATE "C" NOT NULL,
        delivery_config_id TEXT COLLATE "C" NOT NULL,
        delivery_config_version BIGINT NOT NULL,
        reporting_obligation_id TEXT COLLATE "C" NOT NULL,
        reporting_revision_id TEXT COLLATE "C" NOT NULL,
        reporting_materialization_id TEXT COLLATE "C" NOT NULL,
        attempt_namespace TEXT COLLATE "C" NOT NULL DEFAULT 'materialization_attempt'
            CHECK (attempt_namespace = 'materialization_attempt'),
        generation BIGINT NOT NULL CHECK (generation > 0),
        binding_sha256 TEXT COLLATE "C" NOT NULL CHECK (binding_sha256 ~ '^[0-9a-f]{64}$'),
        verification_key_sha256 TEXT COLLATE "C" NOT NULL
            CHECK (verification_key_sha256 ~ '^[0-9a-f]{64}$'),
        external_id TEXT COLLATE "C" NOT NULL CHECK (external_id ~ '^rwm_[0-9a-f]{64}$'),
        state TEXT COLLATE "C" NOT NULL DEFAULT 'pending' CHECK (state IN ('pending','acked')),
        retry_allowed BOOLEAN NOT NULL DEFAULT FALSE,
        reason TEXT COLLATE "C" NOT NULL DEFAULT 'ready' CHECK (reason IN (
            'ready','verified','retry','inactive','revision_not_ready','revision_unreadable',
            'target_changed','history_corrupt','legacy_pending','legacy_terminal',
            'component_unavailable','binding_changed','effect_unknown','operator_required')),
        created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
        acknowledged_at TIMESTAMPTZ,
        completion_token UUID,
        lease_token UUID,
        lease_until TIMESTAMPTZ,
        due_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
        imported BOOLEAN NOT NULL DEFAULT FALSE,
        notifications_enabled BOOLEAN NOT NULL,
        -- No B2.1 reservation is production-admitted. Future activation must
        -- preserve this epoch on retained work, including pending resumes.
        admission_epoch BIGINT NOT NULL DEFAULT 0 CHECK (admission_epoch = 0),
        PRIMARY KEY (account_id, consumer_id, reporting_materialization_id),
        UNIQUE (account_id, consumer_id, external_id),
        FOREIGN KEY (account_id, consumer_id, delivery_config_id, delivery_config_version,
            reporting_obligation_id) REFERENCES reporting_materializer_candidates,
        FOREIGN KEY (account_id, consumer_id, attempt_namespace, reporting_materialization_id)
            REFERENCES reporting_reconciliation_records(account_id, consumer_id, namespace, record_id),
        FOREIGN KEY (account_id, reporting_obligation_id, reporting_revision_id)
            REFERENCES reporting_revisions(account_id, reporting_obligation_id, reporting_revision_id),
        CHECK ((lease_token IS NULL) = (lease_until IS NULL)),
        CHECK ((state = 'acked') = (acknowledged_at IS NOT NULL)),
        CHECK ((state = 'acked') = (completion_token IS NOT NULL)),
        CHECK (state <> 'acked' OR lease_token IS NULL),
        CHECK (NOT retry_allowed OR state = 'acked')
    );
    CREATE UNIQUE INDEX IF NOT EXISTS reporting_materializer_one_pending
        ON reporting_materializer_work (account_id, consumer_id, delivery_config_id,
            delivery_config_version, reporting_obligation_id) WHERE state = 'pending';
    CREATE INDEX IF NOT EXISTS reporting_materializer_work_due
        ON reporting_materializer_work (account_id, due_at, reporting_materialization_id)
        WHERE state = 'pending';

    CREATE TABLE IF NOT EXISTS reporting_materializer_status_heads (
        account_id TEXT COLLATE "C" NOT NULL,
        consumer_id TEXT COLLATE "C" NOT NULL,
        max_sequence BIGINT NOT NULL CHECK (max_sequence > 0),
        PRIMARY KEY (account_id, consumer_id)
    );
    CREATE TABLE IF NOT EXISTS reporting_materializer_status_boundaries (
        account_id TEXT COLLATE "C" NOT NULL,
        consumer_id TEXT COLLATE "C" NOT NULL,
        sequence BIGINT NOT NULL CHECK (sequence > 0),
        account_sequence BIGINT NOT NULL CHECK (account_sequence > 0),
        reporting_materialization_id TEXT COLLATE "C" NOT NULL,
        outcome_namespace TEXT COLLATE "C" NOT NULL DEFAULT 'materialization'
            CHECK (outcome_namespace = 'materialization'),
        as_of TIMESTAMPTZ NOT NULL,
        input JSONB NOT NULL,
        content_sha256 TEXT COLLATE "C" NOT NULL CHECK (content_sha256 ~ '^[0-9a-f]{64}$'),
        PRIMARY KEY (account_id, consumer_id, sequence),
        UNIQUE (account_id, consumer_id, reporting_materialization_id),
        UNIQUE (account_id, account_sequence),
        FOREIGN KEY (account_id, consumer_id, reporting_materialization_id)
            REFERENCES reporting_materializer_work,
        FOREIGN KEY (account_id, consumer_id, outcome_namespace, reporting_materialization_id)
            REFERENCES reporting_reconciliation_records(account_id, consumer_id, namespace, record_id),
        CHECK ((input->>'version')::integer IS NOT DISTINCT FROM 1),
        CHECK ((input->>'account_id') IS NOT DISTINCT FROM account_id),
        CHECK ((input->>'consumer_id') IS NOT DISTINCT FROM consumer_id),
        CHECK ((input->>'reporting_materialization_id') IS NOT DISTINCT FROM reporting_materialization_id),
        CHECK ((input->>'sequence')::bigint IS NOT DISTINCT FROM sequence),
        CHECK ((input->>'account_sequence')::bigint IS NOT DISTINCT FROM account_sequence),
        CHECK ((input->>'as_of')::timestamptz IS NOT DISTINCT FROM as_of),
        CHECK (content_sha256 = reporting_payload_sha256(input)),
        CHECK (input - ARRAY['version','account_id','consumer_id','reporting_materialization_id',
            'sequence','account_sequence','as_of','core','reconciliation'] = '{}'::jsonb)
    );
    CREATE TABLE IF NOT EXISTS reporting_materializer_notification_events (
        account_id TEXT COLLATE "C" NOT NULL,
        notification_id TEXT COLLATE "C" NOT NULL,
        notification_type TEXT COLLATE "C" NOT NULL CHECK (notification_type='reporting.delivery_ready'),
        cause_kind TEXT COLLATE "C" NOT NULL CHECK (cause_kind='materialization_ready'),
        cause_id TEXT COLLATE "C" NOT NULL,
        cause_generation BIGINT NOT NULL CHECK (cause_generation=1),
        consumer_namespace TEXT COLLATE "C" NOT NULL CHECK (length(consumer_namespace)>0),
        admission_epoch BIGINT NOT NULL DEFAULT 0 CHECK (admission_epoch = 0),
        fired_at TIMESTAMPTZ NOT NULL,
        snapshot JSONB NOT NULL,
        reporting_materialization_id TEXT COLLATE "C" GENERATED ALWAYS AS
            (snapshot #>> '{cause,reporting_materialization_id}') STORED,
        PRIMARY KEY (account_id, consumer_namespace, notification_id),
        UNIQUE (account_id, consumer_namespace, notification_type, cause_kind, cause_id, cause_generation),
        FOREIGN KEY (account_id, consumer_namespace, reporting_materialization_id)
            REFERENCES reporting_materializer_status_boundaries
                (account_id, consumer_id, reporting_materialization_id),
        CHECK ((snapshot->>'account_id') IS NOT DISTINCT FROM account_id),
        CHECK ((snapshot->>'notification_id') IS NOT DISTINCT FROM notification_id),
        CHECK (cause_id::jsonb IS NOT DISTINCT FROM
            jsonb_build_array(consumer_namespace, reporting_materialization_id)),
        CHECK ((snapshot #>> '{cause,consumer_id}') IS NOT DISTINCT FROM consumer_namespace)
    );
    CREATE TABLE IF NOT EXISTS reporting_materializer_notification_expansions (
        account_id TEXT COLLATE "C" NOT NULL,
        consumer_namespace TEXT COLLATE "C" NOT NULL,
        notification_id TEXT COLLATE "C" NOT NULL,
        emission_generation BIGINT NOT NULL CHECK (emission_generation>0),
        state TEXT NOT NULL DEFAULT 'quarantined' CHECK
            (state IN ('pending','leased','complete','suppressed','quarantined')),
        due_at TIMESTAMPTZ NOT NULL,
        lease_token TEXT,
        lease_expires_at TIMESTAMPTZ,
        claim_count BIGINT NOT NULL DEFAULT 0,
        error_code TEXT CHECK (error_code IN (
            'network','retryable_http','permanent_http','signing_unavailable',
            'permanent_scope','subscription_unavailable','subscription_changed',
            'invalid_configuration','invalid_payload','integrity_failure','lease_expired')),
        PRIMARY KEY (account_id, consumer_namespace, notification_id, emission_generation),
        FOREIGN KEY (account_id, consumer_namespace, notification_id)
            REFERENCES reporting_materializer_notification_events
    );
    CREATE INDEX IF NOT EXISTS reporting_materializer_notification_due
        ON reporting_materializer_notification_expansions (account_id, due_at)
        WHERE state IN ('pending','leased');

    EXECUTE $function$
    CREATE OR REPLACE FUNCTION reporting_materializer_expansion_guard() RETURNS trigger
    LANGUAGE plpgsql AS $body$
    DECLARE epoch BIGINT;
    BEGIN
        SELECT admission_epoch INTO epoch FROM reporting_materializer_notification_events
        WHERE account_id=NEW.account_id AND consumer_namespace=NEW.consumer_namespace
            AND notification_id=NEW.notification_id;
        IF epoch=0 AND NEW.state <> 'quarantined' THEN
            RAISE EXCEPTION 'reporting_materializer_pre_activation_event'
                USING ERRCODE='23514';
        END IF;
        RETURN NEW;
    END
    $body$ $function$;
    IF NOT EXISTS (SELECT 1 FROM pg_trigger
        WHERE tgrelid='reporting_materializer_notification_expansions'::regclass
        AND tgname='reporting_materializer_pre_activation_guard') THEN
        CREATE TRIGGER reporting_materializer_pre_activation_guard BEFORE INSERT OR UPDATE
            ON reporting_materializer_notification_expansions FOR EACH ROW
            EXECUTE FUNCTION reporting_materializer_expansion_guard();
    END IF;

    EXECUTE $function$
    CREATE OR REPLACE FUNCTION reporting_materializer_retained_guard() RETURNS trigger
    LANGUAGE plpgsql AS $body$
    BEGIN
        RAISE EXCEPTION 'reporting_materializer_evidence_immutable' USING ERRCODE='23514';
    END
    $body$ $function$;
    IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgrelid='reporting_materializer_status_boundaries'::regclass
        AND tgname='reporting_materializer_boundary_immutable') THEN
        CREATE TRIGGER reporting_materializer_boundary_immutable BEFORE UPDATE OR DELETE
            ON reporting_materializer_status_boundaries FOR EACH ROW
            EXECUTE FUNCTION reporting_materializer_retained_guard();
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgrelid='reporting_materializer_notification_events'::regclass
        AND tgname='reporting_materializer_event_immutable') THEN
        CREATE TRIGGER reporting_materializer_event_immutable BEFORE UPDATE OR DELETE
            ON reporting_materializer_notification_events FOR EACH ROW
            EXECUTE FUNCTION reporting_materializer_retained_guard();
    END IF;

    EXECUTE $function$
    CREATE OR REPLACE FUNCTION reporting_materializer_wake(a TEXT) RETURNS void
    LANGUAGE sql AS $body$
        INSERT INTO reporting_materializer_accounts (account_id, due_at)
        VALUES (a, clock_timestamp()) ON CONFLICT (account_id) DO UPDATE
        SET due_at = LEAST(reporting_materializer_accounts.due_at, EXCLUDED.due_at)
    $body$ $function$;

    EXECUTE $function$
    CREATE OR REPLACE FUNCTION reporting_materializer_dirty(
        a TEXT, c TEXT, d TEXT, v BIGINT, o TEXT
    ) RETURNS void LANGUAGE plpgsql AS $body$
    BEGIN
        PERFORM pg_advisory_xact_lock(hashtext('adcp.reporting:' || a));
        PERFORM reporting_materializer_wake(a);
        INSERT INTO reporting_materializer_candidates
            (account_id, consumer_id, delivery_config_id, delivery_config_version,
             reporting_obligation_id, due_at)
        VALUES (a,c,d,v,o,clock_timestamp())
        ON CONFLICT (account_id, consumer_id, delivery_config_id, delivery_config_version,
            reporting_obligation_id) DO UPDATE
        SET generation = reporting_materializer_candidates.generation + 1,
            due_at = clock_timestamp(), reason = 'ready';
    END
    $body$ $function$;

    EXECUTE $function$
    CREATE OR REPLACE FUNCTION reporting_materializer_source_dirty() RETURNS trigger
    LANGUAGE plpgsql AS $body$
    DECLARE r RECORD;
    BEGIN
        IF TG_OP = 'UPDATE' AND NEW IS NOT DISTINCT FROM OLD THEN
            RETURN NEW;
        END IF;
        PERFORM pg_advisory_xact_lock(hashtext('adcp.reporting:' || NEW.account_id));
        IF TG_TABLE_NAME = 'reporting_configurations' THEN
            UPDATE reporting_materializer_candidates SET generation = generation + 1,
                due_at = clock_timestamp(), reason = 'ready'
            WHERE account_id = NEW.account_id AND delivery_config_id = NEW.delivery_config_id
                AND delivery_config_version = NEW.delivery_config_version;
            IF FOUND THEN PERFORM reporting_materializer_wake(NEW.account_id); END IF;
        ELSIF TG_TABLE_NAME = 'reporting_revisions' THEN
            UPDATE reporting_materializer_candidates SET generation = generation + 1,
                due_at = clock_timestamp(), reason = 'ready'
            WHERE account_id = NEW.account_id AND reporting_obligation_id = NEW.reporting_obligation_id;
            IF FOUND THEN PERFORM reporting_materializer_wake(NEW.account_id); END IF;
        ELSE
            FOR r IN SELECT consumer_id FROM reporting_materializer_discovery
                WHERE account_id = NEW.account_id AND delivery_config_id = NEW.delivery_config_id
                    AND delivery_config_version = NEW.delivery_config_version
                ORDER BY consumer_id
            LOOP
                PERFORM reporting_materializer_dirty(NEW.account_id, r.consumer_id,
                    NEW.delivery_config_id, NEW.delivery_config_version, NEW.reporting_obligation_id);
            END LOOP;
        END IF;
        RETURN NEW;
    END
    $body$ $function$;

    EXECUTE $function$
    CREATE OR REPLACE FUNCTION reporting_materializer_binding_dirty() RETURNS trigger
    LANGUAGE plpgsql AS $body$
    BEGIN
        IF NEW.record_kind = 'destination_binding' THEN
            PERFORM pg_advisory_xact_lock(hashtext('adcp.reporting:' || NEW.account_id));
            INSERT INTO reporting_materializer_discovery
                (account_id, consumer_id, delivery_config_id, delivery_config_version)
            VALUES (NEW.account_id, NEW.consumer_id, NEW.delivery_config_id, NEW.delivery_config_version)
            ON CONFLICT DO NOTHING;
            PERFORM reporting_materializer_wake(NEW.account_id);
        ELSIF NEW.record_kind = 'materialization_check' THEN
            PERFORM reporting_materializer_dirty(NEW.account_id, NEW.consumer_id,
                NEW.delivery_config_id, NEW.delivery_config_version, NEW.reporting_obligation_id);
        END IF;
        -- Consumer rejection is never a materializer retry signal.
        RETURN NEW;
    END
    $body$ $function$;

    EXECUTE $function$
    CREATE OR REPLACE FUNCTION reporting_materializer_work_guard() RETURNS trigger
    LANGUAGE plpgsql AS $body$
    DECLARE a JSONB; result RECORD;
    BEGIN
        IF TG_OP = 'DELETE' THEN
            RAISE EXCEPTION 'reporting_materializer_history_immutable' USING ERRCODE='23514';
        END IF;
        IF TG_OP = 'UPDATE' AND (
            (to_jsonb(NEW) - ARRAY['lease_token','lease_until','due_at','state','retry_allowed',
                'reason','acknowledged_at','completion_token']) IS DISTINCT FROM
            (to_jsonb(OLD) - ARRAY['lease_token','lease_until','due_at','state','retry_allowed',
                'reason','acknowledged_at','completion_token']) OR OLD.state = 'acked'
        ) THEN
            RAISE EXCEPTION 'reporting_materializer_identity_immutable' USING ERRCODE='23514';
        END IF;
        SELECT payload INTO a FROM reporting_reconciliation_records
        WHERE account_id = NEW.account_id AND consumer_id = NEW.consumer_id
            AND namespace = 'materialization_attempt' AND record_id = NEW.reporting_materialization_id;
        IF a IS NULL OR a->>'reporting_revision_id' IS DISTINCT FROM NEW.reporting_revision_id
            OR a #>> '{scope,reporting_obligation_id}' IS DISTINCT FROM NEW.reporting_obligation_id
            OR a #>> '{scope,generation_key,delivery_config_id}' IS DISTINCT FROM NEW.delivery_config_id
            OR (a #>> '{scope,generation_key,delivery_config_version}')::bigint
                IS DISTINCT FROM NEW.delivery_config_version THEN
            RAISE EXCEPTION 'reporting_materializer_attempt_mismatch' USING ERRCODE='23514';
        END IF;
        IF NEW.state = 'acked' THEN
            IF TG_OP <> 'UPDATE' OR OLD.state <> 'pending' OR OLD.lease_token IS NULL
                OR OLD.lease_until <= clock_timestamp()
                OR NEW.completion_token IS DISTINCT FROM OLD.lease_token THEN
                RAISE EXCEPTION 'reporting_materializer_fence_required' USING ERRCODE='23514';
            END IF;
            SELECT payload INTO result FROM reporting_reconciliation_records
            WHERE account_id = NEW.account_id AND consumer_id = NEW.consumer_id
                AND namespace = 'materialization' AND record_id = NEW.reporting_materialization_id;
            IF NOT FOUND OR (NEW.retry_allowed AND result.payload->>'status' <> 'failed') THEN
                RAISE EXCEPTION 'reporting_materializer_outcome_required' USING ERRCODE='23514';
            END IF;
            IF NOT EXISTS (SELECT 1 FROM reporting_materializer_status_boundaries b
                WHERE b.account_id=NEW.account_id AND b.consumer_id=NEW.consumer_id
                    AND b.reporting_materialization_id=NEW.reporting_materialization_id
                    AND b.as_of=NEW.acknowledged_at
                    AND b.as_of=(result.payload->>'completed_at')::timestamptz) THEN
                RAISE EXCEPTION 'reporting_materializer_capture_required' USING ERRCODE='23514';
            END IF;
            IF NEW.notifications_enabled AND result.payload->>'status' <> 'failed'
                AND NOT EXISTS (SELECT 1 FROM reporting_materializer_notification_events e
                    WHERE e.account_id=NEW.account_id AND e.consumer_namespace=NEW.consumer_id
                    AND e.reporting_materialization_id=NEW.reporting_materialization_id
                    AND e.admission_epoch=NEW.admission_epoch) THEN
                RAISE EXCEPTION 'reporting_materializer_event_required' USING ERRCODE='23514';
            END IF;
        END IF;
        RETURN NEW;
    END
    $body$ $function$;

    IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgrelid='reporting_reconciliation_records'::regclass
        AND tgname='reporting_materializer_binding') THEN
        CREATE TRIGGER reporting_materializer_binding AFTER INSERT ON reporting_reconciliation_records
            FOR EACH ROW EXECUTE FUNCTION reporting_materializer_binding_dirty();
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgrelid='reporting_obligations'::regclass
        AND tgname='reporting_materializer_obligation') THEN
        CREATE TRIGGER reporting_materializer_obligation AFTER INSERT ON reporting_obligations
            FOR EACH ROW EXECUTE FUNCTION reporting_materializer_source_dirty();
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgrelid='reporting_revisions'::regclass
        AND tgname='reporting_materializer_publication') THEN
        CREATE TRIGGER reporting_materializer_publication AFTER INSERT OR UPDATE ON reporting_revisions
            FOR EACH ROW EXECUTE FUNCTION reporting_materializer_source_dirty();
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgrelid='reporting_configurations'::regclass
        AND tgname='reporting_materializer_configuration') THEN
        CREATE TRIGGER reporting_materializer_configuration AFTER UPDATE ON reporting_configurations
            FOR EACH ROW EXECUTE FUNCTION reporting_materializer_source_dirty();
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgrelid='reporting_materializer_work'::regclass
        AND tgname='reporting_materializer_guard') THEN
        CREATE TRIGGER reporting_materializer_guard BEFORE INSERT OR UPDATE OR DELETE
            ON reporting_materializer_work FOR EACH ROW
            EXECUTE FUNCTION reporting_materializer_work_guard();
    END IF;

    -- One-time indexed backfill seeds bindings, not account enumeration or
    -- recurring whole-ledger scans. Reinstall never rewinds a discovery cursor.
    INSERT INTO reporting_materializer_discovery
        (account_id, consumer_id, delivery_config_id, delivery_config_version)
    SELECT account_id, consumer_id, delivery_config_id, delivery_config_version
        FROM reporting_reconciliation_records WHERE record_kind='destination_binding'
    ON CONFLICT DO NOTHING;
    INSERT INTO reporting_materializer_accounts (account_id, due_at)
    SELECT account_id, clock_timestamp() FROM reporting_materializer_discovery WHERE NOT complete
        GROUP BY account_id
    ON CONFLICT (account_id) DO UPDATE
        SET due_at=LEAST(reporting_materializer_accounts.due_at, EXCLUDED.due_at);
END
$materializer$;
