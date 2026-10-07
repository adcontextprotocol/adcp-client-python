# Agent resolution and publisher pins in AdCP 3.3

This implementation follows [adcp#7882](https://github.com/adcontextprotocol/adcp/pull/7882)
at commit `175bcd3b023244aeb97dbdcce8fed674ebd61302`. The protocol change
is still under review. Historical bundled 3.1/3.2 schemas retain their published
verifier constraints; these helpers implement the new canonical matching rules.

> **Signing agent's operator:** The operator here publishes the `brand.json`
> listing the signing agent and its keys. Discover that record from the agent's
> `get_adcp_capabilities` response (`identity.brand_json_url`), or configure it
> at onboarding. It never means the `operator` or `brand` in the request's account.
> These are different roles even when their domains coincide. Do not derive key
> discovery from request-body fields.

## Discover from the agent URL

Start with the signing agent's URL from your trusted counterparty configuration.
For request verification, use `verify_from_agent_url`; for key discovery alone,
use `async_resolve_agent`:

```python
from adcp.signing import InMemoryReplayStore, async_resolve_agent, verify_from_agent_url

# Single-process example: create once outside the request handler.
# Across replicas, use a shared replay-store implementation instead.
replay_store = InMemoryReplayStore()
buyer_agent_url = "https://buying-agent.example.com/mcp"  # onboarded agent URL

# In your request handler:
verified = await verify_from_agent_url(
    request,
    agent_url=buyer_agent_url,
    operation="create_media_buy",
    replay_store=replay_store,
)

# Alternatively, discover keys without verifying a request:
resolution = await async_resolve_agent(buyer_agent_url)
```

These helpers invoke `get_adcp_capabilities` via MCP by default, or A2A with
`protocol="a2a"`. They use the advertised `identity.brand_json_url`, enforce
origin binding, and check every declared key origin. Cross-domain origin binding
accepts `authorized_operators` only on a House Portfolio; its account-level
brand/country scopes do not restrict key discovery. Discovery does not reuse
onboarding key mappings, so each call reconfirms the advertised operator record.

[Discovery step 7](https://github.com/adcontextprotocol/adcp/blob/175bcd3b023244aeb97dbdcce8fed674ebd61302/docs/building/by-layer/L1/security.mdx#L1331)
checks every advertised `identity.key_origins` purpose against the selected
agent's JWKS source, including purposes other than the signature being verified.
A different host for any purpose rejects discovery with
`request_signature_key_origin_mismatch`, even when the active purpose matches.
The SDK follows this explicit all-purpose rule. The draft's separate guidance
on origin separation remains in tension with that rule pending clarification.

`verify_from_agent_url` always takes the signer identity (`VerifiedSigner.agent_url`)
and the replay namespace from the URL the caller passed. A brand.json entry's `url`
never supplies them. Discovery selects the entry by that URL. A resolution whose
entry names a different URL fails closed with
`request_signature_agent_not_in_brand_json`. A record that lists a victim's URL
with its own keys therefore cannot verify as the victim.

## Verify onboarded counterparties

For framework verification with `serve()`, configure `JwksUriSignerKeys` or
`StaticSignerKeys` with mappings keyed by the signing agent's URL. The endpoint
or public keys must come from trusted onboarding, rather than the account:

```python
from adcp.signing import JwksUriSignerKeys, StaticSignerKeys

signer_keys = JwksUriSignerKeys({
    "https://buying-agent.example.com/mcp": "https://buying-agent.example.com/.well-known/jwks.json",
})

# Alternative for public keys exchanged at onboarding:
signer_keys = StaticSignerKeys({
    "https://buying-agent.example.com/mcp": {"keys": [buyer_public_jwk]},
})
```

These resolvers map a request's `keyid` to a configured agent and its key; they
do not discover unknown signers. When onboarding mappings replace discovery,
the [3.3 draft's shortcut rules](https://github.com/adcontextprotocol/adcp/blob/175bcd3b023244aeb97dbdcce8fed674ebd61302/docs/building/by-layer/L1/security.mdx#L1319)
require callers to establish or reconfirm the agent-to-operator mapping against
the agent's `identity.brand_json_url`, then reconfirm it within the brand.json
cache lifetime. These mapping resolvers do not perform that discovery or refresh
automatically. See the
[framework verification guide](request-signing-migration.md#framework-verification)
for wiring them into `serve()`.

## Lower-level resolver construction

Construct `BrandJsonJwksResolver` directly only when you already trust the
applicable `brand.json` record, for example from onboarding or agent discovery.
Direct construction does not perform the capabilities-based origin binding
above. Pass `agent_url` to direct resolver construction and to the shared
resolver builder:

```python
from adcp.signing import BrandJsonJwksResolver

resolver = BrandJsonJwksResolver(
    "https://signing-agent-operator.example.com/brand.json",  # trusted operator record
    agent_url="https://signing-agent-operator.example.com/sales",
    agent_type="sales",
)
```

Agent URLs are the primary selector. Scheme/host case, default ports, dot
segments, and percent-encoded unreserved characters normalize; trailing slashes
and tenant paths remain significant. Optional type and id selectors only narrow
a URL match. Duplicate canonical matches within one collection fail, including
duplicates with identical ids. House Portfolio operator discovery scans
`house.agents[]` and all inline `brands[].agents[]`; repeated attestations across
collections count once when their type and resolved JWKS source agree.

Omitting `agent_url` is deprecated in 8.1. It emits a `DeprecationWarning`
and is rejected in the next major release. Without it, `agent_type` is required
and selection falls back to the 8.0 role-based scope: `brands[brand_id].agents`
when that brand has a matching role, otherwise `house.agents`, or top-level
`agents` outside a portfolio. The fallback fails closed in two cases:

- Any ambiguity raises `agent_ambiguous` and asks for `agent_url`. More than one
  entry matching the role (and `agent_id`, when given) is ambiguous, even when
  the duplicates share an id.
- A defaulted JWKS (entry without `jwks_uri`) must share the brand.json origin,
  or resolution raises `jwks_origin_mismatch`. This restores the 8.0 guard that
  stops an attacker-controlled record from naming a victim origin and making its
  `/.well-known/jwks.json` authoritative. An explicit `jwks_uri` may live on any
  origin, as in 8.0. With `agent_url`, the caller has named the agent, so a
  defaulted JWKS on that agent's own origin is accepted.

Direct resolvers use operator-style collection selection, including all sibling
collections in a House Portfolio unless `brand_id` selects one inline brand.
For a relying-party record, select the surface's applicable collection explicitly;
use `resolve_governance_jwks` for governance rather than an unscoped direct resolver.

## Webhooks and governance

For webhooks, use the asynchronous discovery helper:

```python
from adcp.webhooks import verify_webhook_from_agent_url

sender = await verify_webhook_from_agent_url(
    method=request.method,
    url=str(request.url),
    headers=request.headers,
    body=await request.body(),
    agent_url=media_buy.seller_url,
    publisher_domains=media_buy.publisher_domains,
)
```

The seller URL and applicable publishers must come from your own media-buy
record. The helper discovers the operator's JWKS, intersects it with every
applicable publisher's `adagents.json` pin, and refreshes publisher records before
rejecting a pin mismatch. A pin never supplies a key missing from the operator
JWKS. Matching uses RFC 7638 thumbprints, so a `kid` alone or a different key with
the same `kid` does not qualify. Pins reject signatures created at or after their
`revoked_at` timestamp. Operator key-origin checks
still apply. An unknown `kid` triggers one JWKS refetch, with a 30-second cooldown.

Sellers omitting `identity.brand_json_url` use the 3.x fallback: first their host's
`/.well-known/brand.json`, then the registrable domain on a host 404. This path
skips the advertised-record origin and key-origin checks. A malformed or
unreachable advertised record never downgrades to the fallback. Webhook discovery
failures, including a JWKS fetch that failed or was refused, return
`webhook_signature_key_unknown` and log the specific `request_signature_*`
cause, which also stays on `exc.__cause__`. `exc.transient` is `True` when the
cause could clear on retry (DNS, network, or a 5xx). Fallback document resolution follows at most one
`authoritative_location` or `house` indirection and rejects indirection chains.

For a receiver that already resolves operator keys, `WebhookVerifyOptions`
accepts `publisher_pins` keyed by the applicable publisher domain. Supply
`refresh_publisher_pins`, a synchronous callback returning fresh pins for the same
publisher set. A missing pin uses `None`; an empty pin accepts no key. Configure
the operator JWKS resolver and capabilities `expected_key_origins` alongside it.

Governance issuer matching is canonical. `resolve_governance_jwks` takes the
authenticated buyer's exact record, `issuer`, and the governed request's
`brand_domain`. It selects one applicable brand collection, preserving explicit
brand overrides, and never searches sibling brands. For signed requests, pass
`VerifiedSigner.operator_brand_json`, which `verify_from_agent_url` retains from
discovery. Do not refetch a different document from the operator's host.

## Enforced in 8.1 and tightening in the next major

8.1 enforces these discovery and verification checks by default. Each one
closes an impersonation or key-confusion path, and each fails with a specific
error code:

| Check | Applies to | Why it stays enforced |
|---|---|---|
| URL-based selection and caller-derived signer identity | `async_resolve_agent`, `verify_from_agent_url`, webhook discovery | Closes the 8.0 cross-tenant impersonation, where type-based selection plus the entry `url` let a record verify as any URL it listed. |
| Agent and operator origin binding (same registrable domain, or `authorized_operators` on a House Portfolio) | Discovery helpers | The bound record becomes `VerifiedSigner.operator_brand_json` and supplies governance keys. Without binding, a record on an unrelated domain could decide the agent's keys and governance authority. Cross-domain operators publish `authorized_operators`. |
| Every declared `identity.key_origins` purpose checked | Discovery helpers and the verifier | A JWKS host that disagrees with any declared key origin means the record and keys are inconsistent, which is a key-confusion signal. Discovery step 7 requires it. |
| `jwks_source="publisher_pin"` resolvers get the key-origin check | Verifier | In AdCP 3.3, publisher pins narrow the operator JWKS. Skipping the check let a publisher-declared key verify as the operator's agent. |

`brand_id` selection without a house fallback applies only to the `agent_url`
path, which is new in 8.1. The deprecated fallback keeps the 8.0 house fallback.

The next major release requires `agent_url` on `BrandJsonJwksResolver` and
`build_brand_json_resolvers`, and removes the role-based fallback.
