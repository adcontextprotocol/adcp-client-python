# Experimental AdCP 3.3 agent identity

This SDK supports an opt-in preview of issue [#1500](https://github.com/adcontextprotocol/adcp-client-python/issues/1500). The Web Bot Auth wire profile and identity schemas are pinned to the draft commits recorded in `tests/conformance/vectors/wba-profile/README.md` and `src/adcp/signing/_identity_schemas/UPSTREAM.json`. They may change before the protocol release. The stable wire pin and `COMPATIBLE_ADCP_VERSIONS` do not advertise 3.3 yet.

## Buyer signing and publishing

Give each agent its own HTTPS origin. Generate a key using the existing keygen helpers, then derive its WBA public JWK and thumbprint:

```python
from adcp.signing import (
    SigningConfig, load_private_key_pem, public_directory_key, sign_key_directory,
)

private_key = load_private_key_pem(private_pem)
public_jwk = public_directory_key(private_key)
signing = SigningConfig(
    private_key=private_key,
    key_id=public_jwk["kid"],
    agent_origin="https://buyer.agency.com",
)
# Pass signing=signing to ADCPClient or install_signing_event_hook.
```

`agent_origin` explicitly selects Web Bot Auth, sets and covers the structured `Signature-Agent` member, uses `tag="web-bot-auth"`, and uses standard Base64 signatures regardless of the legacy wire encoding. The signer derives the algorithm from the public key, generates a nonce, and binds every nonempty body with its digest. Keep header emission opt-in until all counterparties, including Go verifiers, tolerate the additional covered component. Automatic signing also requires `web-bot-auth` in the seller's `request_signing.profiles`; omission advertises legacy support only. An unsupported configured profile raises before sending and never silently downgrades.

Serve a signed directory response at `/.well-known/http-message-signatures-directory` on that origin:

```python
response = sign_key_directory(
    agent_origin="https://buyer.agency.com",
    private_keys=[private_key],
)
# Serve response.body as the exact response bytes, with response.headers.
```

The helper signs once per key, covering the request authority and content digest. Recreate and republish the response before its signatures expire. Publish new keys before using them; cached directories expire within five minutes. A publisher must not expose private key material in the directory.

## Seller verification and binding

```python
from adcp.server import RequestSignatureVerification, serve
from adcp.signing import (
    DiscoveredSignerKeys, OriginBindings, OriginRevocationChecker, TrustRecordEvaluator,
)

keys = DiscoveredSignerKeys()
bindings = OriginBindings()  # One-process example; production supplies a durable resolver.
records = TrustRecordEvaluator(polling_interval_seconds=60)
verification = RequestSignatureVerification(
    signer_keys=keys,
    enable_web_bot_auth=True,
    origin_bindings=bindings,
    trust_records=records,
    wba_revocation_checker=OriginRevocationChecker(keys),
    required_for=frozenset({"sync_accounts", "create_media_buy"}),
    # Supply a shared replay_store for multiple processes/replicas.
)
serve(handler, request_signature_verification=verification)
```

A presented `Signature-Agent` or `web-bot-auth` tag selects WBA exclusively, including when verification is in shadow mode. Signature parsing, failed signatures/replays, and verification capacity failures use HTTP 400, 403 with `Accept-Signature`, and 429, with a generic response body. Every WBA signature verifies, and a relay must cover the earlier signers' signature fields. The originating signer remains the AdCP caller. Once identity is established, authorization failures use the standard AdCP error envelope; a correctly verified request never receives an `Accept-Signature` challenge.

Duplicate structured-field parameters, dictionary labels, and `Signature-Agent` labels are malformed. Verification rejects them before key discovery or replay admission: WBA returns HTTP 400, and the RFC 9421 profile returns `request_signature_header_malformed`. Parameters on different covered components remain independent, and quoted delimiters do not create extra members or parameters.

`DiscoveredSignerKeys` resolves `(covered origin, thumbprint)` from a signed directory. It never calls capabilities or reads brand.json or trust.json. `verify_wba_request` is the standalone async verifier; it returns verified origins without granting authority. The legacy synchronous verifier rejects WBA markers and cannot be used as a fallback.

When a key directory cannot be fetched or a revocation list is stale past grace, the request receives retryable `SERVICE_UNAVAILABLE`. It establishes no identity and performs no business operation; it does not silently fall back to anonymous access. A fetched directory without the requested key, or a directory returning `404` or `410`, remains a signature failure with the WBA challenge.

To continue accepting configured RFC 9421 buyers on the same endpoint, pass their existing `StaticSignerKeys` or `JwksUriSignerKeys` as `DiscoveredSignerKeys(legacy_resolver=...)`. Only messages selected for the legacy profile use this resolver. A failed WBA discovery never falls back to it. Concurrent directory lookups coalesce per origin and do not block unrelated signers. Successful directory history admits at most `max_origins=256` origins and `max_origins_per_operator=8` origins per registrable operator. It does not evict a previously admitted origin to make room for a new one. Unauthenticated initial failures use a separate bounded cooldown cache.

A verified unbound caller can invoke `get_adcp_capabilities` and `get_principal` by default. Configure `unbound_for` to match the handler's other anonymous permissions, such as public `get_products` and `list_creative_formats`, and JSON-RPC methods such as `initialize` or `tools/list` when they are public. Include `sync_principal` only when the application implements an explicit open onboarding policy that checks trust, requirements and billing before approval. These operations receive an established origin with no principal or account authority. `get_principal` implementations should return the preview's `unconfigured` result when no durable principal exists. Discovering a key never creates or upserts an account. To approve a binding, evaluate the operator's record explicitly:

```python
await records.bind(
    verified_signer,
    principal_id="principal_123",
    bindings=bindings,
)
```

The record must list exactly one agent at the verified origin with `adcp:buying`. Migrating an existing RFC 9421 principal requires `existing_rfc9421_principal=True, migration_approved=True` and a seller-side approval decision. The same origin cannot be rebound to another principal. Once bound, legacy signatures for that origin are rejected. Persist bindings and the origin's commercial identity in the seller's registry; the registry still governs suspension, billing and account access.

The framework preserves the verified origin as caller identity and exposes the binding's principal ID in `context.metadata["adcp.auth_info"].extra["principal_id"]`. Use that seller-owned ID when resolving durable principal/account state during an approved migration; the transport does not rewrite application records.

A stored binding records seller approval, not permanent operator membership. Framework binding authorization re-evaluates the operator's agent entry using `trust_records` and its polling-bounded cache, including when reusing a trusted edge's verified signer. `binding_role` defaults to `adcp:buying`; configure the required role for other receiving surfaces. A delisted origin falls back to identity-established access: the application's anonymous operations and `get_principal`, with no account authority. It can still poll and cancel tasks it created while listed, as specified in [the first-contact rules](https://github.com/adcontextprotocol/adcp/blob/c1ce495b380d4b98f6892939b0eb5e137ce20d2c/docs/building/by-layer/L2/accounts-and-agents.mdx). Those controls retain the original task owner without restoring its principal or allowing new work under the historical binding. It cannot bind again while unlisted. Keep its historical binding to prevent a legacy downgrade.

An unreachable trust record does not delist a bound origin. The last record read stays authoritative, including a delisting or removal. Spend-committing calls fail with retryable `SERVICE_UNAVAILABLE`; other calls can continue under existing permissions only within four polling intervals after the last listing record's freshness deadline. Without a prior listing, or after that grace window, they also receive `SERVICE_UNAVAILABLE`. A `404` or `410` after a record was read is removal rather than an outage. A failed binding or trust-record match uses `PERMISSION_DENIED`. These authority checks run after transport verification; key discovery still reads no trust record.

`RequestSignatureVerification.spend_classifier(operation, params)` lets a seller classify spending using its own state. The default treats new purchases, acquisitions, resumes, financial changes, and unknown operations as spend-committing; name changes, pauses, and cancellations can continue under a cached binding. Budget changes are conservative because the verifier cannot compare them with seller-owned balances. The trusted decision also governs the account grant gate.

Framework `serve` dispatch projects authorization failures through the existing MCP and A2A error bindings before handler side effects. Standalone `SignedRequestVerificationMiddleware` rejects denied requests before entering a custom app by default. Set `defer_authorization_errors=True` only when the integration applies the verified context with `apply_verified_signer` and calls `check_wba_authorization` from `adcp.server.signed_requests` before its handler. A custom A2A `request_handler` also keeps the default rejection boundary. Applications still enforce anonymous permissions and task ownership.

For `get_principal`, an identity-established or delisted caller with `principal_id=None` receives `unconfigured` and any advisory relationship; never resolve historical principal configuration from its origin alone. After approval, read `get_principal` again using the bound identity. The built-in A2A handler checks the owner-scoped task store before cancelling or subscribing to an active task. A custom `request_handler` must enforce equivalent ownership checks. The installed MCP server has no task-control implementation; accepting an authenticated task method does not add one. A2A tasks created under an anonymous owner do not automatically move to the bound owner. Keep anonymous task polling and controls disabled unless the application's task ownership policy supports them.

Framework WBA verification installs `OriginRevocationChecker` by default to read signed `adcp-gov-revocation+jws` lists from the verified origin. Supply a custom `wba_revocation_checker` for shared or durable revocation state; standalone `verify_wba_request` callers must pass a checker before relying on the identity for authorization. Both revocation checker variants retain revoked identifiers across refreshes and keep `keyCompromise` and the earliest `invalidity_date`. An observed undated compromise stays undated, so a later date cannot narrow its exposure window. `RevocationDetails.exposes(received_at)` uses seller receipt time to assess compromise exposure. Durable applications should persist revocation history across process restarts. The origin checker refuses new origins at capacity instead of forgetting retained revocations.

## Trust records and demand grants

The built-in origin revocation checker admits at most 256 origins and eight per registrable operator by default. Initial discovery without an authenticated list consumes only a bounded 60-second cooldown entry. Authenticated signed history, including revocations in stale responses, is retained; a stale response still cannot authorize a request past grace. Configure `max_origins` and `max_origins_per_operator`, or supply a durable checker with seller-owned admission policy for larger deployments. Discovery caches coalesce requests per origin/domain so one slow counterparty does not block another tenant.

Revoked key and token facts also have non-evicting limits: `max_facts_per_origin=2048`, `max_facts_per_operator=8192`, and `max_history_facts=65536`. If an authenticated list exceeds a limit, that origin's checker stays unavailable for its lifetime, retaining its earlier facts; a later list omitting the refused revocations cannot restore access. Other admitted origins keep their history. Size these limits for the deployment, and persist admission decisions and revocation history when supplying a durable checker.

`TrustRecordEvaluator` derives `https://{eTLD+1}/.well-known/trust.json` using the SDK's pinned ICANN and PRIVATE PSL snapshot. It follows one `authoritative_location` pointer while preserving the original owner, follows no HTTP redirects, caps bytes at 2 MiB, rejects ambiguous JSON, and caches no longer than the configured revocation polling interval. Keep a custom polling interval no longer than the identity's revocation cadence; the default is 60 seconds. Fresh approval requires current records; outage grace is an explicit option for existing non-spend access and never replaces a newer negative record. A 200 response permanently disables legacy fallback for that host during the evaluator's lifetime, including after a later disappearance or fetch failure.

Host failure cooldowns are scoped to the original record owner, so an owner's pointer cannot suppress another owner's discovery. Content errors do not cool down a host. The default limits allow 256 pending records and four concurrent fetches per destination host; local saturation fails closed without caching a failure, so callers can retry as soon as capacity is available. Shared transport responses still receive separate ownership validation.

Outage continuity rechecks the latest retained policy for every owner, even when another origin or claim fetched it. Removals and invalid records supersede earlier approvals. `max_history_records` admits at most 256 registrable owners by default, separately from the ephemeral `max_records` cache limit. Each owner uses one history slot. Published-record and policy facts are retained without eviction; when history is full, a new owner receives retryable `SERVICE_UNAVAILABLE` before fetching. An initial `404` or `410` also retains admission, so an established legacy grantor can refresh after the pool fills; its absence cache still expires, and admission alone never authorizes outage fallback. Existing owners can still refresh, and their publication and removal evidence stays authoritative. A final snapshot and clock check prevents a concurrent refresh or slow callback from extending older authority.

`validate_trust_record(document, operator_domain=...)` validates authoring output, rejects keys in agent entries, checks ownership, and enforces one agent per origin. The record's host is its owner; a hosted document cannot name another owner inside its content.

`TrustGrantEvaluator.check(...)` evaluates authority from the brand's record. It recognizes same-domain implicit authority and direct or one-intermediary chains that include `account.operator`. A self-operated brand can grant the signer's operator directly: the brand itself is `account.operator` on that chain. Every link must cover the requested scope, brand, countries, agent narrowing, and validity window. It intersects authority and cannot follow a third delegation link. `exclusive_grants` disables discretionary authorization for scopes named by the record. The optional `seller_authorization` hook consults seller-owned onboarding policy; `legacy_authorization` runs only when the brand has never successfully published trust.json and its initial fetch returns 404/410. Fetch failures cannot establish new authority. Explicit outage continuity rechecks retained records and preserves every grant's hard validity deadline.

For `DecisioningPlatform`, wire a gate and an active buyer-agent registry:

```python
from adcp.decisioning.trust_grants import TrustGrantClaim, TrustGrantGate
from adcp.signing import TrustGrantEvaluator

# Extract existing-account brand/operator/countries from seller-owned state.
gate = TrustGrantGate(
    evaluator=TrustGrantEvaluator(records),
    extract_claim=lambda account, agent: TrustGrantClaim(
        brand_domain=account.metadata["brand_domain"],
        account_operator=account.metadata["operator"],
        brand_id=account.metadata.get("brand_id"),
        countries=tuple(account.metadata.get("countries", ())),
    ),
)
# adcp.decisioning.serve(..., buyer_agent_registry=registry, trust_grant_gate=gate,
#                         request_signature_verification=verification)
```

The gate checks every natural-key `sync_accounts` declaration before any upsert. Existing account IDs and account-scoped calls use the seller's extractor, including a fresh check before spending. An uncovered grant raises `PERMISSION_DENIED` with `{"scope": "agent", "reason": "grant_absent"}` after commercial identity is established. A failed trust-record match uses `PERMISSION_DENIED` without grant details; an outage requiring fresh authority uses retryable `SERVICE_UNAVAILABLE`. Existing bound accounts can continue non-spend calls only under previously read authority within its grace window; provisioning still requires a current grant. A grantor's record removed after a read means `grant_absent`, and cannot restore legacy authorization. A known grant denial persists until a fresh successful check; an outage cannot restore withdrawn authority. `max_denials` bounds the combined process-local claim history (default 4,096), with `max_claims_per_operator` limiting each signer's registrable operator to 128 claims. Admission reserves a slot before evaluation. Overflow refuses the new claim without forgetting retained approvals or denials, or disabling other operators' outage continuity. Persist withdrawals in seller-owned state when continuity across restarts is required. The gate changes no account status. Supply equivalent checks in custom `ADCPHandler` implementations before account side effects.

Typed client results preserve `adcp_error`, including its code, recovery guidance, and details. A `SERVICE_UNAVAILABLE` result has transient recovery and can be retried after backoff; `PERMISSION_DENIED` requires an authorization change.

Only a previously approved claim can use outage grace. Successful proofs and denials share the bounded claim slots and remain available until a fresh check changes their authority. An outage read never refreshes a proof or extends an explicit grant's validity window.

When configured, this gate replaces the older `brand_authorization_gate` for account-scoped dispatch. Put any permitted brand.json fallback in `TrustGrantEvaluator(legacy_authorization=...)`; applying both gates independently would wrongly consult `authorized_operators` after a successful trust.json fetch.

The framework passes the trusted transport profile into this gate. Legacy RFC 9421 and bearer buyers can use the configured legacy authorization hook without an operator trust record while the brand has never published trust.json. Once the brand returns 200, both profiles use fresh operator membership and trust grants; a missing record or fetch failure cannot restore the old path.

`TrustGrantClaim.scope` defaults to `adcp:media_buying` for buying and account provisioning. For other domains, configure `extract_operation_claim(account, buyer_agent, operation)` to select an explicit scope from the framework's operation name and seller-owned state. A media-buy grant cannot authorize creative generation merely because both calls name an account.

## Preview types and rollout

Use the preview `RequestSigning` model to advertise `profiles=["adcp/request-signing/v1", "web-bot-auth"]` on a server accepting both. The preview reader models preserve unknown onboarding requirement kinds so applications can fall back to the posture's message and URL; unknown kinds cannot be satisfied automatically. Authoring validation still enforces the pinned draft schema.

For preview `principal.changed` reasons, pass `experimental_agent_identity=True` to `WebhookSender.prepare_principal_changed` or `send_principal_changed`, and again to `send_prepared` or `resend` when publishing persisted events. The shared validator retains the stable envelope checks and permits only the two draft relationship reasons under this explicit option.

Import experimental models from `adcp.types.experimental_identity`: `AgentOnboarding`, its modes/tiers/requirements, `BuyerOnboardingPosture`, `TrustRecord`, `GetPrincipalResponse`, `PrincipalRelationship`, `PrincipalChangedWebhook`, and `GrantAbsentDetails`. The response wrappers preserve existing fields while adding typed onboarding and relationship fields. Both new relationship webhook reasons are accepted. Generate the private models offline with `python scripts/generate_identity_preview.py`; validate behavioral constraints with the trust helpers.

Use `ADCPClient(..., experimental_agent_identity=True)` or the equivalent multi-agent option to preserve and parse these fields on capabilities, principal reads and completed-task callbacks. WBA signing opts into these response models automatically. Stable 3.2 schema validation remains tied to the wire pin; validate new identity fields against the preview schemas rather than treating the stable bundle as a 3.3 conformance bundle.

`PrincipalService(relationship_resolver=...)` accepts an async seller-owned lookup keyed by the authenticated `PrincipalIdentity`. It can return a `PrincipalRelationship` model, a mapping or `None`. Reading a pending application can therefore return `unconfigured` with a relationship without creating a durable principal or authorizing an account.

Onboarding posture is advisory. Sellers still implement requirements, applications and billing policy. Evaluate the verified origin's trust record and required role with `records.evaluate(...)` when applying an `open` policy, before provisioning access. An `open` sandbox tier grants sandbox access only; live spending requires a billing relationship and applicable brand grants. A mode absent from the posture stays undeclared.

RFC 9421 configuration emits a `DeprecationWarning` and continues to work through 3.x. `async_resolve_agent`, `verify_from_agent_url`, BrandJsonJwksResolver and governance/rights/document JWS verification retain their existing chain. Their JWS migration is deferred to AdCP 3.4. Roll back opt-in signing only for origins that have not been bound; rolling a bound origin back to RFC 9421 requires explicit seller migration, not an automatic fallback.

The draft WBA transport vectors are vendored and tested. Released P7 trust-record and grant-chain vectors and the final 3.3 schema bundle remain upstream release dependencies; update the pinned previews and run conformance again when they land. The upstream migration-guide SDK table must be updated when a release includes this support.
