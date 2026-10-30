# Publisher agent discovery

Agent lookup treats host case and explicit default ports as equivalent, and
continues to match HTTP and HTTPS URLs. Paths remain case-sensitive; trailing
slashes, query strings and fragments do not affect matching. Non-default ports
remain significant.

Use the listing helper alongside property resolution to distinguish an agent
that is listed but whose selectors match no properties from an unlisted agent:

```python
from adcp import find_authorized_agent_entries, get_properties_by_agent

entries = find_authorized_agent_entries(document, "https://sales.example.com/mcp")
properties = get_properties_by_agent(document, "https://sales.example.com/mcp")
listed_but_unbound = bool(entries) and not properties
```

Malformed sibling entries and non-string or empty URL values are skipped during
lookup. `validate_adagents_structure(document)` reports invalid URL types through
its existing `missing_url` diagnostic. The listing helper returns matching entry
objects in source order and does not modify the document.

## Attributing properties to a publisher

After resolving the agent's selectors, use `properties_for_publisher_domain`
to select a publisher's inventory from a shared manifest:

```python
from adcp import get_properties_by_agent, properties_for_publisher_domain

authorized = get_properties_by_agent(document, agent_url)
properties = properties_for_publisher_domain(authorized, "www.example.org")
```

An explicit property `publisher_domain` takes precedence over identifiers and
must name the requested publisher. Otherwise, a `type="domain"` or
`type="subdomain"` identifier must
name that publisher or any subdomain. For example, `shop.example.org` and
`ctv.example.org` both belong to `example.org`. Host case, trailing dots, IDNA
and a leading `www.` alias within the same registrable domain are normalized.
The alias rule respects both public and private suffixes, so `www.com` cannot
become `com` and `www.github.io` cannot become `github.io`. Domain matching uses
`domain_matches`, including an explicit `*.example.org` pattern.
Wildcard domain inventory such as `*.example.org` belongs to `example.org`;
publisher-domain arguments must be concrete hostnames.

Properties with neither a publisher anchor nor a domain identifier, such as
bundle-only apps, are kept by default. Pass `include_unattributed=False` to
exclude them. Invalid domain claims are excluded. The helper returns original
objects in input order without changing the document. Attribution trusts the
manifest's claims; it does not establish cross-domain authority.

## Gating bare-entry resolution

When using permissive resolution, supply the domain from the fetch origin:

```python
from adcp import fetch_adagents, resolve_properties_for_agent

document = await fetch_adagents(domain, validate_structure=False)
properties = resolve_properties_for_agent(
    document, agent_url, mode="permissive", publisher_domain=domain
)
```

For bare-entry fallback, each property must have a domain identifier naming
the publisher or a subdomain. The property's own `publisher_domain` cannot
satisfy this gate. Apps, venues and other inventory without a matching domain
identifier are excluded. Typed authorization selectors keep their existing
behavior in both modes. Omitting `publisher_domain` preserves the existing
permissive behavior; strict resolution remains the default.

## Verifying cross-domain claims

Use `verify_cross_domain_properties` on resolved inventory before trusting
claims about other domains:

```python
from adcp import (
    fetch_adagents,
    get_properties_by_agent,
    properties_for_publisher_domain,
    verify_cross_domain_properties,
)

source_domain = "network.example"
document = await fetch_adagents(source_domain)
authorized = get_properties_by_agent(document, agent_url)
verified = await verify_cross_domain_properties(
    authorized, source_domain, agent_url
)
publisher_properties = properties_for_publisher_domain(verified, "publisher.example")
```

The source domain must come from the trusted fetch origin. For every external
publisher anchor and domain or subdomain identifier, the helper fetches that domain's own
manifest and strictly resolves the same agent's selectors. The remote
authorization must cover the claimed inventory and be attributable to the
checked domain. A matching agent URL alone is insufficient. All claimed typed
identifiers must match an authorized property of the same type, using
`identifiers_match`; a matching property ID cannot override different domain
or bundle identifiers. When identifiers are absent, a publisher-scoped
property ID is used.

A verified publisher anchor also covers that publisher's subdomain identifiers.
Parent domains and unrelated domains require their own verification. Wildcard
inventory is verified at its base hostname, and Unicode and IDNA domain
identities compare equivalently. The permissive gate continues to require
`type="domain"`; attribution and verification also recognize `type="subdomain"`.

The default fetcher is `fetch_adagents`, which applies the existing URL/DNS,
response size and redirect protections, follows `authoritative_location`, and
discovers ads.txt `MANAGERDOMAIN` delegation. Delegation still requires the
resolved manifest to authorize inventory attributable to the checked publisher;
an unanchored app in a shared manager file is insufficient. Remote revocations
override authorization. A missing, invalid or unavailable manifest excludes
that domain's claims and logs the failure at debug level. Each external domain
is fetched once per call, sequentially, without recursively verifying its
other claims. There is no persistent verification cache, so later calls can
observe revocations.

Local inventory, including subdomains of the source, passes through. Inventory
with no domain identifier or publisher anchor also passes through because
there is no remote domain to check. Use `include_unattributed=False` during
attribution to exclude it, and use the domain gate above for bare entries.

For custom timeouts, deterministic tests or caller-controlled discovery, pass
an async `fetcher(domain) -> dict`. It must provide secure discovery and follow
publisher delegation. For example:

```python
async def fetch_publisher(domain: str) -> dict:
    return await fetch_adagents(domain, timeout=5.0)

verified = await verify_cross_domain_properties(
    authorized, source_domain, agent_url, fetcher=fetch_publisher
)
```
