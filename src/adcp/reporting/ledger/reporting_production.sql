-- B2.4 production admission. Earlier work/queues and their mandatory manifests
-- remain byte-for-byte unchanged. The SDK uses the same materializer state
-- machine with a closed connection adapter for these new participants.
DO $production$
BEGIN
    PERFORM pg_advisory_xact_lock(hashtext('adcp.reporting.schema'), hashtext(current_schema()));
    CREATE TABLE IF NOT EXISTS reporting_production_delivery_windows (
        account_id TEXT COLLATE "C" NOT NULL,
        idempotency_key TEXT COLLATE "C" NOT NULL,
        queue TEXT NOT NULL CHECK (queue IN ('core','status','ready')),
        body_sha256 TEXT NOT NULL CHECK (body_sha256 ~ '^[0-9a-f]{64}$'),
        started_at TIMESTAMPTZ NOT NULL,
        expires_at TIMESTAMPTZ NOT NULL,
        PRIMARY KEY (account_id,idempotency_key),
        CHECK (expires_at=started_at+interval '86400 seconds')
    );
    IF NOT EXISTS (SELECT 1 FROM pg_trigger
        WHERE tgrelid='reporting_production_delivery_windows'::regclass
          AND tgname='reporting_production_delivery_window_immutable') THEN
        CREATE TRIGGER reporting_production_delivery_window_immutable BEFORE UPDATE OR DELETE
            ON reporting_production_delivery_windows FOR EACH ROW
            EXECUTE FUNCTION reporting_receipt_ingestion_immutable();
    END IF;
    CREATE TABLE IF NOT EXISTS reporting_production_accounts (
        account_id TEXT COLLATE "C" PRIMARY KEY REFERENCES reporting_projection_accounts,
        admission_epoch BIGINT NOT NULL DEFAULT 2 CHECK (admission_epoch=2),
        activated_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
        policy JSONB NOT NULL CHECK (jsonb_typeof(policy)='object'),
        CHECK (jsonb_typeof(policy->'verification_keys') IS NOT DISTINCT FROM 'array'),
        CHECK (jsonb_array_length(policy->'verification_keys') > 0),
        CHECK (jsonb_typeof(policy->'notifications_enabled') IS NOT DISTINCT FROM 'boolean')
    );
    IF NOT EXISTS (SELECT 1 FROM pg_trigger
        WHERE tgrelid='reporting_production_accounts'::regclass
          AND tgname='reporting_production_activation_immutable') THEN
        CREATE TRIGGER reporting_production_activation_immutable BEFORE UPDATE OR DELETE
            ON reporting_production_accounts FOR EACH ROW
            EXECUTE FUNCTION reporting_receipt_ingestion_immutable();
    END IF;
    CREATE TABLE IF NOT EXISTS reporting_production_work (
        account_id TEXT COLLATE "C" NOT NULL REFERENCES reporting_production_accounts,
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
        -- Only new reservations enter this epoch; no historical work is copied.
        admission_epoch BIGINT NOT NULL DEFAULT 2 CHECK (admission_epoch = 2),
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
    CREATE UNIQUE INDEX IF NOT EXISTS reporting_production_one_pending
        ON reporting_production_work (account_id, consumer_id, delivery_config_id,
            delivery_config_version, reporting_obligation_id) WHERE state = 'pending';
    CREATE INDEX IF NOT EXISTS reporting_production_work_due
        ON reporting_production_work (account_id, due_at, reporting_materialization_id)
        WHERE state = 'pending';

    CREATE TABLE IF NOT EXISTS reporting_production_status_heads (
        account_id TEXT COLLATE "C" NOT NULL,
        consumer_id TEXT COLLATE "C" NOT NULL,
        max_sequence BIGINT NOT NULL CHECK (max_sequence > 0),
        PRIMARY KEY (account_id, consumer_id)
    );
    CREATE TABLE IF NOT EXISTS reporting_production_status_boundaries (
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
            REFERENCES reporting_production_work,
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
    CREATE TABLE IF NOT EXISTS reporting_production_notification_events (
        account_id TEXT COLLATE "C" NOT NULL,
        notification_id TEXT COLLATE "C" NOT NULL,
        notification_type TEXT COLLATE "C" NOT NULL CHECK (notification_type='reporting.delivery_ready'),
        cause_kind TEXT COLLATE "C" NOT NULL CHECK (cause_kind='materialization_ready'),
        cause_id TEXT COLLATE "C" NOT NULL,
        cause_generation BIGINT NOT NULL CHECK (cause_generation=1),
        consumer_namespace TEXT COLLATE "C" NOT NULL CHECK (length(consumer_namespace)>0),
        admission_epoch BIGINT NOT NULL DEFAULT 2 CHECK (admission_epoch = 2),
        fired_at TIMESTAMPTZ NOT NULL,
        snapshot JSONB NOT NULL,
        reporting_materialization_id TEXT COLLATE "C" GENERATED ALWAYS AS
            (snapshot #>> '{cause,reporting_materialization_id}') STORED,
        PRIMARY KEY (account_id, consumer_namespace, notification_id),
        UNIQUE (account_id, consumer_namespace, notification_type, cause_kind, cause_id, cause_generation),
        UNIQUE (account_id, consumer_namespace, notification_id, cause_kind, cause_id, cause_generation),
        FOREIGN KEY (account_id, consumer_namespace, reporting_materialization_id)
            REFERENCES reporting_production_status_boundaries
                (account_id, consumer_id, reporting_materialization_id),
        CHECK ((snapshot->>'account_id') IS NOT DISTINCT FROM account_id),
        CHECK ((snapshot->>'notification_id') IS NOT DISTINCT FROM notification_id),
        CHECK (cause_id::jsonb IS NOT DISTINCT FROM
            jsonb_build_array(consumer_namespace, reporting_materialization_id)),
        CHECK ((snapshot #>> '{cause,consumer_id}') IS NOT DISTINCT FROM consumer_namespace)
    );
    CREATE TABLE IF NOT EXISTS reporting_production_notification_expansions (
        account_id TEXT COLLATE "C" NOT NULL,
        consumer_namespace TEXT COLLATE "C" NOT NULL,
        notification_id TEXT COLLATE "C" NOT NULL,
        emission_generation BIGINT NOT NULL CHECK (emission_generation>0),
        state TEXT NOT NULL DEFAULT 'pending' CHECK
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
            REFERENCES reporting_production_notification_events
    );
    CREATE INDEX IF NOT EXISTS reporting_production_notification_due
        ON reporting_production_notification_expansions (account_id, due_at)
        WHERE state IN ('pending','leased');

    CREATE TABLE IF NOT EXISTS reporting_production_notification_deliveries (
        account_id TEXT COLLATE "C" NOT NULL,
        delivery_id TEXT COLLATE "C" NOT NULL,
        subscriber_id TEXT COLLATE "C" NOT NULL,
        principal_id TEXT COLLATE "C" NOT NULL,
        notification_id TEXT COLLATE "C" NOT NULL,
        notification_type TEXT COLLATE "C" NOT NULL CHECK (notification_type = 'reporting.delivery_ready'),
        emission_generation BIGINT NOT NULL CHECK (emission_generation > 0),
        idempotency_key TEXT COLLATE "C" NOT NULL,
        destination_sha256 TEXT NOT NULL,
        subscription_fingerprint TEXT NOT NULL,
        signing_scope_id TEXT COLLATE "C",
        cause_kind TEXT COLLATE "C" NOT NULL CHECK (cause_kind = 'materialization_ready'),
        cause_id TEXT COLLATE "C" NOT NULL,
        cause_generation BIGINT NOT NULL CHECK (cause_generation > 0),
        consumer_namespace TEXT COLLATE "C" NOT NULL,
        auth_mode TEXT NOT NULL,
        body_sha256 TEXT NOT NULL,
        envelope_version INTEGER NOT NULL,
        key_version TEXT COLLATE "C" NOT NULL,
        envelope BYTEA NOT NULL,
        state TEXT NOT NULL DEFAULT 'pending' CHECK
            (state IN ('pending', 'leased', 'complete', 'suppressed', 'quarantined')),
        due_at TIMESTAMPTZ NOT NULL,
        lease_token TEXT,
        lease_expires_at TIMESTAMPTZ,
        claim_count BIGINT NOT NULL DEFAULT 0,
        error_code TEXT CHECK (error_code IN (
            'network', 'retryable_http', 'permanent_http', 'signing_unavailable',
            'permanent_scope', 'subscription_unavailable', 'subscription_changed',
            'invalid_configuration', 'invalid_payload', 'integrity_failure', 'lease_expired')),
        PRIMARY KEY (account_id, consumer_namespace, delivery_id),
        UNIQUE (account_id, consumer_namespace, notification_id, emission_generation, subscriber_id),
        UNIQUE (account_id, consumer_namespace, idempotency_key),
        CHECK (length(principal_id) > 0 AND (consumer_namespace = '' OR consumer_namespace = principal_id)),
        FOREIGN KEY (account_id, consumer_namespace, notification_id, cause_kind, cause_id, cause_generation)
            REFERENCES reporting_production_notification_events
                (account_id, consumer_namespace, notification_id, cause_kind, cause_id, cause_generation),
        FOREIGN KEY (account_id, consumer_namespace, notification_id, emission_generation)
            REFERENCES reporting_production_notification_expansions
                (account_id, consumer_namespace, notification_id, emission_generation)
    );
    CREATE INDEX IF NOT EXISTS reporting_production_notification_deliveries_due
        ON reporting_production_notification_deliveries (account_id, due_at)
        WHERE state IN ('pending', 'leased');


    CREATE TABLE IF NOT EXISTS reporting_production_webhook_attempt_heads (
        account_id TEXT COLLATE "C" NOT NULL,
        consumer_namespace TEXT COLLATE "C" NOT NULL,
        principal_id TEXT COLLATE "C" NOT NULL CHECK (length(principal_id) > 0),
        subscriber_id TEXT COLLATE "C" NOT NULL,
        idempotency_key TEXT COLLATE "C" NOT NULL,
        last_attempt BIGINT NOT NULL CHECK (last_attempt > 0),
        PRIMARY KEY (account_id, consumer_namespace, principal_id, subscriber_id, idempotency_key)
    );
    CREATE TABLE IF NOT EXISTS reporting_production_webhook_attempts (
        account_id TEXT COLLATE "C" NOT NULL,
        principal_id TEXT COLLATE "C" NOT NULL,
        subscriber_id TEXT COLLATE "C" NOT NULL,
        notification_id TEXT COLLATE "C" NOT NULL,
        idempotency_key TEXT COLLATE "C" NOT NULL,
        attempt BIGINT NOT NULL CHECK (attempt > 0),
        delivery_id TEXT COLLATE "C" NOT NULL,
        consumer_namespace TEXT COLLATE "C" NOT NULL,
        lease_token TEXT NOT NULL,
        reservation_token TEXT NOT NULL,
        binding JSONB NOT NULL,
        fired_at TIMESTAMPTZ NOT NULL,
        url TEXT NOT NULL CONSTRAINT reporting_production_webhook_url_safe
            CHECK (length(url) <= 8192 AND url !~ '[?#@]' AND url ~ '^https?://'),
        payload_size_bytes BIGINT NOT NULL CHECK (payload_size_bytes >= 0),
        status TEXT NOT NULL DEFAULT 'pending' CHECK
            (status IN ('pending', 'success', 'failed', 'timeout', 'connection_error')),
        completed_at TIMESTAMPTZ,
        http_status_code INTEGER,
        response_time_ms BIGINT CHECK (response_time_ms >= 0),
        PRIMARY KEY (account_id, consumer_namespace, principal_id, subscriber_id, idempotency_key, attempt),
        UNIQUE (account_id, consumer_namespace, principal_id, delivery_id, lease_token),
        FOREIGN KEY (account_id, consumer_namespace, delivery_id)
            REFERENCES reporting_production_notification_deliveries,
        FOREIGN KEY (account_id, consumer_namespace, principal_id, subscriber_id, idempotency_key)
            REFERENCES reporting_production_webhook_attempt_heads
                (account_id, consumer_namespace, principal_id, subscriber_id, idempotency_key),
        CONSTRAINT reporting_production_webhook_identity CHECK (
            (binding->>'account_id') IS NOT DISTINCT FROM account_id AND
            (binding->>'principal_id') IS NOT DISTINCT FROM principal_id AND
            (binding->>'subscriber_id') IS NOT DISTINCT FROM subscriber_id AND
            (binding->>'notification_id') IS NOT DISTINCT FROM notification_id AND
            (binding->>'idempotency_key') IS NOT DISTINCT FROM idempotency_key AND
            (binding->>'delivery_id') IS NOT DISTINCT FROM delivery_id AND
            (binding->>'consumer_namespace') IS NOT DISTINCT FROM consumer_namespace),
        CONSTRAINT reporting_production_webhook_completion CHECK ((status = 'pending') = (completed_at IS NULL)),
        CONSTRAINT reporting_production_webhook_timestamps CHECK (completed_at >= fired_at),
        CONSTRAINT reporting_production_webhook_outcome CHECK (
            (status IN ('pending', 'timeout', 'connection_error')
                AND http_status_code IS NULL AND response_time_ms IS NULL)
            OR (status IN ('success', 'failed') AND http_status_code BETWEEN 100 AND 599
                AND http_status_code IS NOT NULL AND response_time_ms IS NOT NULL
                AND ((status = 'success') = (http_status_code BETWEEN 200 AND 299))))
    );
    CREATE INDEX IF NOT EXISTS reporting_production_webhook_activity_newest ON reporting_production_webhook_attempts
        (account_id, principal_id, fired_at DESC, notification_id DESC,
         idempotency_key DESC, subscriber_id DESC, attempt DESC, delivery_id DESC);
    CREATE INDEX IF NOT EXISTS reporting_production_webhook_activity_retention ON reporting_production_webhook_attempts
        (account_id, principal_id, completed_at) WHERE completed_at IS NOT NULL;

    -- Retention never resets a logical delivery's sequence, even after all
    -- terminal attempts are purged. Writers always lock parent, then head.
    CREATE OR REPLACE FUNCTION reporting_production_webhook_head_guard()
    RETURNS TRIGGER LANGUAGE plpgsql AS $head_guard$
    BEGIN
        IF TG_OP = 'DELETE' OR
           (TG_OP = 'INSERT' AND NEW.last_attempt <> 1) OR
           (TG_OP = 'UPDATE' AND (NEW.last_attempt <> OLD.last_attempt + 1 OR
            (to_jsonb(NEW) - 'last_attempt') <> (to_jsonb(OLD) - 'last_attempt'))) THEN
            RAISE EXCEPTION 'reporting activity counter must advance and be retained'
                USING ERRCODE = '23514';
        END IF;
        RETURN NEW;
    END;
    $head_guard$;
    IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgrelid = 'reporting_production_webhook_attempt_heads'::regclass
                   AND tgname = 'reporting_production_webhook_head_guard' AND NOT tgisinternal) THEN
        CREATE TRIGGER reporting_production_webhook_head_guard BEFORE INSERT OR UPDATE OR DELETE
            ON reporting_production_webhook_attempt_heads FOR EACH ROW EXECUTE FUNCTION reporting_production_webhook_head_guard();
    END IF;

    CREATE OR REPLACE FUNCTION reporting_production_webhook_attempt_guard()
    RETURNS TRIGGER LANGUAGE plpgsql AS $guard$
    DECLARE
        delivery reporting_production_notification_deliveries;
    BEGIN
        IF TG_OP = 'DELETE' THEN
            IF OLD.completed_at IS NULL THEN
                RAISE EXCEPTION 'pending reporting activity must be retained' USING ERRCODE = '23514';
            END IF;
            RETURN OLD;
        END IF;
        IF TG_OP = 'UPDATE' AND (
            OLD.status <> 'pending' OR NEW.status = 'pending' OR
            (to_jsonb(NEW) - ARRAY['status','completed_at','http_status_code','response_time_ms']) <>
            (to_jsonb(OLD) - ARRAY['status','completed_at','http_status_code','response_time_ms'])) THEN
            RAISE EXCEPTION 'immutable reporting activity reservation' USING ERRCODE = '23514';
        END IF;
        -- The opaque reservation token fences terminalization in the UPDATE
        -- predicate. A known late response may finish its own reservation after
        -- lease expiry/reclaim; it must not require or modify the parent lease.
        IF TG_OP = 'UPDATE' THEN
            RETURN NEW;
        END IF;
        IF TG_OP = 'INSERT' AND NEW.status <> 'pending' THEN
            RAISE EXCEPTION 'reporting activity requires reservation' USING ERRCODE = '23514';
        END IF;
        SELECT * INTO delivery FROM reporting_production_notification_deliveries
            WHERE account_id = NEW.account_id AND principal_id = NEW.principal_id
            AND consumer_namespace = NEW.consumer_namespace AND delivery_id = NEW.delivery_id
            AND subscriber_id = NEW.subscriber_id AND notification_id = NEW.notification_id
            AND idempotency_key = NEW.idempotency_key
            AND state = 'leased' AND lease_token = NEW.lease_token
            AND lease_expires_at > coalesce(NEW.completed_at, NEW.fired_at) FOR UPDATE;
        IF NOT FOUND OR
           NEW.binding->>'account_id' IS DISTINCT FROM NEW.account_id OR
           NEW.binding->>'principal_id' IS DISTINCT FROM NEW.principal_id OR
           NEW.binding->>'subscriber_id' IS DISTINCT FROM NEW.subscriber_id OR
           NEW.binding->>'notification_id' IS DISTINCT FROM NEW.notification_id OR
           NEW.binding->>'idempotency_key' IS DISTINCT FROM NEW.idempotency_key OR
           NEW.binding->>'delivery_id' IS DISTINCT FROM NEW.delivery_id OR
           NEW.binding->>'consumer_namespace' IS DISTINCT FROM NEW.consumer_namespace OR
           NEW.binding->>'notification_type' IS DISTINCT FROM delivery.notification_type OR
           NEW.binding->>'body_sha256' IS DISTINCT FROM delivery.body_sha256 THEN
            RAISE EXCEPTION 'reporting activity lease or identity mismatch' USING ERRCODE = '23514';
        END IF;
        RETURN NEW;
    END;
    $guard$;
    IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgrelid = 'reporting_production_webhook_attempts'::regclass
                   AND tgname = 'reporting_production_webhook_attempt_guard' AND NOT tgisinternal) THEN
        CREATE TRIGGER reporting_production_webhook_attempt_guard BEFORE INSERT OR UPDATE OR DELETE
            ON reporting_production_webhook_attempts FOR EACH ROW EXECUTE FUNCTION reporting_production_webhook_attempt_guard();
    END IF;

    EXECUTE $function$
    CREATE OR REPLACE FUNCTION reporting_production_retained_guard() RETURNS trigger
    LANGUAGE plpgsql AS $body$
    BEGIN
        RAISE EXCEPTION 'reporting_materializer_evidence_immutable' USING ERRCODE='23514';
    END
    $body$ $function$;
    IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgrelid='reporting_production_status_boundaries'::regclass
        AND tgname='reporting_production_boundary_immutable') THEN
        CREATE TRIGGER reporting_production_boundary_immutable BEFORE UPDATE OR DELETE
            ON reporting_production_status_boundaries FOR EACH ROW
            EXECUTE FUNCTION reporting_production_retained_guard();
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgrelid='reporting_production_notification_events'::regclass
        AND tgname='reporting_production_event_immutable') THEN
        CREATE TRIGGER reporting_production_event_immutable BEFORE UPDATE OR DELETE
            ON reporting_production_notification_events FOR EACH ROW
            EXECUTE FUNCTION reporting_production_retained_guard();
    END IF;

    EXECUTE $function$
    CREATE OR REPLACE FUNCTION reporting_production_work_guard() RETURNS trigger
    LANGUAGE plpgsql AS $body$
    DECLARE a JSONB; result RECORD; activation RECORD;
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
        SELECT activated_at, policy INTO activation FROM reporting_production_accounts
            WHERE account_id=NEW.account_id;
        IF NOT FOUND OR NEW.admission_epoch <> 2 OR NEW.imported
            OR (a->>'created_at')::timestamptz < activation.activated_at
            OR NOT (activation.policy->'verification_keys' ? NEW.verification_key_sha256)
            OR (activation.policy->>'notifications_enabled')::boolean
                IS DISTINCT FROM NEW.notifications_enabled THEN
            RAISE EXCEPTION 'reporting_production_admission_required' USING ERRCODE='23514';
        END IF;
        IF TG_OP='INSERT' AND EXISTS (SELECT 1 FROM reporting_materializer_work previous_work
            WHERE previous_work.account_id=NEW.account_id AND previous_work.consumer_id=NEW.consumer_id
                AND (previous_work.reporting_materialization_id=NEW.reporting_materialization_id OR
                    (previous_work.state='pending' AND previous_work.delivery_config_id=NEW.delivery_config_id
                     AND previous_work.delivery_config_version=NEW.delivery_config_version
                     AND previous_work.reporting_obligation_id=NEW.reporting_obligation_id))) THEN
            RAISE EXCEPTION 'reporting_production_historical_work_conflict' USING ERRCODE='23514';
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
            IF NOT EXISTS (SELECT 1 FROM reporting_production_status_boundaries b
                WHERE b.account_id=NEW.account_id AND b.consumer_id=NEW.consumer_id
                    AND b.reporting_materialization_id=NEW.reporting_materialization_id
                    AND b.as_of=NEW.acknowledged_at
                    AND b.as_of=(result.payload->>'completed_at')::timestamptz) THEN
                RAISE EXCEPTION 'reporting_materializer_capture_required' USING ERRCODE='23514';
            END IF;
            IF NEW.notifications_enabled AND result.payload->>'status' <> 'failed'
                AND NOT EXISTS (SELECT 1 FROM reporting_production_notification_events e
                    WHERE e.account_id=NEW.account_id AND e.consumer_namespace=NEW.consumer_id
                    AND e.reporting_materialization_id=NEW.reporting_materialization_id
                    AND e.admission_epoch=NEW.admission_epoch) THEN
                RAISE EXCEPTION 'reporting_materializer_event_required' USING ERRCODE='23514';
            END IF;
        END IF;
        RETURN NEW;
    END
    $body$ $function$;

    IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgrelid='reporting_production_work'::regclass
        AND tgname='reporting_production_guard') THEN
        CREATE TRIGGER reporting_production_guard BEFORE INSERT OR UPDATE OR DELETE
            ON reporting_production_work FOR EACH ROW
            EXECUTE FUNCTION reporting_production_work_guard();
    END IF;
    CREATE OR REPLACE FUNCTION reporting_production_old_reservation_guard() RETURNS trigger
    LANGUAGE plpgsql AS $function$
    DECLARE activation TIMESTAMPTZ; attempt JSONB;
    BEGIN
        PERFORM pg_advisory_xact_lock(hashtext('adcp.reporting:' || NEW.account_id));
        SELECT activated_at INTO activation FROM reporting_production_accounts
            WHERE account_id=NEW.account_id;
        IF FOUND THEN
            SELECT payload INTO attempt FROM reporting_reconciliation_records
                WHERE account_id=NEW.account_id AND consumer_id=NEW.consumer_id
                    AND namespace='materialization_attempt'
                    AND record_id=NEW.reporting_materialization_id;
            IF NOT NEW.imported OR attempt IS NULL
                OR (attempt->>'created_at')::timestamptz >= activation THEN
                RAISE EXCEPTION 'reporting_production_old_worker_fenced' USING ERRCODE='23514';
            END IF;
        END IF;
        RETURN NEW;
    END
    $function$;
    IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgrelid='reporting_materializer_work'::regclass
        AND tgname='reporting_production_old_reservation_guard') THEN
        CREATE TRIGGER reporting_production_old_reservation_guard BEFORE INSERT
            ON reporting_materializer_work FOR EACH ROW
            EXECUTE FUNCTION reporting_production_old_reservation_guard();
    END IF;
    CREATE TABLE IF NOT EXISTS reporting_production_generations (
        account_id TEXT COLLATE "C" NOT NULL,
        delivery_config_id TEXT COLLATE "C" NOT NULL,
        delivery_config_version INTEGER NOT NULL,
        producer_key TEXT COLLATE "C" NOT NULL CHECK (producer_key ~ '^[0-9a-f]{64}$'),
        source_binding JSONB NOT NULL CHECK (jsonb_typeof(source_binding)='object'),
        PRIMARY KEY (account_id,delivery_config_id,delivery_config_version),
        FOREIGN KEY (account_id,delivery_config_id,delivery_config_version)
            REFERENCES reporting_configurations
    );
    CREATE INDEX IF NOT EXISTS reporting_production_source_generations
        ON reporting_production_generations(producer_key,account_id,delivery_config_id,delivery_config_version);
    IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgrelid='reporting_production_generations'::regclass
        AND tgname='reporting_production_generation_immutable') THEN
        CREATE TRIGGER reporting_production_generation_immutable BEFORE UPDATE OR DELETE
            ON reporting_production_generations FOR EACH ROW
            EXECUTE FUNCTION reporting_receipt_ingestion_immutable();
    END IF;
    CREATE TABLE IF NOT EXISTS reporting_production_destination_bindings (
        account_id TEXT COLLATE "C" NOT NULL,
        consumer_id TEXT COLLATE "C" NOT NULL,
        delivery_config_id TEXT COLLATE "C" NOT NULL,
        delivery_config_version INTEGER NOT NULL,
        method JSONB NOT NULL CHECK (jsonb_typeof(method)='object'),
        PRIMARY KEY (account_id,consumer_id,delivery_config_id,delivery_config_version),
        FOREIGN KEY (account_id,delivery_config_id,delivery_config_version)
            REFERENCES reporting_production_generations
    );
    IF NOT EXISTS (SELECT 1 FROM pg_trigger
        WHERE tgrelid='reporting_production_destination_bindings'::regclass
        AND tgname='reporting_production_destination_immutable') THEN
        CREATE TRIGGER reporting_production_destination_immutable BEFORE UPDATE OR DELETE
            ON reporting_production_destination_bindings FOR EACH ROW
            EXECUTE FUNCTION reporting_receipt_ingestion_immutable();
    END IF;
END
$production$;

-- Global due sampling for the owned optional workers; account claims retain
-- the original SDK outbox transactions and recipient ordering.
DO $production_delivery_discovery$
BEGIN
    PERFORM pg_advisory_xact_lock(hashtext('adcp.reporting.schema'), hashtext(current_schema()));
    CREATE INDEX IF NOT EXISTS reporting_production_core_expansions_due
        ON reporting_notification_expansions (due_at,account_id) WHERE state IN ('pending','leased');
    CREATE INDEX IF NOT EXISTS reporting_production_core_deliveries_due
        ON reporting_notification_deliveries (due_at,account_id) WHERE state IN ('pending','leased');
    CREATE INDEX IF NOT EXISTS reporting_production_status_expansions_due
        ON reporting_projection_notification_expansions (due_at,account_id) WHERE state IN ('pending','leased');
    CREATE INDEX IF NOT EXISTS reporting_production_status_deliveries_due
        ON reporting_projection_notification_deliveries (due_at,account_id) WHERE state IN ('pending','leased');
    CREATE INDEX IF NOT EXISTS reporting_production_ready_expansions_due
        ON reporting_production_notification_expansions (due_at,account_id) WHERE state IN ('pending','leased');
    CREATE INDEX IF NOT EXISTS reporting_production_ready_deliveries_due
        ON reporting_production_notification_deliveries (due_at,account_id) WHERE state IN ('pending','leased');
END
$production_delivery_discovery$;

-- Generation-scoped producer closing and unfinished acquisition work. The
-- cursor never replaces original obligations, revision history or source keys.
DO $production_source_progress$
BEGIN
    PERFORM pg_advisory_xact_lock(hashtext('adcp.reporting.schema'), hashtext(current_schema()));
    -- Rejected admitted generations advance through the bounded discovery
    -- window without acquiring an ordinary lease or changing frozen bindings.
    CREATE TABLE IF NOT EXISTS reporting_production_source_probe_turns (
        account_id TEXT COLLATE "C" NOT NULL,
        delivery_config_id TEXT COLLATE "C" NOT NULL,
        delivery_config_version INTEGER NOT NULL,
        probe_turn BIGINT NOT NULL CHECK (probe_turn>0),
        PRIMARY KEY(account_id,delivery_config_id,delivery_config_version),
        FOREIGN KEY(account_id,delivery_config_id,delivery_config_version)
            REFERENCES reporting_production_generations
    );
    CREATE TABLE IF NOT EXISTS reporting_production_source_progress (
        account_id TEXT COLLATE "C" NOT NULL,
        delivery_config_id TEXT COLLATE "C" NOT NULL,
        delivery_config_version INTEGER NOT NULL,
        closed_through TIMESTAMPTZ,
        acquisition_turn BIGINT NOT NULL DEFAULT 0 CHECK (acquisition_turn>=0),
        PRIMARY KEY(account_id,delivery_config_id,delivery_config_version),
        FOREIGN KEY(account_id,delivery_config_id,delivery_config_version)
            REFERENCES reporting_production_generations
    );
    CREATE TABLE IF NOT EXISTS reporting_production_source_work (
        account_id TEXT COLLATE "C" NOT NULL,
        delivery_config_id TEXT COLLATE "C" NOT NULL,
        delivery_config_version INTEGER NOT NULL,
        reporting_obligation_id TEXT COLLATE "C" NOT NULL REFERENCES reporting_obligations,
        period_end TIMESTAMPTZ NOT NULL,
        acquisition_turn BIGINT NOT NULL DEFAULT 0 CHECK (acquisition_turn>=0),
        state TEXT COLLATE "C" NOT NULL DEFAULT 'pending'
            CHECK (state IN ('pending','settled','parked')),
        PRIMARY KEY(account_id,reporting_obligation_id),
        FOREIGN KEY(account_id,delivery_config_id,delivery_config_version)
            REFERENCES reporting_production_generations
    );
    CREATE INDEX IF NOT EXISTS reporting_production_source_pending
        ON reporting_production_source_work
            (account_id,delivery_config_id,delivery_config_version,acquisition_turn,reporting_obligation_id)
        INCLUDE(period_end) WHERE state='pending';

    CREATE OR REPLACE FUNCTION reporting_production_source_progress_guard() RETURNS trigger
    LANGUAGE plpgsql AS $function$
    BEGIN
        IF TG_OP='DELETE' OR (TG_OP='UPDATE' AND (
            (to_jsonb(NEW)-ARRAY['closed_through','acquisition_turn']) IS DISTINCT FROM
                (to_jsonb(OLD)-ARRAY['closed_through','acquisition_turn'])
            OR NEW.acquisition_turn < OLD.acquisition_turn
            OR (OLD.closed_through IS NOT NULL AND
                (NEW.closed_through IS NULL OR NEW.closed_through < OLD.closed_through)))) THEN
            RAISE EXCEPTION 'reporting_production_source_progress_immutable' USING ERRCODE='23514';
        END IF;
        IF NEW.closed_through IS NOT NULL AND NOT EXISTS (
            SELECT 1 FROM reporting_obligations o WHERE o.account_id=NEW.account_id
                AND o.delivery_config_id=NEW.delivery_config_id
                AND o.delivery_config_version=NEW.delivery_config_version
                AND o.period_end=NEW.closed_through
        ) THEN
            RAISE EXCEPTION 'reporting_production_source_obligation_required' USING ERRCODE='23514';
        END IF;
        RETURN NEW;
    END
    $function$;
    CREATE OR REPLACE FUNCTION reporting_production_source_work_guard() RETURNS trigger
    LANGUAGE plpgsql AS $function$
    BEGIN
        IF TG_OP='DELETE' OR (TG_OP='UPDATE' AND (
            (to_jsonb(NEW)-ARRAY['state','acquisition_turn']) IS DISTINCT FROM
                (to_jsonb(OLD)-ARRAY['state','acquisition_turn'])
            OR NEW.acquisition_turn < OLD.acquisition_turn)) THEN
            RAISE EXCEPTION 'reporting_production_source_identity_immutable' USING ERRCODE='23514';
        END IF;
        IF NOT EXISTS (SELECT 1 FROM reporting_obligations o
            WHERE o.reporting_obligation_id=NEW.reporting_obligation_id
                AND o.account_id=NEW.account_id AND o.delivery_config_id=NEW.delivery_config_id
                AND o.delivery_config_version=NEW.delivery_config_version AND o.period_end=NEW.period_end) THEN
            RAISE EXCEPTION 'reporting_production_source_obligation_required' USING ERRCODE='23514';
        END IF;
        RETURN NEW;
    END
    $function$;
    CREATE OR REPLACE FUNCTION reporting_production_source_dirty() RETURNS trigger
    LANGUAGE plpgsql AS $function$
    BEGIN
        -- SDK domain writers already hold the account lock before their row
        -- mutation. New triggers retain that order and do no external I/O.
        INSERT INTO reporting_production_source_work
            (account_id,delivery_config_id,delivery_config_version,reporting_obligation_id,period_end)
        SELECT o.account_id,o.delivery_config_id,o.delivery_config_version,
            o.reporting_obligation_id,o.period_end FROM reporting_obligations o
        JOIN reporting_production_generations g
            USING(account_id,delivery_config_id,delivery_config_version)
        WHERE o.reporting_obligation_id=NEW.reporting_obligation_id
            AND o.account_id=NEW.account_id
        ON CONFLICT(account_id,reporting_obligation_id) DO UPDATE SET state='pending';
        RETURN NEW;
    END
    $function$;
    IF NOT EXISTS (SELECT 1 FROM pg_trigger
        WHERE tgrelid='reporting_production_source_progress'::regclass
        AND tgname='reporting_production_source_progress_guard') THEN
        CREATE TRIGGER reporting_production_source_progress_guard BEFORE INSERT OR UPDATE OR DELETE
            ON reporting_production_source_progress FOR EACH ROW
            EXECUTE FUNCTION reporting_production_source_progress_guard();
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_trigger
        WHERE tgrelid='reporting_production_source_work'::regclass
        AND tgname='reporting_production_source_work_guard') THEN
        CREATE TRIGGER reporting_production_source_work_guard BEFORE INSERT OR UPDATE OR DELETE
            ON reporting_production_source_work FOR EACH ROW
            EXECUTE FUNCTION reporting_production_source_work_guard();
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_trigger
        WHERE tgrelid='reporting_obligations'::regclass
        AND tgname='reporting_production_source_obligation') THEN
        CREATE TRIGGER reporting_production_source_obligation AFTER INSERT ON reporting_obligations
            FOR EACH ROW EXECUTE FUNCTION reporting_production_source_dirty();
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_trigger
        WHERE tgrelid='reporting_revisions'::regclass
        AND tgname='reporting_production_source_revision') THEN
        CREATE TRIGGER reporting_production_source_revision AFTER INSERT OR UPDATE OF readable
            ON reporting_revisions FOR EACH ROW EXECUTE FUNCTION reporting_production_source_dirty();
    END IF;
END
$production_source_progress$;
