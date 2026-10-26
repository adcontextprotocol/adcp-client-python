-- AdCP get_media_buys context echo — durable MediaBuyContextStore backing.
--
-- Persists the context a create_media_buy request carried (buy-level and per
-- package) so get_media_buys echoes it unchanged. A new table: applying it
-- changes nothing that exists. Tracked by PgMediaBuyContextStore; see
-- src/adcp/decisioning/pg/media_buy_context_store.py.
--
-- Alternatively, call PgMediaBuyContextStore.create_schema() from application
-- code, or use PgMediaBuyContextStore.migration_sql() for a custom table name.
--
-- context is JSON (not JSONB) so key order and number spelling round-trip
-- exactly. COLLATE "C" on identifiers avoids locale-dependent case folding.

CREATE TABLE IF NOT EXISTS adcp_media_buy_contexts (
    account_id    TEXT        COLLATE "C" NOT NULL,
    media_buy_id  TEXT        COLLATE "C" NOT NULL,
    scope         TEXT        NOT NULL CHECK (scope IN ('media_buy', 'package')),
    package_id    TEXT        COLLATE "C" NOT NULL DEFAULT '',
    context       JSON        NOT NULL,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (account_id, media_buy_id, scope, package_id),
    CHECK ((scope = 'media_buy') = (package_id = ''))
);
