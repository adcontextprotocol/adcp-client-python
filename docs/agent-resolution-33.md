# Agent resolution and publisher pins in AdCP 3.3

This implementation follows [adcp#7882](https://github.com/adcontextprotocol/adcp/pull/7882)
at commit `175bcd3b023244aeb97dbdcce8fed674ebd61302`. The protocol change
is still under review. Historical bundled 3.1/3.2 schemas retain their published
verifier constraints; these helpers implement the new canonical matching rules.

Agent URLs are the primary selector. Scheme/host case, default ports, dot
segments, and percent-encoded unreserved characters normalize; trailing slashes
and tenant paths remain significant. Optional type and id selectors only narrow
a URL match. Duplicate canonical matches within one collection fail, including
duplicates with identical ids. House Portfolio operator discovery scans
`house.agents[]` and all inline `brands[].agents[]`; repeated attestations across
collections count once when their type and resolved JWKS source agree.

Direct resolver construction and the shared resolver builder now require
`agent_url`:

```python
from adcp.signing import BrandJsonJwksResolver

resolver = BrandJsonJwksResolver(
    "https://operator.example.com/brand.json",
    agent_url="https://operator.example.com/sales",
    agent_type="sales",
)
```

Direct resolvers use operator-style collection selection, including all sibling
collections in a House Portfolio unless `brand_id` selects one inline brand.
For a relying-party record, select the surface's applicable collection explicitly;
use `resolve_governance_jwks` for governance rather than an unscoped direct resolver.
For operator discovery, `async_resolve_agent` and `verify_from_agent_url` invoke
`get_adcp_capabilities` via MCP by default, or A2A with `protocol="a2a"`. They use
the advertised `identity.brand_json_url`, enforce origin binding, and check every
declared key origin. Cross-domain origin binding accepts `authorized_operators`
only on a House Portfolio; its account-level brand/country scopes do not restrict
key discovery. Discovery does not reuse onboarding mappings, so each call
reconfirms the advertised operator record.

[Discovery step 7](https://github.com/adcontextprotocol/adcp/blob/175bcd3b023244aeb97dbdcce8fed674ebd61302/docs/building/by-layer/L1/security.mdx#L1331)
checks every advertised `identity.key_origins` purpose against the selected
agent's JWKS source, including purposes other than the signature being verified.
A different host for any purpose rejects discovery with
`request_signature_key_origin_mismatch`, even when the active purpose matches.
The SDK follows this explicit all-purpose rule. The draft's separate guidance
on origin separation remains in tension with that rule pending clarification.

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
failures return `webhook_signature_key_unknown` and log the specific
`request_signature_*` cause. Fallback document resolution follows at most one
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
