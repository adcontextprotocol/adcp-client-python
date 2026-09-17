-- B2.3 isolated immutable snapshots. No old catalog object is changed.
DO $migration$
BEGIN
    PERFORM pg_advisory_xact_lock(hashtext('adcp.reporting.feed.schema'));
    CREATE TABLE IF NOT EXISTS reporting_feed_snapshots (
        account_id TEXT COLLATE "C" NOT NULL,
        consumer_id TEXT COLLATE "C" NOT NULL,
        snapshot_id TEXT COLLATE "C" NOT NULL CHECK (snapshot_id ~ '^rpfs_[0-9a-f]{32}$'),
        as_of TIMESTAMPTZ NOT NULL,
        representation_version INTEGER NOT NULL CHECK (representation_version = 1),
        ownership_mode TEXT COLLATE "C" NOT NULL CHECK (ownership_mode = 'absent'),
        document TEXT NOT NULL,
        content_sha256 TEXT COLLATE "C" NOT NULL,
        signing_key BYTEA NOT NULL CHECK (octet_length(signing_key) = 32),
        PRIMARY KEY (account_id, consumer_id, snapshot_id),
        UNIQUE (snapshot_id),
        CHECK (content_sha256 = encode(sha256(convert_to(document,'UTF8')), 'hex')),
        CHECK (content_sha256 = reporting_receipt_ingestion_sha256(document::jsonb)),
        CHECK ((document::jsonb->>'version')::integer IS NOT DISTINCT FROM 1),
        CHECK ((document::jsonb->>'account_id') IS NOT DISTINCT FROM account_id),
        CHECK ((document::jsonb->>'consumer_id') IS NOT DISTINCT FROM consumer_id),
        CHECK ((document::jsonb->>'snapshot_id') IS NOT DISTINCT FROM snapshot_id),
        CHECK ((document::jsonb->>'as_of')::timestamptz IS NOT DISTINCT FROM as_of),
        CHECK ((document::jsonb->>'representation_version')::integer IS NOT DISTINCT FROM representation_version),
        CHECK ((document::jsonb->>'ownership_mode') IS NOT DISTINCT FROM ownership_mode),
        CHECK (jsonb_typeof(document::jsonb->'records') IS NOT DISTINCT FROM 'array'),
        CHECK (jsonb_array_length(document::jsonb->'records') IS NOT DISTINCT FROM (document::jsonb->>'total_count')::integer),
        CHECK (jsonb_typeof(document::jsonb->'inputs') IS NOT DISTINCT FROM 'object'),
        CHECK (document::jsonb - ARRAY['version','representation_version','ownership_mode',
            'account_id','consumer_id','snapshot_id','as_of','after','through','filters',
            'common','total_count','records','inputs'] = '{}'::jsonb)
    );
    CREATE OR REPLACE FUNCTION reporting_feed_immutable()
    RETURNS TRIGGER LANGUAGE plpgsql AS $function$
    BEGIN
        RAISE EXCEPTION 'reporting feed snapshot is immutable' USING ERRCODE = '23514';
    END
    $function$;
    DROP TRIGGER IF EXISTS reporting_feed_immutable ON reporting_feed_snapshots;
    CREATE TRIGGER reporting_feed_immutable BEFORE UPDATE OR DELETE ON reporting_feed_snapshots
        FOR EACH ROW EXECUTE FUNCTION reporting_feed_immutable();
END
$migration$;
