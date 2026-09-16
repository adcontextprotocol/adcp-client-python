-- #1168C is opt-in. Never replace an A/B function or extend its event enums.
-- The only new A/B-table object is the additive dirty-boundary capture trigger.
DO $status$
BEGIN
    PERFORM pg_advisory_xact_lock(hashtext('adcp.reporting.schema'), hashtext(current_schema()));
    PERFORM account_id, sequence FROM reporting_status_dirty LIMIT 0;

    CREATE TABLE IF NOT EXISTS reporting_status_accounts (
        account_id TEXT COLLATE "C" PRIMARY KEY,
        baseline_complete BOOLEAN NOT NULL DEFAULT FALSE,
        baseline_highwater BIGINT NOT NULL DEFAULT 0 CHECK (baseline_highwater >= 0),
        dirty_sequence BIGINT NOT NULL DEFAULT 0 CHECK (dirty_sequence >= baseline_highwater),
        baseline_at TIMESTAMPTZ,
        policy JSONB NOT NULL DEFAULT '{}',
        replay_lifecycles JSONB NOT NULL DEFAULT '[]'
    );
    CREATE TABLE IF NOT EXISTS reporting_status_scope_checkpoints (
        account_id TEXT COLLATE "C" NOT NULL REFERENCES reporting_status_accounts,
        consumer_namespace TEXT COLLATE "C" NOT NULL,
        delivery_config_id TEXT COLLATE "C" NOT NULL,
        version BIGINT NOT NULL CHECK (version > 0),
        scope_kind TEXT COLLATE "C" NOT NULL CHECK (scope_kind IN ('configuration','obligation')),
        obligation_namespace TEXT COLLATE "C" NOT NULL,
        scope JSONB NOT NULL,
        fingerprint TEXT NOT NULL CHECK (fingerprint ~ '^[0-9a-f]{64}$'),
        generation BIGINT NOT NULL DEFAULT 0 CHECK (generation >= 0),
        snapshot JSONB NOT NULL,
        next_due_at TIMESTAMPTZ,
        source_sequence BIGINT NOT NULL CHECK (source_sequence >= 0),
        baseline BOOLEAN NOT NULL,
        publishable BOOLEAN NOT NULL,
        initialized BOOLEAN NOT NULL DEFAULT FALSE,
        lease_token TEXT,
        lease_expires_at TIMESTAMPTZ,
        PRIMARY KEY (account_id, consumer_namespace, delivery_config_id, version,
                     scope_kind, obligation_namespace),
        FOREIGN KEY (account_id, delivery_config_id, version) REFERENCES reporting_configurations
            (account_id, delivery_config_id, delivery_config_version),
        CHECK ((scope_kind = 'configuration') = (obligation_namespace = '')),
        CHECK ((lease_token IS NULL) = (lease_expires_at IS NULL)),
        CHECK ((scope->>'account_id') IS NOT DISTINCT FROM account_id),
        CHECK (coalesce(scope->>'consumer_id', '') = consumer_namespace),
        CHECK ((scope #>> '{generation_key,delivery_config_id}') IS NOT DISTINCT FROM delivery_config_id),
        CHECK ((scope #>> '{generation_key,account_id}') IS NOT DISTINCT FROM account_id),
        CHECK ((scope #>> '{generation_key,delivery_config_version}')::bigint IS NOT DISTINCT FROM version),
        CHECK (coalesce(scope->>'reporting_obligation_id', '') = obligation_namespace),
        CHECK (snapshot->>'health' IN ('waiting','healthy','delayed','action_required','complete'))
    );
    CREATE INDEX IF NOT EXISTS reporting_status_scope_due
        ON reporting_status_scope_checkpoints (account_id, next_due_at)
        WHERE next_due_at IS NOT NULL;

    CREATE TABLE IF NOT EXISTS reporting_status_boundary_writes (
        account_id TEXT COLLATE "C" NOT NULL,
        transaction_id TEXT COLLATE "C" NOT NULL,
        first_sequence BIGINT NOT NULL CHECK (first_sequence > 0),
        through BIGINT NOT NULL CHECK (through >= first_sequence),
        as_of TIMESTAMPTZ NOT NULL,
        PRIMARY KEY (account_id, transaction_id)
    );
    CREATE TABLE IF NOT EXISTS reporting_status_boundaries (
        account_id TEXT COLLATE "C" NOT NULL,
        transaction_id TEXT COLLATE "C" NOT NULL,
        first_sequence BIGINT NOT NULL,
        through BIGINT NOT NULL,
        as_of TIMESTAMPTZ NOT NULL,
        input JSONB NOT NULL,
        PRIMARY KEY (account_id, transaction_id),
        UNIQUE (account_id, through),
        FOREIGN KEY (account_id, transaction_id) REFERENCES reporting_status_boundary_writes
    );
    CREATE TABLE IF NOT EXISTS reporting_status_notification_events (
        account_id TEXT COLLATE "C" NOT NULL,
        notification_id TEXT COLLATE "C" NOT NULL,
        notification_type TEXT COLLATE "C" NOT NULL CHECK
            (notification_type = 'reporting.status_changed'),
        cause_kind TEXT COLLATE "C" NOT NULL CHECK
            (cause_kind = 'status_changed'),
        cause_id TEXT COLLATE "C" NOT NULL CHECK (length(cause_id) > 0),
        cause_generation BIGINT NOT NULL CHECK (cause_generation > 0),
        consumer_namespace TEXT COLLATE "C" NOT NULL,
        delivery_config_id TEXT COLLATE "C" NOT NULL,
        version BIGINT NOT NULL CHECK (version > 0),
        scope_kind TEXT COLLATE "C" NOT NULL CHECK (scope_kind IN ('configuration','obligation')),
        obligation_namespace TEXT COLLATE "C" NOT NULL,
        fingerprint TEXT NOT NULL CHECK (fingerprint ~ '^[0-9a-f]{64}$'),
        fired_at TIMESTAMPTZ NOT NULL,
        snapshot JSONB NOT NULL,
        PRIMARY KEY (account_id, consumer_namespace, notification_id),
        UNIQUE (account_id, consumer_namespace, delivery_config_id, version,
                scope_kind, obligation_namespace, cause_generation),
        FOREIGN KEY (account_id, consumer_namespace, delivery_config_id, version,
                     scope_kind, obligation_namespace)
            REFERENCES reporting_status_scope_checkpoints,
        CHECK ((scope_kind = 'configuration') = (obligation_namespace = '')),
        UNIQUE (account_id, consumer_namespace, notification_type,
                cause_kind, cause_id, cause_generation),
        UNIQUE (account_id, consumer_namespace, notification_id, cause_kind, cause_id, cause_generation),
        CHECK ((snapshot #>> '{cause,kind}') IS NOT DISTINCT FROM cause_kind),
        CHECK ((snapshot #>> '{cause,fingerprint}') IS NOT DISTINCT FROM fingerprint),
        CHECK ((snapshot #>> '{cause,checkpoint_generation}')::bigint IS NOT DISTINCT FROM cause_generation),
        CHECK ((snapshot #>> '{cause,scope,account_id}') IS NOT DISTINCT FROM account_id),
        CHECK ((snapshot #>> '{cause,scope,generation_key,account_id}') IS NOT DISTINCT FROM account_id),
        CHECK (coalesce(snapshot #>> '{cause,scope,consumer_id}', '') = consumer_namespace),
        CHECK ((snapshot #>> '{cause,scope,generation_key,delivery_config_id}') IS NOT DISTINCT FROM delivery_config_id),
        CHECK ((snapshot #>> '{cause,scope,generation_key,delivery_config_version}')::bigint IS NOT DISTINCT FROM version),
        CHECK (coalesce(snapshot #>> '{cause,scope,reporting_obligation_id}', '') = obligation_namespace),
        CHECK ((snapshot->>'account_id') IS NOT DISTINCT FROM account_id AND
               (snapshot->>'notification_id') IS NOT DISTINCT FROM notification_id),
        CHECK ((snapshot->>'cause_generation')::bigint IS NOT DISTINCT FROM cause_generation),
        CHECK ((snapshot->>'fired_at')::timestamptz IS NOT DISTINCT FROM fired_at)
    );
    CREATE TABLE IF NOT EXISTS reporting_status_notification_expansions (
        account_id TEXT COLLATE "C" NOT NULL,
        consumer_namespace TEXT COLLATE "C" NOT NULL DEFAULT '',
        notification_id TEXT COLLATE "C" NOT NULL,
        emission_generation BIGINT NOT NULL CHECK (emission_generation > 0),
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
        PRIMARY KEY (account_id, consumer_namespace, notification_id, emission_generation),
        FOREIGN KEY (account_id, consumer_namespace, notification_id)
            REFERENCES reporting_status_notification_events (account_id, consumer_namespace, notification_id)
    );
    CREATE INDEX IF NOT EXISTS reporting_status_notification_expansions_due
        ON reporting_status_notification_expansions (account_id, due_at)
        WHERE state IN ('pending', 'leased');

    CREATE TABLE IF NOT EXISTS reporting_status_notification_deliveries (
        account_id TEXT COLLATE "C" NOT NULL,
        delivery_id TEXT COLLATE "C" NOT NULL,
        subscriber_id TEXT COLLATE "C" NOT NULL,
        principal_id TEXT COLLATE "C" NOT NULL,
        notification_id TEXT COLLATE "C" NOT NULL,
        notification_type TEXT COLLATE "C" NOT NULL CHECK (notification_type = 'reporting.status_changed'),
        emission_generation BIGINT NOT NULL CHECK (emission_generation > 0),
        idempotency_key TEXT COLLATE "C" NOT NULL,
        destination_sha256 TEXT NOT NULL,
        subscription_fingerprint TEXT NOT NULL,
        signing_scope_id TEXT COLLATE "C",
        cause_kind TEXT COLLATE "C" NOT NULL CHECK (cause_kind = 'status_changed'),
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
            REFERENCES reporting_status_notification_events
                (account_id, consumer_namespace, notification_id, cause_kind, cause_id, cause_generation),
        FOREIGN KEY (account_id, consumer_namespace, notification_id, emission_generation)
            REFERENCES reporting_status_notification_expansions
                (account_id, consumer_namespace, notification_id, emission_generation)
    );
    CREATE INDEX IF NOT EXISTS reporting_status_notification_deliveries_due
        ON reporting_status_notification_deliveries (account_id, due_at)
        WHERE state IN ('pending', 'leased');


    CREATE TABLE IF NOT EXISTS reporting_status_webhook_attempt_heads (
        account_id TEXT COLLATE "C" NOT NULL,
        consumer_namespace TEXT COLLATE "C" NOT NULL,
        principal_id TEXT COLLATE "C" NOT NULL CHECK (length(principal_id) > 0),
        subscriber_id TEXT COLLATE "C" NOT NULL,
        idempotency_key TEXT COLLATE "C" NOT NULL,
        last_attempt BIGINT NOT NULL CHECK (last_attempt > 0),
        PRIMARY KEY (account_id, consumer_namespace, principal_id, subscriber_id, idempotency_key)
    );
    CREATE TABLE IF NOT EXISTS reporting_status_webhook_attempts (
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
        url TEXT NOT NULL CONSTRAINT reporting_status_webhook_url_safe
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
            REFERENCES reporting_status_notification_deliveries,
        FOREIGN KEY (account_id, consumer_namespace, principal_id, subscriber_id, idempotency_key)
            REFERENCES reporting_status_webhook_attempt_heads
                (account_id, consumer_namespace, principal_id, subscriber_id, idempotency_key),
        CONSTRAINT reporting_status_webhook_identity CHECK (
            (binding->>'account_id') IS NOT DISTINCT FROM account_id AND
            (binding->>'principal_id') IS NOT DISTINCT FROM principal_id AND
            (binding->>'subscriber_id') IS NOT DISTINCT FROM subscriber_id AND
            (binding->>'notification_id') IS NOT DISTINCT FROM notification_id AND
            (binding->>'idempotency_key') IS NOT DISTINCT FROM idempotency_key AND
            (binding->>'delivery_id') IS NOT DISTINCT FROM delivery_id AND
            (binding->>'consumer_namespace') IS NOT DISTINCT FROM consumer_namespace),
        CONSTRAINT reporting_status_webhook_completion CHECK ((status = 'pending') = (completed_at IS NULL)),
        CONSTRAINT reporting_status_webhook_timestamps CHECK (completed_at >= fired_at),
        CONSTRAINT reporting_status_webhook_outcome CHECK (
            (status IN ('pending', 'timeout', 'connection_error')
                AND http_status_code IS NULL AND response_time_ms IS NULL)
            OR (status IN ('success', 'failed') AND http_status_code BETWEEN 100 AND 599
                AND http_status_code IS NOT NULL AND response_time_ms IS NOT NULL
                AND ((status = 'success') = (http_status_code BETWEEN 200 AND 299))))
    );
    CREATE INDEX IF NOT EXISTS reporting_status_webhook_activity_newest ON reporting_status_webhook_attempts
        (account_id, principal_id, fired_at DESC, notification_id DESC,
         idempotency_key DESC, subscriber_id DESC, attempt DESC, delivery_id DESC);
    CREATE INDEX IF NOT EXISTS reporting_status_webhook_activity_retention ON reporting_status_webhook_attempts
        (account_id, principal_id, completed_at) WHERE completed_at IS NOT NULL;

    -- Retention never resets a logical delivery's sequence, even after all
    -- terminal attempts are purged. Writers always lock parent, then head.
    CREATE OR REPLACE FUNCTION reporting_status_webhook_head_guard()
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
    IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgrelid = 'reporting_status_webhook_attempt_heads'::regclass
                   AND tgname = 'reporting_status_webhook_head_guard' AND NOT tgisinternal) THEN
        CREATE TRIGGER reporting_status_webhook_head_guard BEFORE INSERT OR UPDATE OR DELETE
            ON reporting_status_webhook_attempt_heads FOR EACH ROW EXECUTE FUNCTION reporting_status_webhook_head_guard();
    END IF;

    CREATE OR REPLACE FUNCTION reporting_status_webhook_attempt_guard()
    RETURNS TRIGGER LANGUAGE plpgsql AS $guard$
    DECLARE
        delivery reporting_status_notification_deliveries;
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
        SELECT * INTO delivery FROM reporting_status_notification_deliveries
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
    IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgrelid = 'reporting_status_webhook_attempts'::regclass
                   AND tgname = 'reporting_status_webhook_attempt_guard' AND NOT tgisinternal) THEN
        CREATE TRIGGER reporting_status_webhook_attempt_guard BEFORE INSERT OR UPDATE OR DELETE
            ON reporting_status_webhook_attempts FOR EACH ROW EXECUTE FUNCTION reporting_status_webhook_attempt_guard();
    END IF;

    CREATE OR REPLACE FUNCTION reporting_status_projection_input(owner text, at_time timestamptz)
    RETURNS JSONB LANGUAGE sql STABLE AS $input$
        SELECT jsonb_build_object(
            'account_id', owner, 'as_of', at_time,
            'configurations', coalesce((SELECT jsonb_agg(to_jsonb(r)) FROM reporting_configurations r WHERE account_id=owner), '[]'),
            'obligations', coalesce((SELECT jsonb_agg(to_jsonb(r)) FROM reporting_obligations r WHERE account_id=owner), '[]'),
            'revisions', coalesce((SELECT jsonb_agg(to_jsonb(r)) FROM reporting_revisions r WHERE account_id=owner), '[]'),
            'statuses', coalesce((SELECT jsonb_agg(to_jsonb(r)) FROM reporting_consumer_statuses r WHERE account_id=owner), '[]'),
            'lifecycles', coalesce((SELECT jsonb_agg(to_jsonb(r)) FROM reporting_issue_lifecycle r WHERE account_id=owner), '[]'),
            'issue_scopes', coalesce((SELECT jsonb_agg(to_jsonb(r)) FROM reporting_issue_status_scopes r WHERE account_id=owner), '[]'),
            'adjustments', coalesce((SELECT jsonb_agg(to_jsonb(r)) FROM reporting_adjustments r WHERE account_id=owner), '[]'),
            'changes', coalesce((SELECT jsonb_agg(to_jsonb(r)) FROM reporting_ledger_changes r WHERE account_id=owner), '[]'),
            'consumers', coalesce((SELECT jsonb_agg(to_jsonb(r)) FROM reporting_reconciliation_heads r WHERE account_id=owner), '[]')
        )
    $input$;

    CREATE OR REPLACE FUNCTION reporting_status_boundary_mark()
    RETURNS TRIGGER LANGUAGE plpgsql AS $mark$
    BEGIN
        IF current_setting('adcp.status_lifecycle', true) = 'on' THEN
            RETURN NEW;
        END IF;
        IF EXISTS (SELECT 1 FROM reporting_status_boundaries
                   WHERE account_id=NEW.account_id AND transaction_id=pg_current_xact_id()::text) THEN
            RAISE EXCEPTION 'status boundary was already captured' USING ERRCODE='23514';
        END IF;
        INSERT INTO reporting_status_boundary_writes
            (account_id, transaction_id, first_sequence, through, as_of)
            VALUES (NEW.account_id, pg_current_xact_id()::text, NEW.sequence, NEW.sequence,
                    (NEW.snapshot->>'changed_at')::timestamptz)
        ON CONFLICT (account_id, transaction_id) DO UPDATE
            SET through=EXCLUDED.through, as_of=EXCLUDED.as_of;
        RETURN NEW;
    END
    $mark$;
    CREATE OR REPLACE FUNCTION reporting_status_boundary_capture()
    RETURNS TRIGGER LANGUAGE plpgsql AS $capture$
    DECLARE boundary reporting_status_boundary_writes; captured_at TIMESTAMPTZ;
    BEGIN
        SELECT * INTO STRICT boundary FROM reporting_status_boundary_writes
            WHERE account_id=NEW.account_id AND transaction_id=NEW.transaction_id;
        captured_at := CASE WHEN current_setting('adcp.status_clock_override', true)='on'
                            THEN boundary.as_of ELSE clock_timestamp() END;
        INSERT INTO reporting_status_boundaries
            (account_id, transaction_id, first_sequence, through, as_of, input)
            VALUES (boundary.account_id, boundary.transaction_id, boundary.first_sequence,
                    boundary.through, captured_at,
                    reporting_status_projection_input(boundary.account_id, captured_at));
        RETURN NEW;
    END
    $capture$;
    CREATE OR REPLACE FUNCTION reporting_status_scope_guard()
    RETURNS TRIGGER LANGUAGE plpgsql AS $scope_guard$
    DECLARE owner reporting_obligations;
    BEGIN
        IF TG_OP='UPDATE' AND (
            ROW(NEW.account_id, NEW.consumer_namespace, NEW.delivery_config_id, NEW.version,
                NEW.scope_kind, NEW.obligation_namespace) IS DISTINCT FROM
            ROW(OLD.account_id, OLD.consumer_namespace, OLD.delivery_config_id, OLD.version,
                OLD.scope_kind, OLD.obligation_namespace)
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
                WHERE account_id=NEW.account_id AND reporting_obligation_id=NEW.obligation_namespace;
            IF NOT FOUND OR owner.delivery_config_id <> NEW.delivery_config_id
                OR owner.delivery_config_version <> NEW.version THEN
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
    CREATE OR REPLACE FUNCTION reporting_status_event_guard()
    RETURNS TRIGGER LANGUAGE plpgsql AS $event_guard$
    DECLARE checkpoint reporting_status_scope_checkpoints; ids jsonb;
    BEGIN
        SELECT * INTO checkpoint FROM reporting_status_scope_checkpoints
            WHERE account_id=NEW.account_id AND consumer_namespace=NEW.consumer_namespace
            AND delivery_config_id=NEW.delivery_config_id AND version=NEW.version
            AND scope_kind=NEW.scope_kind AND obligation_namespace=NEW.obligation_namespace;
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
    IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgrelid='reporting_status_scope_checkpoints'::regclass
                   AND tgname='reporting_status_scope_guard' AND NOT tgisinternal) THEN
        CREATE TRIGGER reporting_status_scope_guard BEFORE INSERT OR UPDATE
            ON reporting_status_scope_checkpoints FOR EACH ROW EXECUTE FUNCTION reporting_status_scope_guard();
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgrelid='reporting_status_notification_events'::regclass
                   AND tgname='reporting_status_event_guard' AND NOT tgisinternal) THEN
        CREATE TRIGGER reporting_status_event_guard BEFORE INSERT
            ON reporting_status_notification_events FOR EACH ROW EXECUTE FUNCTION reporting_status_event_guard();
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgrelid='reporting_status_dirty'::regclass
                   AND tgname='reporting_status_boundary_mark' AND NOT tgisinternal) THEN
        CREATE TRIGGER reporting_status_boundary_mark AFTER INSERT ON reporting_status_dirty
            FOR EACH ROW EXECUTE FUNCTION reporting_status_boundary_mark();
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgrelid='reporting_status_boundary_writes'::regclass
                   AND tgname='reporting_status_boundary_capture' AND NOT tgisinternal) THEN
        CREATE CONSTRAINT TRIGGER reporting_status_boundary_capture
            AFTER INSERT ON reporting_status_boundary_writes DEFERRABLE INITIALLY DEFERRED
            FOR EACH ROW EXECUTE FUNCTION reporting_status_boundary_capture();
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgrelid='reporting_status_boundaries'::regclass
                   AND tgname='reporting_status_boundary_immutable' AND NOT tgisinternal) THEN
        CREATE TRIGGER reporting_status_boundary_immutable BEFORE UPDATE OR DELETE
            ON reporting_status_boundaries FOR EACH ROW EXECUTE FUNCTION reporting_notification_immutable();
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgrelid='reporting_status_notification_events'::regclass
                   AND tgname='reporting_status_event_immutable' AND NOT tgisinternal) THEN
        CREATE TRIGGER reporting_status_event_immutable BEFORE UPDATE OR DELETE
            ON reporting_status_notification_events FOR EACH ROW EXECUTE FUNCTION reporting_notification_immutable();
    END IF;
END
$status$;
