-- Standalone, additive storage for inline-source objects and committed replay seals.
-- The caller must execute this migration and readiness validation in one transaction.
DO $migration$
BEGIN
    PERFORM pg_advisory_xact_lock(hashtext('adcp.reporting.inline_storage.schema'));

    CREATE TABLE IF NOT EXISTS reporting_inline_objects (
        account_id TEXT COLLATE "C" NOT NULL,
        payload_sha256 TEXT COLLATE "C" NOT NULL,
        payload BYTEA NOT NULL,
        CONSTRAINT reporting_inline_objects_pk PRIMARY KEY (account_id, payload_sha256),
        CONSTRAINT reporting_inline_objects_account CHECK (char_length(account_id) BETWEEN 1 AND 255),
        CONSTRAINT reporting_inline_objects_digest CHECK (payload_sha256 ~ '^[a-f0-9]{64}$'),
        CONSTRAINT reporting_inline_objects_size CHECK (octet_length(payload) <= 16777216),
        CONSTRAINT reporting_inline_objects_bytes CHECK (payload_sha256 = encode(sha256(payload), 'hex'))
    );

    CREATE TABLE IF NOT EXISTS reporting_inline_seals (
        account_id TEXT COLLATE "C" NOT NULL,
        source_execution_key TEXT COLLATE "C" NOT NULL,
        staged_commit_ref TEXT COLLATE "C" NOT NULL,
        manifest_sha256 TEXT COLLATE "C" NOT NULL,
        byte_count INTEGER NOT NULL,
        manifest BYTEA NOT NULL,
        CONSTRAINT reporting_inline_seals_pk PRIMARY KEY (account_id, source_execution_key),
        CONSTRAINT reporting_inline_seals_account CHECK (char_length(account_id) BETWEEN 1 AND 255),
        CONSTRAINT reporting_inline_seals_key CHECK (source_execution_key ~ '^[A-Za-z0-9_.:-]{8,255}$'),
        CONSTRAINT reporting_inline_seals_ref CHECK (staged_commit_ref ~ '^[A-Za-z0-9][A-Za-z0-9_.-]{0,254}$'),
        CONSTRAINT reporting_inline_seals_digest CHECK (manifest_sha256 ~ '^[a-f0-9]{64}$'),
        CONSTRAINT reporting_inline_seals_size CHECK (byte_count BETWEEN 1 AND 1048576),
        CONSTRAINT reporting_inline_seals_count CHECK (byte_count = octet_length(manifest)),
        CONSTRAINT reporting_inline_seals_bytes CHECK (manifest_sha256 = encode(sha256(manifest), 'hex')),
        CONSTRAINT reporting_inline_seals_account_binding CHECK (
            (convert_from(manifest, 'UTF8')::jsonb #>> '{identity,account_id}') IS NOT DISTINCT FROM account_id),
        CONSTRAINT reporting_inline_seals_key_binding CHECK (
            (convert_from(manifest, 'UTF8')::jsonb #>> '{identity,source_execution_key}') IS NOT DISTINCT FROM source_execution_key)
    );

    IF to_regprocedure('reporting_inline_immutable()') IS NULL THEN
        CREATE FUNCTION reporting_inline_immutable() RETURNS TRIGGER LANGUAGE plpgsql AS $function$
        BEGIN
            RAISE EXCEPTION USING ERRCODE = '23514', MESSAGE = 'reporting inline storage is immutable';
        END
        $function$;
    END IF;

    IF NOT EXISTS (SELECT FROM pg_trigger WHERE tgrelid = 'reporting_inline_objects'::regclass
                   AND tgname = 'reporting_inline_objects_immutable') THEN
        CREATE TRIGGER reporting_inline_objects_immutable
            BEFORE UPDATE OR DELETE OR TRUNCATE ON reporting_inline_objects
            FOR EACH STATEMENT EXECUTE FUNCTION reporting_inline_immutable();
    END IF;
    IF NOT EXISTS (SELECT FROM pg_trigger WHERE tgrelid = 'reporting_inline_seals'::regclass
                   AND tgname = 'reporting_inline_seals_immutable') THEN
        CREATE TRIGGER reporting_inline_seals_immutable
            BEFORE UPDATE OR DELETE OR TRUNCATE ON reporting_inline_seals
            FOR EACH STATEMENT EXECUTE FUNCTION reporting_inline_immutable();
    END IF;
END
$migration$;
