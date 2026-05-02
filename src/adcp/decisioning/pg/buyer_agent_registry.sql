-- AdCP Tier 2 commercial-identity layer — durable registry storage.
--
-- Run this once per deployment. Tracked by the adcp-client-python
-- PgBuyerAgentRegistry; see
-- src/adcp/decisioning/pg/buyer_agent_registry.py for the query
-- shapes the Python code executes.
--
-- COLLATE "C" on the identifier columns avoids locale-dependent case
-- folding — on some locales "https://Acme/" and "https://acme/" compare
-- equal, which would conflate distinct buyer agents. "C" is the
-- byte-for-byte comparison the framework's lookup expects.

CREATE TABLE IF NOT EXISTS adcp_buyer_agents (
    -- AdCP v3 canonical identifier. The framework looks up by this
    -- string for HTTP-Signature traffic after the verifier validates
    -- the signature.
    agent_url             TEXT        COLLATE "C" PRIMARY KEY,

    display_name          TEXT        NOT NULL,

    -- Lifecycle: active / suspended / blocked. Adopters update
    -- in-place to suspend / unblock; the framework rejects suspended
    -- and blocked agents with structured error codes before the
    -- platform method runs.
    status                TEXT        NOT NULL DEFAULT 'active'
        CHECK (status IN ('active', 'suspended', 'blocked')),

    -- Set of permitted BillingMode values for accounts under this
    -- agent. Stored as JSON array; the registry projects to
    -- frozenset[BillingMode] inside the wrapper. Default is
    -- passthrough-only — agent has no payments relationship.
    billing_capabilities  JSONB       NOT NULL DEFAULT '["operator"]'::jsonb,

    -- Bearer-token id for pre-trust beta auth. NULL for signing-only
    -- adopters. Indexed (partial) for the bearer-credential lookup
    -- path; the index excludes NULL rows so pre-trust adopters who
    -- never populate it pay nothing.
    api_key_id            TEXT        COLLATE "C",

    -- Default account terms (rate card, payment terms, credit limit,
    -- billing entity). JSONB blob mirroring BuyerAgentDefaultTerms
    -- shape. Adopters with structured validation can swap to a
    -- domain-specific table joined via FK.
    default_terms         JSONB,

    -- Pre-RFC allowlist of brand domains this agent can transact
    -- for. Static fallback; once Tier 3 BrandAuthorizationResolver
    -- lands (gated on ADCP #3690), this layers on top of per-request
    -- brand.json authz.
    allowed_brands        JSONB,

    -- Adopter passthrough for internal ids, audit metadata, anything
    -- the SDK doesn't model.
    ext                   JSONB       NOT NULL DEFAULT '{}'::jsonb,

    created_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at            TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Bearer-credential lookup index. Partial — only rows with an
-- api_key_id pay the index cost. Signing-only adopters store no
-- bearer keys and the index stays empty.
CREATE INDEX IF NOT EXISTS adcp_buyer_agents_api_key_id_idx
    ON adcp_buyer_agents (api_key_id)
    WHERE api_key_id IS NOT NULL;

-- Suspension / blocking sweep helper — admin tools that list
-- agents-needing-attention can scan by status efficiently without
-- a sequential scan.
CREATE INDEX IF NOT EXISTS adcp_buyer_agents_status_idx
    ON adcp_buyer_agents (status)
    WHERE status <> 'active';
