-- #1168B: additive objects only. An unmodified #1168A binary can keep working.
-- No foreign key/index/trigger is added to an A table, and no evidence is backfilled.
DO $activity$
BEGIN
    PERFORM pg_advisory_xact_lock(hashtext('adcp.reporting.schema'), hashtext(current_schema()));
    PERFORM account_id, principal_id, body_sha256 FROM reporting_notification_deliveries LIMIT 0;

    CREATE TABLE IF NOT EXISTS reporting_webhook_attempt_heads (
        account_id TEXT COLLATE "C" NOT NULL,
        principal_id TEXT COLLATE "C" NOT NULL CHECK (length(principal_id) > 0),
        subscriber_id TEXT COLLATE "C" NOT NULL,
        idempotency_key TEXT COLLATE "C" NOT NULL,
        last_attempt BIGINT NOT NULL CHECK (last_attempt > 0),
        PRIMARY KEY (account_id, principal_id, subscriber_id, idempotency_key)
    );
    CREATE TABLE IF NOT EXISTS reporting_webhook_attempts (
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
        url TEXT NOT NULL CONSTRAINT reporting_webhook_url_safe
            CHECK (length(url) <= 8192 AND url !~ '[?#@]' AND url ~ '^https?://'),
        payload_size_bytes BIGINT NOT NULL CHECK (payload_size_bytes >= 0),
        status TEXT NOT NULL DEFAULT 'pending' CHECK
            (status IN ('pending', 'success', 'failed', 'timeout', 'connection_error')),
        completed_at TIMESTAMPTZ,
        http_status_code INTEGER,
        response_time_ms BIGINT CHECK (response_time_ms >= 0),
        PRIMARY KEY (account_id, principal_id, subscriber_id, idempotency_key, attempt),
        UNIQUE (account_id, principal_id, delivery_id, lease_token),
        FOREIGN KEY (account_id, principal_id, subscriber_id, idempotency_key)
            REFERENCES reporting_webhook_attempt_heads
                (account_id, principal_id, subscriber_id, idempotency_key),
        CONSTRAINT reporting_webhook_identity CHECK (
            (binding->>'account_id') IS NOT DISTINCT FROM account_id AND
            (binding->>'principal_id') IS NOT DISTINCT FROM principal_id AND
            (binding->>'subscriber_id') IS NOT DISTINCT FROM subscriber_id AND
            (binding->>'notification_id') IS NOT DISTINCT FROM notification_id AND
            (binding->>'idempotency_key') IS NOT DISTINCT FROM idempotency_key AND
            (binding->>'delivery_id') IS NOT DISTINCT FROM delivery_id AND
            (binding->>'consumer_namespace') IS NOT DISTINCT FROM consumer_namespace),
        CONSTRAINT reporting_webhook_completion CHECK ((status = 'pending') = (completed_at IS NULL)),
        CONSTRAINT reporting_webhook_timestamps CHECK (completed_at >= fired_at),
        CONSTRAINT reporting_webhook_outcome CHECK (
            (status IN ('pending', 'timeout', 'connection_error')
                AND http_status_code IS NULL AND response_time_ms IS NULL)
            OR (status IN ('success', 'failed') AND http_status_code BETWEEN 100 AND 599
                AND http_status_code IS NOT NULL AND response_time_ms IS NOT NULL
                AND ((status = 'success') = (http_status_code BETWEEN 200 AND 299))))
    );
    CREATE INDEX IF NOT EXISTS reporting_webhook_activity_newest ON reporting_webhook_attempts
        (account_id, principal_id, fired_at DESC, notification_id DESC,
         idempotency_key DESC, subscriber_id DESC, attempt DESC, delivery_id DESC);
    CREATE INDEX IF NOT EXISTS reporting_webhook_activity_retention ON reporting_webhook_attempts
        (account_id, principal_id, completed_at) WHERE completed_at IS NOT NULL;

    -- Retention never resets a logical delivery's sequence, even after all
    -- terminal attempts are purged. Writers always lock parent, then head.
    CREATE OR REPLACE FUNCTION reporting_webhook_head_guard()
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
    IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgrelid = 'reporting_webhook_attempt_heads'::regclass
                   AND tgname = 'reporting_webhook_head_guard' AND NOT tgisinternal) THEN
        CREATE TRIGGER reporting_webhook_head_guard BEFORE INSERT OR UPDATE OR DELETE
            ON reporting_webhook_attempt_heads FOR EACH ROW EXECUTE FUNCTION reporting_webhook_head_guard();
    END IF;

    CREATE OR REPLACE FUNCTION reporting_webhook_attempt_guard()
    RETURNS TRIGGER LANGUAGE plpgsql AS $guard$
    DECLARE
        delivery reporting_notification_deliveries;
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
        SELECT * INTO delivery FROM reporting_notification_deliveries
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
    IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgrelid = 'reporting_webhook_attempts'::regclass
                   AND tgname = 'reporting_webhook_attempt_guard' AND NOT tgisinternal) THEN
        CREATE TRIGGER reporting_webhook_attempt_guard BEFORE INSERT OR UPDATE OR DELETE
            ON reporting_webhook_attempts FOR EACH ROW EXECUTE FUNCTION reporting_webhook_attempt_guard();
    END IF;
END;
$activity$;
