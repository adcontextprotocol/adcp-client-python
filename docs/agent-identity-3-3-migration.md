# Experimental AdCP 3.3 agent identity

This SDK supports an opt-in preview of issue [#1500](https://github.com/adcontextprotocol/adcp-client-python/issues/1500). The Web Bot Auth wire profile follows `wba-profile.mdx` in adcontextprotocol/adcp#7894 at `e5929470` (vectors in `tests/conformance/vectors/wba-profile/`, provenance in its `UPSTREAM.json`); the identity schemas are pinned to the commits in `src/adcp/signing/_identity_schemas/UPSTREAM.json`. Both may change before the protocol release. The stable wire pin and `COMPATIBLE_ADCP_VERSIONS` do not advertise 3.3 yet.

The behavior below follows the final spec decisions (adcontextprotocol/adcp#8162, #8168, #8169, #8170) and matches the Go (`adcp-go`) and TypeScript (`@adcp/sdk/identity-3.3`) SDKs, except where [Differences from the other SDKs](#differences-from-the-other-sdks) says otherwise.

RFC 9421 comes from `http-message-signatures` (signature bases, `Signature-Input`, signing and verification, for this profile and for the AdCP RFC 9421 profile alike), Structured Fields from `http-sf`, and JOSE from `joserfc`. The SDK keeps the profile rules on top.

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

`agent_origin` explicitly selects Web Bot Auth, sets and covers the structured `Signature-Agent` member, uses `tag="web-bot-auth"`, and uses standard Base64 signatures regardless of the legacy wire encoding. The signer derives the algorithm from the public key, generates a 64-byte random nonce (unpadded base64url), as the profile requires on every signed request, and binds every nonempty body with its digest. A caller-supplied `nonce` must also encode exactly 64 bytes. `@target-uri` covers the URI the request is sent to, unchanged: percent-encoding such as `/a%7Eb/mcp/` is signed as sent (RFC 9421 §2.2.2). The canonical origin is used only for the `Signature-Agent` identity. A request that already carries signature fields is refused rather than stripped; the signer never drops another party's signature.

Automatic signing also requires `web-bot-auth` in the seller's `request_signing.profiles`; omission advertises legacy support only. An unsupported configured profile raises before sending and never silently downgrades. Keep header emission opt-in until all counterparties tolerate the additional covered component.

Web Bot Auth signs with `ed25519`, the profile's one algorithm, following the current #7894 page (`5c3876073f`). The working group hasn't recorded a decision on that page; if it restores another algorithm, this SDK follows it. `sign_wba_request`, `public_directory_key` and `sign_key_directory` accept Ed25519 keys only. ES256 (P-256) stays on the AdCP RFC 9421 profile: a P-256 or RSA key is refused for Web Bot Auth, and such a key in someone's directory is ignored, with its signature. Every signature names `alg`; a signature without it is refused.

Serve the key directory at `/.well-known/http-message-signatures-directory` on that origin. The profile requires it signed (WBA Appendix B.1); verifiers refuse an unsigned directory:

```python
response = sign_key_directory(
    agent_origin="https://buyer.agency.com",
    private_keys=[private_key],
)
# Serve response.body as the exact response bytes, with response.headers.
```

The helper signs once per key, covering `"@authority";req` and `content-digest`. It backdates `created` by five minutes, because verifiers allow little clock skew on directory signatures, and the signatures still expire `expires_in_seconds` from now. Recreate and republish the response before its signatures expire. Publish new keys before using them; verifiers cache directories for up to five minutes. A publisher must not expose private key material in the directory.

Serve a signed revocation list at `/.well-known/governance-revocations.json` on the same origin (`adcp-gov-revocation+jws`, signed by a directory key), even when no key is revoked. **Without one, sellers treat your origin as identity-established and won't bind it for spend.** While they can't read it, or it is stale past grace, your spend-committing calls get `SERVICE_UNAVAILABLE`; other calls proceed.

List the origin in `https://{your registrable domain}/.well-known/trust.json` with `adcp:buying`. `validate_trust_record(document, operator_domain=...)` validates it before you publish.

## Seller verification

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

Set `authorities=frozenset({"seller.example.com"})` to the `host[:port]` values the endpoint serves: a Web Bot Auth request whose `@authority` is not among them is refused (403) before key discovery, so a signature captured at another seller cannot be replayed here under that seller's `Host`. `serve` logs a warning at boot when Web Bot Auth is enabled without it.

A presented `Signature-Agent` or `web-bot-auth` tag selects WBA exclusively, including when verification is in shadow mode. A seller that has not enabled Web Bot Auth answers such a request 403 without `Accept-Signature`; it never falls back to the RFC 9421 profile. `signer_keys` must resolve by origin (its `__call__` accepts `signer_origin=`), which `DiscoveredSignerKeys` does; construction raises `TypeError` otherwise.

### Layers

Verification and authorization are separate layers, as in the other SDKs.

- **Verification** (`verify_wba_request`) needs only the key directory and a replay store. It reads no revocation list and no trust record, and grants no authority, so it can also run in front of the server. `revocation_checker` is optional there; when given, the revocation check runs after verification.
- **Authorization** runs after it: revocation (`check_wba_revocation`), then one injected `WbaAuthorizer` (`adcp.server.wba_authorization`), an async `(signer, operation, params) -> principal | None` that raises an `AdcpError` for an authorization outcome. The middleware never reads a binding or a trust record itself. The default, `OriginBindingGate`, is built from `origin_bindings`, `trust_records`, `binding_role` and `spend_classifier`; pass `RequestSignatureVerification(wba_authorizer=...)` to keep binding and trust evaluation in your application. Grants run later, in the account gate.

### Errors are split by layer

- **The signature path** answers only with Web Bot Auth statuses and headers, never 503 (DR-0023): 400 when a signature field does not parse; 403 with `Accept-Signature` for a missing or failed signature, a second signature or `Signature-Agent` member, a nonce that is not 64 bytes, or an unresolvable key directory; 403 without `Accept-Signature` for a revoked key; 429 with `Accept-Signature` carrying a fresh nonce for a reused nonce; and 429 without it for replay-store or discovery capacity. `Accept-Signature` names the profile's components and the parameters `created`, `expires`, `nonce`, `keyid`, `alg` and `tag="web-bot-auth"`. Any body is the one generic authentication error, the same for every cause: `{"errors":[{"code":"AUTH_INVALID","message":"Request authentication failed","recovery":"correctable"}]}`. The specific reason is logged, never sent.
- **Authorization outcomes** after verification use the standard AdCP error envelope (the MCP and A2A error bindings for skills; otherwise JSON with HTTP 403 or 503 and `Retry-After`) with existing codes:
  - an operation outside what an unbound caller may do gets what an anonymous caller would get;
  - an operation in `required_for` from an unbound or delisted origin, or a failed binding, gets `PERMISSION_DENIED` with no `details`;
  - a missing grant gets `PERMISSION_DENIED` with `{"scope": "agent", "reason": "grant_absent"}`;
  - a trust-record or revocation-list outage on a spend-committing call gets the retryable `SERVICE_UNAVAILABLE`.
- **Infrastructure outages** (the replay store or revocation store failing) are application-layer `SERVICE_UNAVAILABLE`, never a signature outcome.

A correctly signed request whose key the verifier can resolve never gets 403 with `Accept-Signature` (an unresolvable key directory leaves the request unverified under the default policy). Clients act on the status and `Accept-Signature`, not the generic body: sign again on 403 with `Accept-Signature`, sign again with the offered nonce on 429 with `Accept-Signature`, stop using the key on 403 without it, back off on 429 without it or 503.

**Unsigned requests.** With Web Bot Auth enabled, an unsigned request to an operation in `required_for` is answered as the RFC 9421 profile does (401 with `WWW-Authenticate: Signature error="request_signature_required"`) plus an `Accept-Signature` header for Web Bot Auth, so a client of either profile learns how to sign. Set `rfc9421_profile=False` when the endpoint advertises only `web-bot-auth`: such a request, and any RFC 9421 signature, then gets 403 with `Accept-Signature`. `RequestSignatureVerification.from_capability` sets both from `request_signing.profiles` when it lists `web-bot-auth`. With `allow_bearer_fallback`, an unsigned request is passed to bearer authentication as before.

Duplicate structured-field parameters, dictionary labels, and `Signature-Agent` labels (for example `created=1;created=2`) are malformed. Verification rejects them before key discovery or replay admission: WBA returns HTTP 400, and the RFC 9421 profile returns `request_signature_header_malformed`. Parameters on different covered components remain independent, and quoted delimiters do not create extra members or parameters.

### Interop

- **One signature.** A request carries exactly one `web-bot-auth` signature and exactly one `Signature-Agent` member, keyed to that signature's label. Relays are gone: an intermediary that signs is the signer, and whether it may act for another party is an authorization question, not a second signature. `verify_wba_request` still returns a tuple, now always of one signer. `Signature-Agent` is read as a Structured Fields Dictionary only; the older bare sf-string form is not part of the final profile. The member value is the signer's https origin in canonical form, compared as a string: a lowercase host, no default port, no trailing `/`, and no path, query, fragment, or userinfo. Any other spelling is refused with 403 before key discovery, and `sign_wba_request` sends the canonical form. A `Signature-Agent` member whose `type` parameter is not `directory` (a Token or a String) is ignored, so the signature has no member keyed to its label (403). Every `Signature-Input` member must be an inner list and every `Signature` member a Byte Sequence with no parameters, including members with other tags that the verifier doesn't read; otherwise the field is malformed (400). Key directory verification applies the same rule.
- **Nonce.** The nonce encodes exactly 64 bytes. The profile spells it as unpadded base64url; padded standard base64 of 64 bytes is also accepted, because Cloudflare's `web-bot-auth` signer and the draft's Appendix E vectors send it. The page has no shorter-nonce exception for `required_for` operations or reads.
- `@target-uri` is the target URI as received, built from the request line (`raw_path` and query string), so percent-encoding is preserved. `@authority` is that URI's authority, lowercased with any default port removed (RFC 9421 §2.2.3).
- `tests/conformance/vectors/wba-interop/` vendors adcp-client's Cloudflare interop fixtures with provenance; the SDK verifies both the Cloudflare-signed and the adcp-client-signed requests and directories.

### Key directories

`DiscoveredSignerKeys` resolves `(covered origin, thumbprint)`. It never calls capabilities or reads brand.json or trust.json.

- A directory must be signed (WBA Appendix B.1); an unsigned one is a discovery failure. The media type must be `application/http-message-signatures-directory+json` (WBA section 5.5.1); any other media type fails the directory. Keys are found by their RFC 7638 thumbprint; a `kid` is optional and does not affect lookup. Keys the profile cannot use (P-256 and RSA included) are ignored, and so are their signatures (RFC 7517 section 5); private key material fails the directory.
- Every directory signature must verify, cover at least the exact identifiers `"@authority";req` and `"content-digest"` (a keyed member or a bare `"@authority"` does not count), and fall inside its window (60 seconds of skew on `created`, none on `expires`). The digest covers the exact bytes served, a UTF-8 BOM included. Every usable key signs (one signature per key, Appendix B.1): a usable key without a verified signature fails the directory.
- **Discovery failures** (WBA §6.10) cover a directory that does not resolve: a network or TLS error, a timeout, any status other than 200 (404, 410, 429 and 5xx included), or an invalid directory. A discovery failure never evicts a cached directory: its keys keep verifying for up to `max_stale_seconds` (24 hours by default) after the last successful fetch, never past the directory signatures' expiry. A key missing from such a stale copy is a discovery failure too. Failures are retried after a 60-second cooldown.
- With no usable cached copy, the request is unverified, and what happens is local policy: `RequestSignatureVerification(wba_discovery_failure=...)`. The default `"reject"` answers 403 with `Accept-Signature`. `"anonymous"` serves the request exactly as an unsigned one, except operations in `required_for` or `protocol_methods_required_for`.
- A directory that resolves without the key is evidence that the key was removed: a signature failure.
- Successful directories are an LRU cache of `max_origins` (256) origins; eviction only forces a refetch. Concurrent discoveries are bounded globally and per operator (`max_origins_per_operator`, 8). A refusal by these local limits is load shedding: 429, and a cached copy keeps verifying for one more five-minute period only.

The operator quota groups use a registrable domain, except that every tenant of a PSL PRIVATE hosting suffix (such as `*.vercel.app` or `*.github.io`) counts under the provider's registrable domain (`adcp.signing.etld.quota_key`). Authority itself still treats each tenant as its own operator.

To continue accepting configured RFC 9421 buyers on the same endpoint, pass their existing `StaticSignerKeys` or `JwksUriSignerKeys` as `DiscoveredSignerKeys(legacy_resolver=...)`. Only messages selected for the legacy profile use this resolver. A failed WBA discovery never falls back to it.

### Revocation

Framework WBA verification installs `OriginRevocationChecker` by default. It reads each origin's signed list and reports, through `status(origin, keyid)`:

- **`"revoked"`:** refused with 403 and no `Accept-Signature`. A stale list still revokes everything it revoked, and revocations are never withdrawn.
- **`"current"`:** a list within `next_update` plus grace (four polling intervals) does not revoke the key.
- **`"no_list"`:** the origin answers 404/410 and no list was ever verified. The identity is established but cannot be bound: `TrustRecordEvaluator.bind` refuses it, and the binding gate treats a bound origin with no list as identity-established.
- **`"stale"`:** the list cannot be read or verified, is past grace (a stale first list included), or its revocations exceed the checker's fact limits. Spend-committing calls get `SERVICE_UNAVAILABLE` from the binding and grant gates; other calls proceed. A later 404 after a verified list is a failure to read it, never a reset.
- **`"unavailable"`:** too many revocation discoveries are in flight to check now, or a custom checker's store cannot be read: every bound call gets `SERVICE_UNAVAILABLE`.

The result is recorded in `VerifiedSigner.revocation` and in `context.metadata["adcp.auth_info"].extra["wba_revocation"]`. A custom `wba_revocation_checker` can implement `status()`, or be a legacy async callable `(origin, keyid) -> bool` whose failure counts as `"stale"`. Concurrent checks for one origin share one fetch. Both checker variants keep `keyCompromise` and the earliest `invalidity_date`; an undated compromise stays undated. `RevocationDetails.exposes(received_at)` uses seller receipt time to assess compromise exposure.

Per-origin list state is a least-recently-used cache of `max_origins` (256) origins and `max_origins_per_operator` (8) per quota group, so a flood of attacker origins cannot lock new buyers out; evicting it never forgets a revocation. Revoked key facts are kept separately with non-evicting limits: `max_facts_per_origin=2048`, `max_facts_per_operator=8192` (per quota group) and `max_history_facts=65536`. Overflow refuses the offending origin (it stays `"stale"`) without forgetting another origin's revocations. This history is in memory: it is lost on restart and not shared across replicas. The checker has no pluggable durable store yet; public deployments that need revocations to hold across restarts should supply their own `wba_revocation_checker` with durable state.

## Identity-established callers and binding

A verified origin with no binding is identity-established. It gets what an anonymous caller gets:

- Operations in `unbound_for` (default `get_adcp_capabilities` and `get_principal`) run with the verified identity attached. `get_principal` is always allowed, and on MCP so are the session methods (`initialize`, `notifications/initialized`, `ping`, `tools/list`).
- Any other operation is served exactly as an anonymous request, with no verified signer, so your application answers it as it answers anonymous callers (for example, bearer authentication still applies). An operation in `required_for` or `protocol_methods_required_for` is refused with `PERMISSION_DENIED`, even when `allow_bearer_fallback` is set and a valid bearer token accompanies the signature (as in Go): a caller that presents a signature is judged by it.
- Include `sync_principal` in `unbound_for` only when the application implements an explicit open onboarding policy that checks trust, requirements and billing before approval. `get_principal` implementations should return the preview's `unconfigured` result when no durable principal exists. Discovering a key never creates or upserts an account.

To approve a binding, evaluate the operator's record explicitly:

```python
await records.bind(verified_signer, principal_id="principal_123", bindings=bindings)
```

The signer must carry a current revocation state (`verified_signer.revocation == "current"`; the framework's verification sets it), and the record must currently list exactly one agent at the verified origin with `adcp:buying`. Migrating an existing RFC 9421 principal requires `existing_rfc9421_principal=True, migration_approved=True` and a seller-side approval decision. The same origin cannot be rebound to another principal. Once bound, legacy signatures for that origin are rejected (no downgrade). Persist bindings and the origin's commercial identity in the seller's registry; the registry still governs suspension, billing and account access.

The framework preserves the verified origin as caller identity and exposes the binding's principal ID in `context.metadata["adcp.auth_info"].extra["principal_id"]`. The verified signer is read per request from the request scope, so a stateful MCP session never attributes one request's signer to another.

### Delisting and trust-record outages

A stored binding records seller approval, not permanent operator membership. The binding gate re-evaluates the operator's agent entry through `trust_records` on every request; its cache is bounded by the polling interval. `binding_role` defaults to `adcp:buying`.

- **Delisted** (the last record read does not list the origin with the role, or the operator never published a record): the origin falls back to identity-established, exactly as at first contact, with no exception: task polling and cancelling included. The binding is kept, so the origin still cannot downgrade to RFC 9421, and `bind` refuses to bind it again while it is unlisted. Operators retire an agent by publishing an empty `agents[]`.
- **Unreachable or unreadable** (a transport error, any status other than 200 including a 404/410 after the operator published a record, an invalid record, or a local capacity refusal): a discovery failure, not a delisting and not a removal. An outage never restores access.
  - The last record read stays authoritative. If it did not list the origin, the origin stays delisted whatever made the refresh fail.
  - If it did, spend-committing calls fail with `SERVICE_UNAVAILABLE`. Other calls continue under the binding while that record is within `TrustRecordEvaluator(outage_continuation_seconds=...)` past its freshness. That bound is seller policy (default four polling intervals; `0` disables continuation). After it, they also get `SERVICE_UNAVAILABLE` until a fetch succeeds.
  - With no record in the authority history, outages are `SERVICE_UNAVAILABLE`.
- A 404/410 from an operator that has never published a record means no record: the origin is not listed. `TrustRecordEvaluator.check_listing` implements these rules.

`RequestSignatureVerification.spend_classifier(operation, params)` lets a seller classify spending using its own state. The default fails closed: new purchases, acquisitions, `activate_signal` and unknown operations commit spend, and so does every `update_media_buy` or `control_media_buy` unless each change is a rename, a pause (`paused: true`) or a cancellation, at the buy or package level. Budgets, bids, impressions, flight dates, pacing, spend targets, new packages, resumes and unknown fields all count as spend. A seller that can compare a change with its own state (for example, a budget reduction) supplies its own classifier. The decision also governs the account grant gate.

Framework `serve` dispatch projects authorization failures through the existing MCP and A2A error bindings before handler side effects. Standalone `SignedRequestVerificationMiddleware` answers them itself with the AdCP envelope by default. Set `defer_authorization_errors=True` only when the integration applies the verified context with `apply_verified_signer` and calls `check_wba_authorization` from `adcp.server.signed_requests` before its handler. The built-in A2A handler checks the owner-scoped task store before cancelling or subscribing to an active task; a custom `request_handler` must enforce equivalent ownership checks.

## Trust records

`TrustRecordEvaluator` derives `https://{eTLD+1}/.well-known/trust.json` using the SDK's pinned ICANN and PRIVATE PSL snapshot. It follows one `authoritative_location` pointer while preserving the original owner, follows no HTTP redirects, caps bytes at 2 MiB, rejects ambiguous JSON, and caches no longer than the configured polling interval (default 60 seconds). Negative results are cached for 60 seconds.

- **Host budgets.** Fetches to an operator's own hosts are limited per `(operator, host)` (`max_fetches_per_host`, 4). `authoritative_location` hops to hosts outside the operator's registrable domain share a separate global per-host budget (`max_foreign_fetches_per_host`, 16), so a hop never spends the target operator's own budget and many operators cannot multiply fetches to one host. Host cool-downs are scoped to `(operator, host)` and never follow content errors. A budget refusal is a retryable outage that is never cached as a failure.
- **Authority history** (which operators have served a 200, and the last record read from each) is security state. It is never evicted. The in-memory default admits at most `max_history_records` (65,536) operators and `max_history_records_per_operator` (64) per quota group (PSL PRIVATE tenants count under their provider). A new operator over quota is refused as unavailable before fetching, and no other operator's history is lost. Losing this history (a restart, another replica) turns a later 404 into absence, re-enabling brand.json fallback, or turns a delisting into an outage. The evaluator has no pluggable durable history store yet; until it does, run it in one long-lived process per seller, or treat brand.json fallback as unavailable for houses you have seen publish.

`validate_trust_record(document, operator_domain=...)` validates authoring output, rejects keys in agent entries, checks ownership, and enforces one agent per origin. The record's host is its owner; a hosted document cannot name another owner inside its content.

## Demand grants

`TrustGrantEvaluator.check(...)` follows L2 §Grants in trust.json:

- **One record authorizes: the house's.** Pass `resolve_house=`, an async `(brand_domain, brand_id) -> BrandHouse | str | None`, returning the house named by the brand's verified brand.json (a house redirect, or a `house_domain` the house's `brand_refs[]` reciprocates). `BrandHouse(domain, brand_id)` also gives the brand's child-brand id in the house's portfolio, which grants' `profiles.adcp.brands` are checked against (as in TypeScript). Without the hook, or when it returns `None`, the brand's own domain is the house. A resolver failure is `SERVICE_UNAVAILABLE`. Grants in any other record are never consulted.
- **Domains compare as registrable domains** under the pinned PSL snapshot, the `grantee` included (a grantee of `ads.platform.com` matches `platform.com`; `validate_trust_record` still asks authors to publish registrable domains). Authority is implicit when `account.operator`, the brand's domain and the signer's operator share one.
- **Otherwise a chain must reach the signer's operator with `account.operator` on it:** the house grants the signer's operator, which is `account.operator`; the brand is `account.operator` (a self-operated brand) and its house grants the signer's operator; or the house grants `account.operator`, whose own record grants the signer's operator. There is no third link.
- **Every link must cover** the scope, `profiles.adcp.brands`, the countries and the agent narrowing (a narrowed link that does not list the signer's canonical origin ends the chain), and the check time must fall inside every link's `valid_from`/`valid_until`, so the effective window is the latest `valid_from` to the earliest `valid_until`.
- `exclusive_grants` makes `grants[]` exhaustive for the scopes it names, disabling `seller_authorization` for them.
- **No downgrade.** A 200 from the house's trust.json makes it the only source of grants: `legacy_authorization` (brand.json `authorized_operators`) runs only while the house has never published one.
- **Fetch failures.** A grantor or intermediary record that can't be read, including a 404/410 after it was read, is never absence and never `grant_absent`. A claim marked non-spend (`allow_stale_on_outage=True`) is evaluated on the last records read within the outage-continuation bound; anything else gets `SERVICE_UNAVAILABLE`. An intermediary that has never published a record provides no second link.

For `DecisioningPlatform`, wire a gate and an active buyer-agent registry:

```python
from adcp.decisioning.trust_grants import TrustGrantClaim, TrustGrantGate
from adcp.signing import TrustGrantEvaluator

# Extract existing-account brand/operator/countries from seller-owned state.
gate = TrustGrantGate(
    evaluator=TrustGrantEvaluator(records, resolve_house=resolve_verified_house),
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

- The gate checks every natural-key `sync_accounts` declaration before any upsert. A row that names an existing `account_id` is checked against that account's seller-owned claim, and its brand/operator fields are checked too; an `account_id` that does not resolve is denied.
- Existing accounts and account-scoped calls use the seller's extractor, including a fresh check before spending. An extractor that raises or returns something other than a `TrustGrantClaim`, or an operation outside media buying whose claim keeps the default scope, fails closed with `PERMISSION_DENIED` without details; a claim without a brand or operator is denied the same way. `grant_absent` is reserved for a claim no grant covers.
- `TrustGrantClaim.scope` defaults to `adcp:media_buying` for buying, `list_products` and account provisioning. For other domains, configure `extract_operation_claim(account, buyer_agent, operation)` to select an explicit scope.
- A missing grant raises `PERMISSION_DENIED` with `{"scope": "agent", "reason": "grant_absent"}` after commercial identity is established. A signer its own operator does not list gets `PERMISSION_DENIED` without details; an outage requiring fresh authority gets the retryable `SERVICE_UNAVAILABLE`. A stale revocation list fails spend closed at the gate too. Provisioning always requires fresh authority. The gate changes no account status.
- The gate keeps no claim history of its own: the evaluator's last record read is authoritative, so a withdrawn grant stays withdrawn through a later outage.

When configured, this gate replaces the older `brand_authorization_gate` for account-scoped dispatch. Put any permitted brand.json fallback in `TrustGrantEvaluator(legacy_authorization=...)`. Legacy RFC 9421 and bearer buyers can use it without an operator trust record while the house has never published trust.json. Supply equivalent checks in custom `ADCPHandler` implementations before account side effects.

Typed client results preserve `adcp_error`, including its code, recovery guidance, and details.

## Preview types and rollout

Use the preview `RequestSigning` model (`adcp.types.experimental_identity`) to advertise `profiles=["adcp/request-signing/v1", "web-bot-auth"]` on a server accepting both. The preview reader models preserve unknown onboarding requirement kinds so applications can fall back to the posture's message and URL; unknown kinds cannot be satisfied automatically. Authoring validation still enforces the pinned draft schema.

The schemas follow #8170 and #8168: the buyer posture lives only in trust.json, at `profiles.adcp.onboarding` on the record and on an agent entry; one mode enum (`open`, `application`, `invite_only`) serves sellers and buyers; the `agent_identity` requirement kind and `documentation_url` are gone; `aao_verified` uses `verified_specialisms`; `relationship.status` uses `pending_approval`.

Import experimental models from `adcp.types.experimental_identity`: `AgentOnboarding`, its modes/tiers/requirements, `BuyerOnboardingPosture`, `TrustRecord`, `GetPrincipalResponse`, `PrincipalRelationship`, `PrincipalRelationshipStatus`, `PrincipalChangedWebhook`, and `GrantAbsentDetails`. Generate the private models offline with `python scripts/generate_identity_preview.py`; validate behavioral constraints with the trust helpers.

For preview `principal.changed` reasons, pass `experimental_agent_identity=True` to `WebhookSender.prepare_principal_changed` or `send_principal_changed`, and again to `send_prepared` or `resend` when publishing persisted events.

Use `ADCPClient(..., experimental_agent_identity=True)` or the equivalent multi-agent option to preserve and parse these fields on capabilities, principal reads and completed-task callbacks. WBA signing opts into these response models automatically.

`PrincipalService(relationship_resolver=...)` accepts an async seller-owned lookup keyed by the authenticated `PrincipalIdentity`. `relationship` is a 3.3 field: `PrincipalService.get_principal(..., context=tool_context)` consults the resolver and emits it only when `context.resolved_adcp_version` is 3.3 or later. Because `COMPATIBLE_ADCP_VERSIONS` does not include 3.3 yet, it is emitted only once a server serves 3.3; calls that omit `context=` never emit it. Reading a pending application can therefore return `unconfigured` with a relationship without creating a durable principal or authorizing an account.

Onboarding posture is advisory. Sellers still implement requirements, applications and billing policy. Evaluate the verified origin's trust record and required role with `records.check_listing(...)` when applying an `open` policy, before provisioning access.

AdCP 3.3 deprecates the RFC 9421 profile; it keeps working through 3.x. `SigningConfig` and `RequestSignatureVerification` emit a `DeprecationWarning` for it only once the SDK advertises 3.3 (`COMPATIBLE_ADCP_VERSIONS`), so legacy constructions stay quiet while 3.3 is not served. `async_resolve_agent`, `verify_from_agent_url` and BrandJsonJwksResolver keep the 3.2 chain. JWS artifacts from an issuer that a trust record lists moved to its key directory in 3.3 (adcp#8299): see [JWS artifacts](#jws-artifacts-on-the-key-directory) below.

**Governance tokens under the earlier #7894 draft.** `adcp.signing.wba.verify_wba_governance_issuer(token, signer_keys=..., governance_agents=...)` runs the profile's two governance checks (One agent per purpose): the token's `kid` is in the key directory of the `iss` origin and `jku` is that directory's URL (`governance_key_unknown`), and `iss` is the buyer's governance agent (`governance_issuer_not_authorized`), with the JWS, signature and time checks around them. It returns the issuer origin. The rest of the governance checklist (`aud`, `phase`, `sub`, the action binding, revocation, replay) stays with the caller. The vectors in `tests/conformance/vectors/wba-governance/` grade it. It predates the R4 erratum (it accepts `alg` `Ed25519` and requires `jku`, as those vectors do); verify governance contexts with `verify_governance_authorization(web_bot_auth=...)` instead, below. Roll back opt-in signing only for origins that have not been bound; rolling a bound origin back to RFC 9421 requires explicit seller migration.

## JWS artifacts on the key directory

`specs/agent-identity-3-3.md` R4, as amended by adcp#8299 (WG-approved 2026-10-19), verifies three JWS artifacts with the signing agent's Web Bot Auth key directory. `adcp.signing.jws_artifacts` reuses the request path's components:

```python
from adcp.signing.jws_artifacts import HttpGovernanceKeyArchive, WbaJwsArtifactVerifier

jws_artifacts = WbaJwsArtifactVerifier(
    records=trust_records,          # TrustRecordEvaluator: selects the profile
    signer_keys=directory_keys,     # DiscoveredSignerKeys: O's key directory
    revocation=origin_revocations,  # OriginRevocationChecker: O's list
    archive=HttpGovernanceKeyArchive(),  # history for governance contexts only
)
```

- **O.** The canonical origin of `iss` for a governance context, `agent_url` for designated-task response signing, and `rights_agent.url` for a rights attestation. Pass the URL from your own context (the governance agent you expect, the agent you called, the rights agent named in the constraint you hold), never one read from the unverified token.
- **Selection.** `select_jws_artifact_profile` (or `jws_artifacts.select`) derives the trust record from O. A record that lists O, with any role, selects the key directory. A record that doesn't list O, an operator that has never published one, or an O with no registrable domain selects the 3.2 profile. A record that can't be read (an outage, an invalid record, a 404/410 after a 200, local capacity) raises `JwsArtifactError` `jws_key_unavailable`: a fetch failure never selects 3.2.
- **Key rules.** `alg` is `EdDSA` and `kid` is the key's JWK thumbprint, found in O's directory. No `use`, `key_ops` or `adcp_use` check applies: purpose separation is by origin. A key that another origin publishes is `jws_key_unknown`; a directory that can't be read is `jws_key_unavailable`.
- **Revocation.** O's list. A revoked key is `jws_key_revoked`. A stale list, one that can't be checked, or (unless `revocation_absent="accept"`) an origin that serves none is `jws_revocation_unavailable`.
- **Governance contexts.** `verify_governance_authorization(..., web_bot_auth=jws_artifacts)` selects the profile from `expected_issuer`. On the key-directory path `jwks` and `revocation_checker` are not used, and every other check is unchanged: keep the brand.json cross-check of `iss` (`resolve_governance_jwks`, or `resolve_issuer_jwks` in `make_governance_enforcement_middleware`), `is_jti_revoked` and `revocation_resolver`. Outages are `governance_jwks_unavailable` (new in the error-code literal), revocation problems `governance_token_revoked`, and an origin that serves no list is refused because a governance context authorizes spend. Unlisted issuers stay on the 3.2 path.
- **History.** `verify_historical_governance_context(token=, expected_issuer=, web_bot_auth=)` verifies a retained governance context for audit: a key rotated out of the directory is taken from the governance key archive at O (`/.well-known/jwks-archive.json`, matched by thumbprint; `key_source="archive"`). It checks the header, issuer, key, signature and O's revocation list (an origin with no list has revoked nothing), not expiry, audience or replay. An unlisted issuer returns a failure with `profile="3.2"`.
- **Designated responses and rights attestations.** The SDK has no surface verifier for these yet, so `jws_artifacts.verify(token, artifact=..., signing_agent_url=..., typ=...)` supplies the selection and key rules a surface verifier calls; it raises `JwsArtifactUses32ProfileError` when the 3.2 profile applies. `compact_jws_from_envelope(signed_response)` turns a designated-task `signed_response` (decoded payload, JCS signing input) into the compact JWS it verifies. The surface's own checks (claims, `request_hash`, freshness, the brand-authorization cross-check, the rights profile) stay with the caller.

## Differences from the other SDKs

- **Nonce encodings.** Exactly 64 bytes, as #7894 requires; Python also accepts the padded standard-base64 spelling Cloudflare's signer emits. Go and TypeScript are aligning to #7894 at the same commit.
- **Signed directories with an unsigned key.** Only keys with a verified signature are usable (draft directory-05). Go accepts every key in a signed directory; TypeScript refuses the directory.
- **Discovery capacity.** A local discovery-limit refusal is a 429, as in TypeScript. Go treats it as a discovery failure.
- **Directory created skew.** Directory signatures tolerate 60 seconds of `created` skew, as in TypeScript; Go allows none. All SDKs backdate `created` when publishing.
- **Spend classification.** Fails closed for `update_media_buy`/`control_media_buy` (only renames, pauses and cancellations are non-spend); Go treats every `update_media_buy` as spend and `control_media_buy` as non-spend.
- **Unsigned-request challenge.** Python adds `Accept-Signature` to the 401 for unsigned required requests (and answers 403 when only `web-bot-auth` is offered), per the spec's Coexistence rules. Go and TypeScript do not yet.
- **Revocation admission.** An over-capacity first discovery that cannot evict anything is `"unavailable"` (every bound call fails), never `"stale"`.
- **JWS artifact outages.** A governance context whose issuer's trust record, key directory or key archive can't be read is `governance_jwks_unavailable` (the retryable code in the security.mdx taxonomy); TypeScript answers `governance_key_unknown`. A revocation list that can't be checked is `governance_token_revoked` in both, as on the 3.2 path. A trust record that can't be read is unavailable even when the last record read listed the origin, as in TypeScript; Go keeps the key-directory path within the outage-continuation bound.
- **Grant-gate claim history.** The gate no longer keeps its own approval/denial history (and its `max_denials` / `max_claims_per_operator` options are gone): the evaluator's last record read is the single source, as in Go. A seller-authorization hook must therefore order its own concurrent decisions.

## Breaking changes to the experimental API

The experimental identity surface changed incompatibly with the previous preview. Aligning with the final #7894 profile:

- `verify_wba_request(replay_status=)` and `RequestSignatureVerification(wba_replay_status=)` are gone: a reused nonce is always 429 with `Accept-Signature` carrying a fresh nonce.
- `DiscoveredSignerKeys(require_signed_directories=)` and `verify_directory_response(require_signed=)` are gone: directories must always be signed.
- Relay signatures are refused (403); a request carries one `web-bot-auth` signature and one `Signature-Agent` member.
- The verifier requires a 64-byte nonce (it accepted 22 to 256 characters), and `sign_wba_request(nonce=)` refuses anything else.
- P-256 keys are refused for Web Bot Auth (`public_directory_key`, `sign_wba_request`, `sign_key_directory`, and `SigningConfig`/`sign_request` with `agent_origin`); `ecdsa-p256-sha256` stays on the RFC 9421 profile.
- `accept_signature_value()` names `alg` and, on a 403, a bare `nonce` parameter; a replay 429 passes a fresh `nonce`. `WbaVerificationError.headers` is computed from its status and label, and the `fresh_nonce=` argument marks a replay answer.
- `adcp.signing.wba` adds `verify_wba_governance_issuer`, `WbaGovernanceError`, `WBA_ALGORITHMS` and `WBA_GOVERNANCE_TYP`.

From the earlier alignment:

- `WbaUnavailableError` is an application-layer infrastructure error (`RuntimeError`), no longer a `WbaVerificationError` with `status`/`headers`. Discovery failures are `WbaDiscoveryError`; a revoked key is `WbaRevokedKeyError` (`WbaAuthorizationError` remains as an alias).
- `verify_wba_request(revocation_checker=...)` is optional; `OriginRevocationChecker.status()` replaces the boolean call, which now raises for undecidable states.
- `TrustGrantGate(max_denials=..., max_claims_per_operator=...)` are gone; `require_claim(allow_cached_binding_on_outage=)` is `non_spend=`; `require_account(bound_wba_identity=)` is gone and `wba_revocation=` is new.
- `TrustRecordEvaluator.bind` requires `signer.revocation == "current"`.
- `RequestSignatureVerification.spend_classifier` receives `None` when the arguments cannot be read; the binding logic moved from the middleware into `OriginBindingGate`.
- Standalone `SignedRequestVerificationMiddleware` authorization errors are the AdCP envelope with 403/503, no longer 401 `{"error": "unauthenticated"}`.
- `adcp.types.experimental_identity.BuyerOnboardingMode` is removed (its values are gone); the buyer posture uses `AgentOnboardingMode`.
- The delisted-origin task-control exception, and its A2A owner marker, are removed.

`adcp.signing` and `adcp.types.experimental_identity` are now part of the public-API snapshot (`tests/fixtures/public_api_snapshot.json`), so additions and removals there are reviewed.

## Not covered yet

- The final 3.3 schema bundle and the P7 trust-record and grant-chain conformance vectors are upstream release dependencies; re-pin and rerun conformance when they land.
- Durable stores: bindings and replay state are pluggable; authority history and revocation history are in memory with no pluggable store yet (Go and TypeScript have one). Public deployments need durable versions of all four.
- The directory cache is evictable by unauthenticated traffic naming many origins (as in Go); eviction only forces a refetch, but a signer whose directory is down at that moment loses its stale-copy continuation.
