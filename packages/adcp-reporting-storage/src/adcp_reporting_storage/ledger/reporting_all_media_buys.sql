-- Reporting scope all_media_buys (AdCP 3.2) and per-buy coverage evidence.
--
-- reporting_configurations.media_buy_scope: 'media_buy_ids' reports exactly
-- the generation's media_buy_ids, where [] is a proven zero-buy scope; an
-- 'all_media_buys' generation stores [] and freezes each period's buy set when
-- the producer closes it. Existing generations are exact-scope, so the default
-- keeps them unchanged.
--
-- reporting_obligations.coverage: the core/reporting-coverage.json partition
-- frozen with the obligation at its scope-resolution boundary. NULL on
-- obligations that retained only coverage_status.
--
-- Online-safe: defaulted or nullable columns and their checks. Processes of an
-- earlier release neither read nor write them and keep running beside them.
DO $all_media_buys$
BEGIN
    PERFORM pg_advisory_xact_lock(hashtext('adcp.reporting.schema'), hashtext(current_schema()));
    ALTER TABLE reporting_configurations
        ADD COLUMN IF NOT EXISTS media_buy_scope TEXT NOT NULL DEFAULT 'media_buy_ids';
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'reporting_configurations'::regclass
          AND conname = 'reporting_configurations_media_buy_scope_check'
    ) THEN
        ALTER TABLE reporting_configurations
            ADD CONSTRAINT reporting_configurations_media_buy_scope_check
            CHECK (media_buy_scope IN ('media_buy_ids', 'all_media_buys')
                   AND (media_buy_scope = 'media_buy_ids' OR media_buy_ids = '[]'::jsonb));
    END IF;
    ALTER TABLE reporting_obligations ADD COLUMN IF NOT EXISTS coverage JSONB;
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'reporting_obligations'::regclass
          AND conname = 'reporting_obligations_coverage_check'
    ) THEN
        ALTER TABLE reporting_obligations
            ADD CONSTRAINT reporting_obligations_coverage_check
            CHECK (coverage IS NULL OR (jsonb_typeof(coverage) = 'object'
                   AND coverage->>'status' = coverage_status));
    END IF;
END
$all_media_buys$;
