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

A presented `Signature-Agent` or `web-bot-auth` tag selects WBA exclusively, including when verification is in shadow mode. Parsing, failed signatures/replays, and capacity failures use HTTP 400, 403 with `Accept-Signature`, and 429. Every WBA signature verifies, and a relay must cover the earlier signers' signature fields. The originating signer remains the AdCP caller.

`DiscoveredSignerKeys` resolves `(covered origin, thumbprint)` from a signed directory. It never calls capabilities or reads brand.json or trust.json. `verify_wba_request` is the standalone async verifier; it returns verified origins without granting authority. The legacy synchronous verifier rejects WBA markers and cannot be used as a fallback.

To continue accepting configured RFC 9421 buyers on the same endpoint, pass their existing `StaticSignerKeys` or `JwksUriSignerKeys` as `DiscoveredSignerKeys(legacy_resolver=...)`. Only messages selected for the legacy profile use this resolver. A failed WBA discovery never falls back to it. Concurrent directory lookups coalesce per origin and do not block unrelated signers.

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

A stored binding records seller approval, not permanent operator membership. Framework binding authorization re-evaluates the operator's agent entry using `trust_records` and its polling-bounded cache, including when reusing a trusted edge's verified signer. `binding_role` defaults to `adcp:buying`; configure the required role for other receiving surfaces. Delisting or a failed refresh removes authority without undoing the historical binding or permitting a legacy downgrade. This authority gate runs after transport verification; key discovery still reads no trust record.

Framework WBA verification installs `OriginRevocationChecker` by default to read signed `adcp-gov-revocation+jws` lists from the verified origin. Supply a custom `wba_revocation_checker` for shared or durable revocation state; standalone `verify_wba_request` callers must pass a checker before relying on the identity for authorization. Both revocation checker variants retain revoked identifiers across refreshes and keep `keyCompromise` and the earliest `invalidity_date`. `RevocationDetails.exposes(received_at)` uses seller receipt time to assess compromise exposure. Durable applications should persist revocation history across process restarts. The origin checker refuses new origins at capacity instead of forgetting retained revocations.

## Trust records and demand grants

The built-in origin revocation checker admits at most 256 origins and eight per registrable operator by default. Failed initial discovery consumes only a bounded 60-second cooldown entry; successful signed history is retained. Configure `max_origins` and `max_origins_per_operator`, or supply a durable checker with seller-owned admission policy for larger deployments. Discovery caches coalesce requests per origin/domain so one slow counterparty does not block another tenant.

`TrustRecordEvaluator` derives `https://{eTLD+1}/.well-known/trust.json` using the SDK's pinned ICANN and PRIVATE PSL snapshot. It follows one `authoritative_location` pointer while preserving the original owner, follows no HTTP redirects, caps bytes at 2 MiB, rejects ambiguous JSON, and caches no longer than the configured revocation polling interval. Failed or expired records provide no authority. A 200 response permanently disables legacy fallback for that host during the evaluator's lifetime, including after a later disappearance or fetch failure.

`validate_trust_record(document, operator_domain=...)` validates authoring output, rejects keys in agent entries, checks ownership, and enforces one agent per origin. The record's host is its owner; a hosted document cannot name another owner inside its content.

`TrustGrantEvaluator.check(...)` evaluates authority from the brand's record. It recognizes same-domain implicit authority and direct or one-intermediary chains that include `account.operator`. Every link must cover the requested scope, brand, countries, agent narrowing, and validity window. It intersects authority and cannot follow a third delegation link. `exclusive_grants` disables discretionary authorization for scopes named by the record. The optional `seller_authorization` hook consults seller-owned onboarding policy; `legacy_authorization` runs only when the brand has never successfully published trust.json and its initial fetch returns 404/410. Fetch failures do not invoke either fallback.

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

The gate checks every natural-key `sync_accounts` declaration before any upsert. Existing account IDs and account-scoped calls use the seller's extractor, including a fresh check before spending. Missing authority raises `PERMISSION_DENIED` with `{"scope": "agent", "reason": "grant_absent"}` after commercial identity is established. It changes no account status. Supply equivalent checks in custom `ADCPHandler` implementations before account side effects.

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
