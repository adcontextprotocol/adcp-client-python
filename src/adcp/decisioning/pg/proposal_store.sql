-- AdCP v1.5 proposal lifecycle — durable ProposalStore backing.
--
-- Run this once per deployment. Tracked by PgProposalStore; see
-- src/adcp/decisioning/pg/proposal_store.py for the query shapes
-- the Python code executes.
--
-- Alternatively, call PgProposalStore.create_schema() from
-- application code — it runs the equivalent DDL idempotently on boot.
--
-- COLLATE "C" on identifier columns avoids locale-dependent case
-- folding — on some locales "Account-A" and "account-a" compare
-- equal, which would collapse distinct tenants into one slot.

CREATE TABLE IF NOT EXISTS adcp_proposal_drafts (
    -- Tenant-scoped composite primary key. account_id-first ordering
    -- matches the per-tenant lookup hot path on get().
    account_id              TEXT        COLLATE "C" NOT NULL,
    proposal_id             TEXT        COLLATE "C" NOT NULL,

    -- Lifecycle: draft / committed / consuming / consumed. The framework
    -- enforces the state machine — this column records the current node.
    state                   TEXT        NOT NULL
        CHECK (state IN ('draft', 'committed', 'consuming', 'consumed')),

    -- {product_id: recipe_dict} — adopters provide a recipe_decoder
    -- at PgProposalStore construction time to rehydrate the dicts back
    -- to typed Recipe subclasses on read.
    recipes                 JSONB       NOT NULL DEFAULT '{}'::jsonb,

    -- The wire Proposal payload returned to the buyer. Stored verbatim
    -- so refine iterations and post-finalize replays don't need to
    -- re-roundtrip through the manager.
    proposal_payload        JSONB       NOT NULL,

    -- Set on commit(); enforces the inventory hold window. NULL while
    -- DRAFT.
    expires_at              TIMESTAMPTZ,

    -- Set on finalize_consumption() / mark_consumed(). NULL until the
    -- proposal terminates at CONSUMED. Partial unique index below
    -- enforces (account_id, media_buy_id) uniqueness for non-NULL rows
    -- so get_by_media_buy_id() reverse-index lookups are deterministic.
    media_buy_id            TEXT        COLLATE "C",

    -- Adopter-controlled schema version captured at put_draft time.
    -- Adopters whose Recipe subclasses add required fields bump this
    -- and write a migration (or evict pre-bump records). Framework
    -- reads but does not enforce.
    recipe_schema_version   INTEGER     NOT NULL DEFAULT 1,

    created_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at              TIMESTAMPTZ NOT NULL DEFAULT now(),

    PRIMARY KEY (account_id, proposal_id)
);

-- Reverse-index for get_by_media_buy_id(). Partial unique on non-NULL
-- media_buy_id so the index stays empty for DRAFT / COMMITTED rows.
-- Tenant-scoped uniqueness — adopter media_buy_ids can collide across
-- tenants (sequential IDs, deterministic test fixtures, etc.) without
-- corrupting the reverse lookup for either tenant.
CREATE UNIQUE INDEX IF NOT EXISTS adcp_proposal_drafts_media_buy_idx
    ON adcp_proposal_drafts (account_id, media_buy_id)
    WHERE media_buy_id IS NOT NULL;

-- Eviction-sweep helper. Adopters running a periodic cleanup job
-- (cron / pg_cron / app-loop) can scan by expires_at without a full
-- table scan.
CREATE INDEX IF NOT EXISTS adcp_proposal_drafts_expires_idx
    ON adcp_proposal_drafts (expires_at)
    WHERE expires_at IS NOT NULL;
