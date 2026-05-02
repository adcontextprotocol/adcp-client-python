# v3 identity bundle — Python design + port plan

Pre-implementation reference for the v3 buyer-side identity layer.
Three concerns, one RFC:

1. **JWKS resolution** — port the JS `BrandJsonJwksResolver` +
   `CapabilityCache` + `ensure_capability_loaded` to Python. The
   existing Python `adcp.signing/` already has the underlying
   verifier machinery; this layer is the missing seam.
2. **Buyer-agent registry** — implement
   [`BuyerAgentRegistry`](https://github.com/adcontextprotocol/adcp-client/issues/1269)
   in Python. New Protocol the framework calls *before*
   `accounts.resolve` to ask "do we recognize this counterparty,
   and what's their commercial relationship?"
3. **Brand authorization** — `BrandAuthorizationResolver` —
   per-request "is this agent authorized for *this* brand?" via
   `brand.json/authorized_operators` + eTLD+1 binding.
   #3690-gated for the binding semantics.

These compose. (1) proves an agent's *cryptographic identity*; (2)
asserts the *commercial relationship*; (3) verifies *per-brand
authorization*. All three checks gate a request.

**Status:** RFC, awaiting review.
**Companions:**
- JS `BuyerAgentRegistry` design — [adcp-client#1269](https://github.com/adcontextprotocol/adcp-client/issues/1269)
- JS parity gaps (other items) — [adcp-client#1249](https://github.com/adcontextprotocol/adcp-client/issues/1249)
- ADCP spec — [#3690](https://github.com/adcontextprotocol/adcp/issues/3690)
  (`brand_url` on `get_adcp_capabilities`, eTLD+1, `authorized_operators[]`)

## What changed since the first draft

The first two drafts of this RFC overstated some gaps and missed
others. After two audits + #1269:

**Already in Python `adcp.signing/` (6,325 LOC) — no port needed:**

| Capability | Surface |
|---|---|
| Inbound HTTP-Sig verification | `verify_request_signature`, `verify_starlette_request`, `verify_flask_request` |
| Outbound request signing | `sign_request`, `async_sign_request` |
| JWKS resolver + caching | `JwksResolver` Protocol, `CachingJwksResolver`, `AsyncCachingJwksResolver`, `StaticJwksResolver` |
| Replay protection | `ReplayStore` Protocol + `InMemoryReplayStore` + `PgReplayStore` |
| Revocation list checking | `RevocationChecker`, `CachingRevocationChecker` |
| `SigningProvider` Protocol + `InMemorySigningProvider` | What I called "`SigningKeyStore`" — already exists |
| Content-Digest validation | `compute_content_digest_sha256`, `content_digest_matches` |
| URL canonicalization | `canonicalize_authority`, `canonicalize_target_uri` |
| SSRF-safe transport | `IpPinnedTransport`, `validate_jwks_uri`, `resolve_and_validate_host` |
| All 17 `REQUEST_SIGNATURE_*` error codes | Including `*_REPLAYED`, `*_KEY_PURPOSE_INVALID`, `*_DIGEST_MISMATCH` |
| `adcp.adagents` (publisher direction) | `fetch_adagents`, `verify_agent_authorization` |

**Already in JS `src/lib/signing/` — Python's port targets:**

| File | LOC | Surface |
|---|---|---|
| `brand-jwks.ts` | 577 | `BrandJsonJwksResolver` |
| `capability-cache.ts` | 119 | `CapabilityCache` |
| `capability-priming.ts` | 159 | `ensureCapabilityLoaded` |

**New for both sides (#1269 — design only, not yet built on JS):**

| Surface | Status |
|---|---|
| `BuyerAgentRegistry` Protocol | Designed in #1269; not yet implemented in either language |
| `BuyerAgent` dataclass with `billing_capability`, `default_account_terms` | New shape — neither language has this today |
| Kind-discriminated `AuthInfo`/`ResolvedAuthInfo` `credential` variant | JS migration designed in #1269; Python's `AuthInfo` needs the same migration |
| `billing_capability` enforcement on `sync_accounts` | New framework-level constraint |
| `BrandAuthorizationResolver` (separate from the JWKS resolver) | Net-new on both sides; #3690-gated |

**Naming feedback received from #1269:** the original first-draft
RFC's `AdagentsResolver` was misnamed; the right surface for
buyer-agent authz is `brand.json/authorized_operators`, not
`adagents.json` (which is sell-side). Already corrected to
`BrandResolver` / `BrandAuthorizationResolver` here. ✓

## The v3 trust model

Three identity layers in spec 3.x with three corresponding
verifications. The spec separates them cleanly; the SDK must too.

| Layer | What it is | Identified by | Trust source |
|---|---|---|---|
| **Agent** | The buyer-side AI agent making the call | `agent_url` (HTTPS origin) | NEVER self-attested. Keys discovered via the brand (operator-attested) per #3690 |
| **Brand** | The advertiser whose ads run | `brand.domain` (house domain) | The brand's `/.well-known/brand.json` `agents[]` array |
| **Operator** | Seller-side platform / SSP / publisher operator | Operator service URL | Operator's `get_adcp_capabilities` response carries top-level `brand_url` per #3690; multi-tenant SaaS operators handled via `authorized_operators[]` on the brand |

**Three checks gate a request, in order:**

```
1. Cryptographic identity (Tier 1)
   inbound request signature
     → CapabilityCache.get(agent_url) → brand_url
     → BrandJsonJwksResolver.resolve(kid)
     → JWK → verify signature
   ⇒ proves: agent_url is who they say they are

2. Commercial recognition (Tier 2 — #1269)
   verified agent_url
     → BuyerAgentRegistry.resolve_by_agent_url(agent_url)
     → BuyerAgent | None
   ⇒ proves: we recognize this agent commercially
   ⇒ provides: billing_capability, default_account_terms, status

3. Per-brand authorization (Tier 3 — #3690)
   (verified agent_url, claimed brand_domain)
     → BrandAuthorizationResolver.is_authorized(agent_url, brand_domain, agent_type)
     → bool   (walks brand.json/agents[]; eTLD+1 binding;
               authorized_operators[] delegation)
   ⇒ proves: this agent is authorized for THIS brand

→ then: accounts.resolve(ref, ctx with BuyerAgent attached)
```

**Direction note (already corrected):**

* `/.well-known/brand.json` (this RFC's primary surface) — published
  by **brands**; authorizes **buyer-side agents** via the `agents[]`
  array, with `jwks_uri` pointers + `authorized_operators[]` for
  multi-tenant operators.
* `/.well-known/adagents.json` (publisher direction, already handled
  by `adcp.adagents`) — published by **publishers / operators**;
  authorizes **sell-side agents** to monetize the publisher's
  inventory.

Both files can be fetched directly OR via an AAO community proxy /
registry (e.g., `adcontextprotocol/registry`).

**Why the three layers don't collapse into one.**

It might seem simpler to fold all of this into one fat
`AccountStore.resolve` (today's surface). #1269 explicitly rejects
that:

- HTTP-Sig + brand.json answer "who signed?" and "are they
  brand-authorized?" — not "who are they to us *commercially*?"
- The OpenRTB analogy: an SSP doesn't just verify a DSP's auth — it
  has a `buyer_id` row with rate cards, credit, payment status,
  suspension flags. Token proves identity; row drives commercial
  behavior.
- The registry is durable infrastructure even with full v3 identity.
  Sellers still need allowlist + onboarding state + commercial-handle
  + billing-capability.

## Tier 1 — JWKS resolution port (in flight)

Status: **shipped on branch `bokelley/round2-webhook-supervisor`**;
2,975 tests pass; awaiting PR-split + review.

Three Python modules, all under `adcp.signing/`:

### 1a. `BrandJsonJwksResolver` (port `brand-jwks.ts`)

Implements `adcp.signing.AsyncJwksResolver`. Walks brand.json,
follows `authoritative_location` / `house` redirects (with bare-host
validation), picks agent by `(agent_type, agent_id?, brand_id?)`,
falls back to origin-bound `/.well-known/jwks.json` (security:
prevents cross-origin trust pivot). Composes with
`AsyncCachingJwksResolver` for the inner JWKS fetch — does NOT
reinvent JWK caching.

10 typed error codes (`invalid_url`, `invalid_house`,
`redirect_loop`, `redirect_depth_exceeded`, `fetch_failed`,
`invalid_body`, `schema_invalid`, `agent_not_found`,
`agent_ambiguous`, `jwks_origin_mismatch`) — same set as JS for
cross-language conformance.

### 1b. `CapabilityCache` (port `capability-cache.ts`)

Per-agent TTL cache for `request_signing` capability blocks. Keyed
on `build_capability_cache_key(agent_uri, auth_token, signer_fingerprint)`
— byte-identical key format to JS so a future shared Redis cache
works cross-language.

### 1c. `ensure_capability_loaded` (port `capability-priming.ts`)

Async primer with fail-open negative-cache TTL (60s) on fetch
failures, in-flight dedup via `asyncio.Future`, MCP / A2A transport
unwrapping (`structuredContent`, `content[].text`,
`result.artifacts[].parts[].data`, `result.parts[].data`).

## Tier 2 — `BuyerAgentRegistry` (the round-2 spine, per #1269)

The Protocol the framework calls *before* `accounts.resolve` to
resolve agent identity into a `BuyerAgent` row. Two methods so
adopters can pick their posture (signing-only / mixed / pre-trust)
by which methods they implement.

### 2a. `BuyerAgent` dataclass

```python
@dataclass(frozen=True)
class BuyerAgent:
    """Commercial identity for a buyer agent we recognize.

    Mirrors the JS surface in adcp-client#1269. ``agent_url`` is the
    canonical, on-the-wire identifier — treat like a public key: stable
    enough that rotation requires explicit re-onboarding. No separate
    internal id is exposed; adopters with internal ids attach them via
    ``ext``.
    """

    agent_url: str
    display_name: str
    status: Literal["active", "suspended", "blocked"]

    #: Drives sync_accounts validation (see § 2c). passthrough_only
    #: means the agent has no payments relationship — accounts under
    #: this agent MUST be operator-billed.
    billing_capability: Literal["passthrough_only", "agent_billable"]

    #: Commercial defaults applied when accounts are provisioned under
    #: this agent. Each field optional; the framework merges these
    #: with per-request overrides (per-request wins).
    default_account_terms: BuyerAgentDefaultTerms | None = None

    #: Pre-RFC allowlist. Obviated once BrandAuthorizationResolver
    #: (Tier 3) is wired — at that point per-brand authz is verified
    #: per-request against brand.json rather than this static list.
    allowed_brands: tuple[str, ...] | None = None

    #: Adopter passthrough — internal ids, audit metadata, etc.
    ext: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class BuyerAgentDefaultTerms:
    rate_card: str | None = None
    payment_terms: str | None = None  # net_15 | net_30 | net_45 | net_60 | net_90 | prepay
    credit_limit: dict[str, Any] | None = None  # {amount, currency}
    billing_entity: dict[str, Any] | None = None  # applies when agent_billable
```

### 2b. `BuyerAgentRegistry` Protocol

```python
class BuyerAgentRegistry(Protocol):
    """Adopter-implemented registry mapping credentials → BuyerAgent.

    Two methods so adopters pick their posture by which ones they
    implement:

    * Signing-only (production target): implement
      ``resolve_by_agent_url`` only; ``resolve_by_credential`` returns
      None. Bearer traffic refused at the registry layer with no extra
      config.
    * Mixed (transition): implement both. Signed traffic resolves
      cryptographically; bearer falls through to the legacy key table.
    * Pre-trust beta: implement ``resolve_by_credential`` only.
      Existing world, just typed. Migration path: add the signed method
      later.

    The framework calls whichever method matches the request's
    credential kind (see § 2d). Returning None rejects the request
    with ``REQUEST_AUTH_UNRECOGNIZED_AGENT``.
    """

    async def resolve_by_agent_url(self, agent_url: str) -> BuyerAgent | None:
        """Resolve a cryptographically-verified agent_url. Called only
        after the framework has verified the RFC 9421 signature and
        extracted ``agent_url`` from the JWK claim — adopters do NOT
        re-verify the signature here, just look up the counterparty
        row. Return None to reject."""

    async def resolve_by_credential(
        self, credential: ApiKeyCredential | OAuthCredential
    ) -> BuyerAgent | None:
        """Resolve a bearer / API-key / OAuth credential to a buyer
        agent. For pre-trust beta sellers, this IS the existing key
        table, just exposed through a typed surface. Return None to
        reject."""
```

### 2c. `billing_capability` enforcement

Concrete validation the framework can enforce now that
`billing_capability` exists on the resolved buyer agent:

| `billing_capability` | Legal `sync_accounts` `billing` values |
|---|---|
| `agent_billable` | `'agent'`, `'operator'`, `'advertiser'` — all three |
| `passthrough_only` | `'operator'` only — `'agent'` and `'advertiser'` reject |

Reject path:

```python
raise AdcpError(
    "INVALID_BILLING_MODEL",
    message=(
        "This buyer agent does not have a payments relationship; "
        "accounts must be operator-billed."
    ),
    field="billing",
    recovery="terminal",
)
```

This is the first place the framework can enforce a commercial
constraint that today drifts silently between adopters.

### 2d. `AuthInfo` migration (kind-discriminated `credential`)

Today's `adcp.decisioning.context.AuthInfo` is flat:

```python
@dataclass(frozen=True)
class AuthInfo:
    kind: str  # "derived" | "signed_request" | ...
    key_id: str | None
    principal: str | None
    scopes: list[str]
```

Per #1269, this should become a kind-discriminated `credential`
shape — JS does this in `ResolvedAuthInfo`, Python's port follows:

```python
@dataclass(frozen=True)
class ApiKeyCredential:
    kind: Literal["api_key"]
    key_id: str

@dataclass(frozen=True)
class OAuthCredential:
    kind: Literal["oauth"]
    client_id: str
    scopes: tuple[str, ...]
    expires_at: float | None = None

@dataclass(frozen=True)
class HttpSigCredential:
    kind: Literal["http_sig"]
    keyid: str
    agent_url: str  # cryptographically verified (NOT claimed)
    verified_at: float

Credential = ApiKeyCredential | OAuthCredential | HttpSigCredential


@dataclass(frozen=True)
class AuthInfo:
    """Resolved authentication context attached to RequestContext.

    Note the rename: previously a flat shape, now wraps a discriminated
    ``credential``. Legacy callsites preserved via a deprecated
    backward-compat alias for one minor.
    """

    credential: Credential

    #: Canonical agent identifier. Populated by BuyerAgentRegistry
    #: resolution. For http_sig: same as credential.agent_url. For
    #: api_key/oauth: from registry.resolve_by_credential().
    #: Authoritative for downstream — audit logs source from here.
    agent_url: str | None = None

    #: Optional seat within the agent (only when the credential carries
    #: one — e.g., signed claims with sub=operator).
    operator: str | None = None

    #: Adopter passthrough.
    extra: Mapping[str, Any] = field(default_factory=dict)
```

**Migration:** keep current flat-shape `AuthInfo` working as a
deprecated alias for one minor. The framework's existing
``serve_kwargs={'context_factory': ...}`` adopter hook continues to
work — adopters that build their own `AuthInfo` either upgrade to
the new shape, or the framework wraps the legacy shape into
`HttpSigCredential` / `ApiKeyCredential` based on `kind`.

### 2e. Dispatch wire-up (call order)

In `decisioning/dispatch.py`, the per-request flow becomes:

```
inbound request
  → context_factory builds AuthInfo (with verified Credential)
  → registry.resolve_by_(agent_url|credential) → BuyerAgent | None
       ── if None: 401/403 with REQUEST_AUTH_UNRECOGNIZED_AGENT
  → status check: if buyer_agent.status != "active":
       403 with AGENT_SUSPENDED / AGENT_BLOCKED
  → attach BuyerAgent to RequestContext (ctx.buyer_agent)
  → method-specific pre-validation:
       - sync_accounts: check billing_capability against request.billing
  → accounts.resolve(ref, ctx)  [unchanged surface, ctx now richer]
  → platform method invoked
```

`RequestContext` gains a typed `buyer_agent: BuyerAgent | None`
field. Platform methods reading commercial defaults
(`default_account_terms` for upserts) source from there.

### 2f. Three implementer postures (table from #1269)

| Seller posture | Implements | Behavior |
|---|---|---|
| **Signing-only** (production target) | `resolve_by_agent_url` only; `resolve_by_credential` returns None | Bearer traffic refused at the registry layer with no extra config |
| **Mixed** (transition) | Both | Signed resolves cryptographically; bearer falls through to legacy key table |
| **Pre-trust beta** | `resolve_by_credential` only | Existing world, just typed. Migration path: add the signed method later |

The signing-only posture is the path of least resistance once an
adopter is ready: implement one method, traffic that doesn't sign is
automatically refused.

## Tier 3 — `BrandAuthorizationResolver` (#3690-gated)

The per-request brand-authz check. Separate from the JWKS resolver;
both walk brand.json and should share an underlying fetcher.

```python
class BrandAuthorizationResolver(Protocol):
    """Verify a buyer agent is authorized to act for a brand.

    Per ADCP #3690: walks brand.json/agents[], enforces eTLD+1
    binding (agent host eTLD+1 must match brand_domain eTLD+1, OR
    the agent host appears in brand.authorized_operators[]).
    """

    async def is_authorized(
        self,
        *,
        agent_url: str,
        brand_domain: str,
        agent_type: str | None = None,
    ) -> bool: ...

    async def resolve(self, brand_domain: str) -> BrandRecord | None:
        """Return the cached/fetched brand.json document. Shared with
        BrandJsonJwksResolver via a common BrandJsonFetcher (factor
        out during implementation)."""
```

**Implementation note:** `BrandJsonJwksResolver` already has the
brand.json fetch + redirect-following + agent-pick machinery.
Factor a shared `_BrandJsonFetcher` (or extend the resolver) so
both surfaces share the snapshot — no double-fetching.

**Net-new on both sides** — neither JS nor Python has this Protocol
yet. Defer until #3690 lands so the eTLD+1 binding rule and
`authorized_operators[]` semantics are stable.

## Tier 1.5 — wire-through (lands with Tier 2)

`adcp.decisioning.serve()` accepts the new dependencies:

```python
serve(
    platform,
    *,
    # Tier 1 (JWKS lookup):
    brand_jwks_resolver_factory: Callable[[str], BrandJsonJwksResolver] | None = None,
    capability_cache: CapabilityCache | None = None,

    # Tier 2 (registry):
    buyer_agent_registry: BuyerAgentRegistry | None = None,

    # Tier 3 (brand authz, #3690):
    brand_authz_resolver: BrandAuthorizationResolver | None = None,

    # Already exist; surfacing here for completeness:
    signing_provider: SigningProvider | None = None,
    replay_store: ReplayStore | None = None,
    revocation_checker: RevocationChecker | None = None,

    enable_request_signature_verification: bool = False,
    require_signed_requests: bool = False,
    ...
)
```

When `enable_request_signature_verification=True`, `serve()` mounts
the existing `verify_starlette_request` middleware
(`adcp.signing.middleware`) configured with the supplied dependencies.
The dispatch layer calls `buyer_agent_registry.resolve_*` after
verification but before `accounts.resolve` (see § 2e).

**No new middleware code** — the signing middleware exists.
What's new is the dispatch-level call to the registry.

## Salesagent migration path

The original concern that drove this RFC was: how does salesagent's
identity model map to v3? Answer: their `Principal` table is a
primitive `BuyerAgent` table.

| Salesagent today | v3 mapping | What changes |
|---|---|---|
| `Principal.access_token` | `ApiKeyCredential.key_id` | Token storage stays; surfaced through typed credential |
| `Principal.principal_id`, `name` | `BuyerAgent.agent_url` (synthesize from existing principal_id), `display_name` | Need to populate `agent_url` — for pre-trust beta they can use `https://principal-{id}.salesagent.local` or similar; on signing onboarding it becomes the real `agent_url` |
| `Account.billing` enum | `BuyerAgent.billing_capability` constraint | Framework now enforces; salesagent's silent drift becomes loud rejection |
| `Account.rate_card`, `payment_terms`, `credit_limit`, `billing_entity` | `BuyerAgent.default_account_terms` | Move from per-account to per-agent default; per-account override still allowed |
| `AgentAccountAccess` junction | Hand-rolled "registry resolves agent, then accounts.resolve finds the account" | Becomes the formal sequence in dispatch |
| Bearer-token auth path | "Pre-trust beta" posture per #1269 | Implement `resolve_by_credential` only — single method, ~50 LOC against existing Principal table |

**Concrete migration steps (after Tier 2 lands):**

1. **No new tables required** — `Principal` already has the data.
   Add `billing_capability` column (default `'passthrough_only'` —
   safe; salesagent's existing principals don't have a payments
   relationship modeled).
2. **Implement `BuyerAgentRegistry.resolve_by_credential`** —
   single method that does
   `SELECT FROM principals WHERE access_token = ?` and projects to
   `BuyerAgent`. ~50 LOC.
3. **Wire into `serve()`** —
   `serve(buyer_agent_registry=SalesagentBuyerAgentRegistry(session_factory))`.
4. **Get framework-enforced `billing_capability` validation
   immediately.** If salesagent currently allows
   `billing: 'agent'` on `sync_accounts` from passthrough principals
   (likely a real bug — they have no enforcement today), the
   framework now catches it.
5. **Later, when ready for signing:** add
   `resolve_by_agent_url` against the same Principal table keyed on
   a new `Principal.agent_url` column. Co-exists with bearer.

LOC delete on salesagent's side: ~zero. They configure existing
primitives + add one column. The benefit isn't LOC delete; it's
**framework-enforced commercial correctness** that prevents
billing-model drift.

## Open questions

1. **Where does `BuyerAgentRegistry` live?**
   `adcp.decisioning.registry` (next to `accounts.py`)?
   `adcp.server.auth.registry`? **Preference:
   `adcp.decisioning.registry`** — the registry's resolution
   composes with `accounts.resolve` and they're conceptually
   adjacent. `adcp.server.auth` is for bearer/JWKS-validation
   primitives, lower-level.

2. **`BuyerAgent` immutability vs mutability.** I have it as a
   frozen dataclass. JS's interface is mutable. Argument for frozen:
   it's a runtime resolution result, mutation by the platform method
   is a bug. **Preference: frozen.**

3. **`AuthInfo` migration cycle.** Keep the legacy flat shape
   working for one minor with a deprecation warning, then remove?
   Or longer? **Preference: one minor with deprecation, two minors
   to remove** — adopters need migration time.

4. **Error code shape for registry rejection.** New code, or reuse
   `REQUEST_SIGNATURE_KEY_UNKNOWN`? Bearer rejection isn't a
   signature error. **Preference: new code
   `REQUEST_AUTH_UNRECOGNIZED_AGENT`** — distinct from signature
   verification failures. Sibling codes: `AGENT_SUSPENDED`,
   `AGENT_BLOCKED`.

5. **Does `BuyerAgentRegistry` get a Postgres reference impl?**
   Most adopters land here against an existing tenant-style
   schema. Argument for shipping a SQLAlchemy reference: every
   adopter writes ~50 LOC of similar code. Argument against:
   adopter schemas vary too much for one reference to fit. **Soft
   yes** — ship a small `examples/buyer_agent_registry_sqlalchemy.py`
   reference, not a class in the SDK proper.

6. **Cross-language `CapabilityCache` Redis sharing.** Already in
   the JS issue; cache key format is byte-identical so the seam
   exists. Lock the format here. **Preference: yes, locked.**

7. **`tldextract` as hard dependency** for eTLD+1 (Tier 3).
   Defer until #3690 lands; revisit then. Alternative: ship
   bundled PSL with refresh instructions.

8. **`BrandAuthorizationResolver` snapshot sharing with
   `BrandJsonJwksResolver`.** Factor a `_BrandJsonFetcher` that
   both classes use, or just have the authz resolver call the
   JWKS resolver's internal cache? **Preference: factor a shared
   private fetcher** so the snapshot isn't duplicated and ETag/
   Cache-Control state is single-source.

## Out of scope

- Offline provisioning (contract, KYC, credit check, ToS sign-off,
  billing-entity capture). Adopter responsibility, same posture as
  how operator account creation isn't modeled.
- Outbound HTTP-Signatures from sellers calling agents. Already
  shipped via `sign_request` / `async_sign_request`.
- Reference impls for `SigningProvider` beyond
  `InMemorySigningProvider`. Adopters with KMS/HSM write their own.
- Multi-tenant JWKS routing infrastructure for the signing path.
  Tracked separately under `adcp.signing/`.
- Adding an envelope `buyer_agent_url` field. Explicitly rejected
  in #1269: bearer auth has no cryptographic binding to envelope
  content, so the field would be exactly as trustworthy as the
  bearer→agent mapping the seller already maintains. Audit logs
  source `agent_url` from `AuthInfo.agent_url`, which the registry
  populates.

## Acceptance (rolled up across tiers)

**Tier 1 (in flight):**

- [x] `BrandJsonJwksResolver` ported with same surface + 10 error
  codes + tests
- [x] `CapabilityCache` ported with byte-identical key format +
  in-flight dedup + tests
- [x] `ensure_capability_loaded` ported with fail-open negative-cache
  + transport unwrapping + tests
- [ ] PR opened, reviewed, merged

**Tier 2 (next, lands without #3690):**

- [ ] `BuyerAgent` + `BuyerAgentDefaultTerms` dataclasses in
  `adcp.decisioning.registry`
- [ ] `BuyerAgentRegistry` Protocol with both async methods
- [ ] `AuthInfo` migration: kind-discriminated `credential` shape +
  legacy alias for one minor + deprecation warning
- [ ] Dispatch wire-up: registry call after auth verification, before
  `accounts.resolve`
- [ ] `RequestContext.buyer_agent: BuyerAgent | None` field
- [ ] `sync_accounts` enforcement of `billing_capability`
- [ ] New error codes: `REQUEST_AUTH_UNRECOGNIZED_AGENT`,
  `AGENT_SUSPENDED`, `AGENT_BLOCKED`, `INVALID_BILLING_MODEL`
- [ ] `examples/buyer_agent_registry_sqlalchemy.py` reference
- [ ] Conformance test: passthrough-only agent rejects
  `billing: 'agent'`; agent-billable agent accepts all three
- [ ] Storyboard updated to cover the three postures

**Tier 3 (gated on #3690):**

- [ ] `BrandAuthorizationResolver` Protocol + reference impl
- [ ] eTLD+1 binding helper (`tldextract` dep or bundled PSL)
- [ ] `authorized_operators[]` delegation
- [ ] `identity.key_origins.{purpose}` consistency check on the
  verifier
- [ ] Diff #3690's seven new `request_signature_*` codes against
  Python's existing 17; add the delta
- [ ] Shared `_BrandJsonFetcher` between JWKS and authz resolvers

## Cross-references

- **ADCP #3690** — `brand_url` on `get_adcp_capabilities`,
  formalizes the verifier chain, eTLD+1 binding,
  `authorized_operators[]`, `request_signature_*` error codes.
- **adcp-client#1269** — `BuyerAgentRegistry` design (the spine of
  Tier 2 above).
- **adcp-client#1249** — JS-side parity gaps (supervisor, audit,
  subdomain routing, Account v3, #3690 follow-on). Note: section 2
  was trimmed after the audit found JS already had Tier 1 ahead of
  Python.
- JS source we're porting:
  - `adcp-client/src/lib/signing/brand-jwks.ts` (577 LOC)
  - `adcp-client/src/lib/signing/capability-cache.ts` (119 LOC)
  - `adcp-client/src/lib/signing/capability-priming.ts` (159 LOC)
- Python primitives we're composing:
  - `adcp.signing.JwksResolver`, `AsyncCachingJwksResolver`,
    `StaticJwksResolver`
  - `adcp.signing.verify_starlette_request`,
    `verify_request_signature`
  - `adcp.signing.ReplayStore`, `PgReplayStore`
  - `adcp.signing.RevocationChecker`, `CachingRevocationChecker`
  - `adcp.signing.SigningProvider`, `InMemorySigningProvider`
  - `adcp.decisioning.AccountStore` (existing — gets a `BuyerAgent`
    threaded through `RequestContext`)
- AAO community proxy / registry: `adcontextprotocol/registry`
- AdCP brand.json schema: `schemas/cache/brand.json`
- AdCP adagents.json schema: `schemas/cache/adagents.json` (opposite
  direction, already handled)
- RFC 9421 (HTTP Message Signatures), RFC 9530 (HTTP Content Digest)
