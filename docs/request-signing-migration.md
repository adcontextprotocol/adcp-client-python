# Migrating to signed AdCP requests

Rolling out RFC 9421 request signing against an existing AdCP integration is a two-track exercise: **bootstrap** once, then **enforce** in stages per operation. Key **rotation** follows the same pattern and is meant to be routine.

This guide covers the operator-facing mechanics. Spec reference: [Signed Requests (Transport Layer)](https://adcontextprotocol.org/docs/building/implementation/security#signed-requests-transport-layer).

The Python SDK ships parallel ergonomics to [adcp-go's MIGRATION guide](https://github.com/adcontextprotocol/adcp-go/blob/main/adcp/signing/MIGRATION.md) — same staged rollout, same key-rotation pattern, different language idioms.

`ADCPClient` derives the signing wire profile from its trusted
`server_version` / `adcp_version` pin. An explicit
`SigningConfig(signing_profile_version=...)` overrides that selection. The
low-level `sign_request()` and `async_sign_request()` primitives require an
explicit profile because they do not participate in version negotiation.
The supported signing profiles are 3.0, 3.1, and 3.2. A signed client pinned
to an older AdCP version therefore fails at construction unless
`signing_profile_version` explicitly selects a supported profile; unsigned
legacy clients are unaffected.

## 1. Bootstrap

One-time work to make an agent able to sign (as a buyer) or verify (as a seller).

### Generate and publish a key

```bash
adcp-keygen --alg ed25519 --kid my-agent-2026-01 --purpose request-signing \
  --out signing.pem
```

Or programmatically — the same spine the CLI uses:

```python
from adcp.signing import generate_signing_keypair

pem, public_jwk = generate_signing_keypair(
    alg="ed25519",
    kid="my-agent-2026-01",
    purpose="request-signing",
)
```

Prefer Ed25519 over ES256 unless a regulatory constraint forces NIST curves. Ed25519 is deterministic by construction — no RNG participates at sign time, which simplifies replay analysis.

The command writes `signing.pem` (PKCS#8 private key) and prints/returns a JWK with `use: "sig"`, `key_ops: ["verify"]`, and `adcp_use: "request-signing"`. Publish the JWK at your agent's `jwks_uri`:

```json
{ "keys": [ { "kid": "my-agent-2026-01", "kty": "OKP", "crv": "Ed25519", "x": "...", "use": "sig", "key_ops": ["verify"], "adcp_use": "request-signing" } ] }
```

Hold the PEM in your secret store — environment variable, GCP Secret Manager, AWS Secrets Manager. Never commit it.

### Advertise signing on `get_adcp_capabilities`

Set `request_signing` on your capabilities response with empty `supported_for` / `warn_for` / `required_for` to start. Counterparties probing your capabilities can now see the block exists.

```python
from adcp.types.capabilities import RequestSigning

request_signing = RequestSigning(
    supported=True,
    covers_content_digest="either",  # "required" | "forbidden" | "either"
    required_for=[],
    warn_for=[],
    supported_for=[],
)
```

## 2. Staged enforcement (per operation)

Never flip an operation straight from unsigned to required. Stage it through three stops.

### Step A — `supported_for`

Add the operation to `supported_for`. Counterparties **MAY** sign; your verifier **MUST** accept signed requests but does not yet reject unsigned ones.

The Python middleware stays permissive — wire `replay_store` and `jwks_resolver`, but leave `required_for` empty:

```python
from adcp.signing import (
    CachingJwksResolver,
    InMemoryReplayStore,
    VerifierCapability,
    VerifyOptions,
    verify_starlette_request,
)

jwks_resolver = CachingJwksResolver(jwks_uri="https://buyer.example.com/.well-known/jwks.json")

# `replay_store` defaults to a fresh InMemoryReplayStore — single-process
# deployments don't need to wire one explicitly. Pass an explicit shared
# store (Redis-backed, custom Postgres) for multi-replica setups.
options = VerifyOptions(
    now=time.time(),
    capability=VerifierCapability(
        covers_content_digest="either",
        required_for=frozenset(),         # nothing rejected yet
        supported_for=frozenset({"create_media_buy"}),
    ),
    operation="create_media_buy",
    jwks_resolver=jwks_resolver,
)

verified = await verify_starlette_request(request, options=options)
# verified.key_id is the buyer's signing identity.
```

Success signal: signed requests arrive, `verify_starlette_request` returns a `VerifiedSigner` rather than raising.

### Step B — `warn_for`

Move the operation to `warn_for`. Verification still runs, failures are logged, traffic is unaffected. Watch your failure rate and walk down the long tail of "some counterparty is misbehaving" before flipping to reject.

The spec calls this shadow mode. Python doesn't have a built-in `observe_only` flag yet — approximate by catching `SignatureVerificationError` and logging:

```python
from adcp.signing import SignatureVerificationError

try:
    verified = await verify_starlette_request(request, options=options)
except SignatureVerificationError as exc:
    # WARNING: step-B shim only. Delete before enabling required_for in step C —
    # this turns every required-signed op into an unsigned op.
    logger.warning(
        "signature would reject", extra={"code": exc.code, "step": exc.step}
    )
    verified = None  # fall through unsigned
```

Success signal: search your logs for `"signature would reject"` and watch the rate fall to zero — or to a known-and-tolerated set of counterparties — over a window long enough to cover your slowest integrator's deploy cadence.

### Step C — `required_for`

Move the operation to `required_for` and populate `VerifierCapability.required_for`. **Don't enable `covers_content_digest="required"` yet** — body-modifying intermediaries are the most common surprise failure in production rollouts and they only break the digest path. Land `required_for` first:

```python
options = VerifyOptions(
    now=time.time(),
    capability=VerifierCapability(
        covers_content_digest="either",
        required_for=frozenset({"create_media_buy"}),
    ),
    operation="create_media_buy",
    jwks_resolver=jwks_resolver,
)

# Remove the step-B shim. Let SignatureVerificationError propagate to your
# 401 response builder.
try:
    verified = await verify_starlette_request(request, options=options)
except SignatureVerificationError as exc:
    return JSONResponse(
        status_code=401,
        headers=unauthorized_response_headers(exc),
        content={"error": exc.code, "message": str(exc)},
    )
```

A rollout later, once you've confirmed no `request_signature_digest_mismatch` in step-B logs, tighten to `covers_content_digest="required"`:

```python
capability=VerifierCapability(
    covers_content_digest="required",
    required_for=frozenset({"create_media_buy"}),
)
```

### Rollback from step C

If production breaks after flipping `required_for`:

1. Revert the verifier config — drop the operation from `VerifierCapability.required_for` (and from `covers_content_digest="required"` if set). Redeploy.
2. Do **not** touch `jwks_uri` or your revocation list. Counterparties that are already signing correctly will keep doing so, harmlessly.
3. Update `get_adcp_capabilities.request_signing.required_for` on the next deploy to match the rolled-back verifier — counterparties probing capabilities must not be told "required" while the verifier is back to permissive.

Returning to step B's shadow mode is the safe resting state while you diagnose.

## 3. Key rotation

Schedule rotation routinely — monthly to quarterly is the common range — so the path is exercised and a compromise-driven rotation isn't the first time you run it.

### Routine rotation

Two JWKS publishes plus a signer cutover:

1. **Publish new kid alongside old kid.** Update `jwks_uri` to list both keys. Wait for counterparties' JWKS caches to refresh — `CachingJwksResolver` holds a 30-second refetch cooldown on kid-miss, so one minute is a safe floor.
2. **Cut over the signer.** Flip `SigningConfig.key_id` / `SigningConfig.private_key` on every instance. Adapters using `ADCPClient(signing=...)` reconstruct the client with the new config; adapters using `install_signing_event_hook` recreate the hook against the new `SigningConfig`.
3. **Grace period.** Hold both kids in the JWKS for at least **2× the max validity window** (10 minutes with the default 300-second window). Reasoning: one window for the last request you signed under the old kid to reach its `expires`, plus one window for its replay-cache entry to age out so you can't distinguish a real replay from a drained entry. Add operational headroom on top (1–2× the deploy cadence of your slowest counterparty) so in-flight retries under the old kid still verify.
4. **Remove the old kid.** Publish a JWKS with only the new kid.

### Compromise rotation

Ordering is different — the old kid must stop being trusted *before* anything else happens, because during routine step 1 ("publish both"), counterparties still accept signatures under the old kid for the full JWKS-propagation window:

1. **Revoke first.** Add the burned kid to your revocation list with a timestamp covering the compromise window. Counterparties polling the revocation list (via `CachingRevocationChecker`) will reject anything signed by the burned kid, even if their JWKS cache hasn't refreshed yet.
2. **Publish the new kid.** Update `jwks_uri` to include the new kid. The old kid can stay in the JWKS or be removed immediately — revocation beats presence.
3. **Cut over the signer** to the new kid on every instance.
4. **Remove the old kid** from the JWKS once the compromise-window revocation entry is no longer needed. Keep the revocation entry active for at least the historical audit window (whatever your governance requires).

## 4. Common pitfalls

- **Body-modifying intermediaries break `content-digest` coverage.** CDNs, WAFs, and API gateways that recompress or re-serialize request bodies cause `request_signature_digest_mismatch`. Diagnose by comparing signer-side body bytes to verifier-side body bytes — they must be byte-identical. Either preserve bytes end-to-end or stay on `covers_content_digest="either"` for the affected operation.
- **Forgetting to disable redirect-following on signed clients.** `@target-uri` is part of the signature base. If the server returns a 3xx redirect, the signature still binds to the original URL. Configure `httpx.AsyncClient(follow_redirects=False)` or implement a redirect handler that re-signs.
- **Clock skew > 60s.** Verifiers reject with `request_signature_window_invalid` when `created` is more than `max_skew_seconds` in the future or `expires` is past. NTP-sync both sides; investigate container hosts that drift after suspend/resume.
- **Custom JWKS fetcher losing SSRF protection.** If you implement a custom `JwksFetcher`, gate it through `validate_jwks_uri` / `resolve_and_validate_host` — otherwise an attacker who controls a `jwks_uri` can pivot against your internal network.
- **Per-keyid replay cap.** The default `InMemoryReplayStore` caps at 1,000,000 entries per keyid (configurable via `per_keyid_cap`). Sustained > 3k QPS per signing key will trip `request_signature_rate_abuse`. For multi-process deployments, use `PgReplayStore` (the `[pg]` extra) or roll your own backing the `ReplayStore` Protocol.
- **Step-B shim surviving into production.** The `try/except SignatureVerificationError → log → fall through` pattern from step B silently turns every required-signed op into an unsigned op once `required_for` is set. Before flipping, grep your codebase for `SignatureVerificationError` and confirm the shim is gone. Months later, if you inherit the repo, re-grep.
- **`current_operation` ContextVar leaking into background tasks.** `signing_operation()` sets a ContextVar that copies into `asyncio.create_task` children. Don't spawn unrelated network calls inside a signing scope — they'd inherit the operation name and sign requests the caller didn't intend to sign.

## 5. Verification checklist before enforcing

- [ ] At least one signed request from a staging counterparty has been verified end-to-end — `verify_starlette_request` returns a `VerifiedSigner` in your handler — before flipping `required_for`.
- [ ] `jwks_uri` returns your current kid (and only your current kid, once rotation is complete).
- [ ] `get_adcp_capabilities.request_signing.required_for` matches `VerifierCapability.required_for`.
- [ ] Revocation source is configured (`revocation_checker` or `revocation_list` is non-None) if you've published any revocations — otherwise revoked keys are accepted silently.
- [ ] Replay store strategy is explicit: default `InMemoryReplayStore` for single-instance, `PgReplayStore` (or equivalent shared store) for multi-instance.
- [ ] Logs from step B show zero unexpected failures over at least one full deploy cycle of your slowest counterparty.
- [ ] No step-B shim (`try/except SignatureVerificationError → log → continue`) remains in the verifier wiring.
- [ ] Redirect-following is disabled on every signing client.
- [ ] Clock sync monitoring is in place on signer and verifier hosts.

## Related

- [`adcp.signing.install_signing_event_hook`](../src/adcp/signing/client.py) — buyer-side preset for adapters not using `ADCPClient`.
- [`ADCPClient(signing=...)`](../src/adcp/client.py) — the higher-level client integration; signing is wired automatically once you pass a `SigningConfig`.
- [adcp-go's MIGRATION.md](https://github.com/adcontextprotocol/adcp-go/blob/main/adcp/signing/MIGRATION.md) — Go sibling guide.
- [AdCP signing guide (docs PR)](https://github.com/adcontextprotocol/adcp/pull/3064) — the cross-language end-user walkthrough.
