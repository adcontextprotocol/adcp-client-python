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

## Attributing properties to a publisher domain

One adagents.json is often served at several publisher domains (a media
group's shared file) or names a network's publishers. When syncing a file for
publisher domain `D`, `properties_for_publisher_domain(properties, D)` returns
the properties that are `D`'s:

- a property that declares `publisher_domain` belongs to `D` when that value
  names `D` (its identifiers are not consulted);
- otherwise it belongs to `D` when a `type: "domain"` identifier names `D` or
  any subdomain (`www.D`, `m.D`, `shop.D`, the pattern `*.D`);
- a property with no domain at all (an app known only by bundle ID, a DOOH
  venue) is excluded unless `include_domainless=True`.

`www.D` is treated as `D`, and hosts compare case-insensitively in A-label
form. `D` must be a registrable hostname: an IP literal, a single label or a
public suffix (`co.uk`, `github.io`) raises `AdagentsValidationError`.
Attribution does not authorize anything; apply it to the output of
`get_properties_by_agent` or `resolve_properties_for_agent`.

```python
from adcp import get_properties_by_agent, properties_for_publisher_domain

authorized = get_properties_by_agent(document, agent_url)
ours = properties_for_publisher_domain(authorized, "example.org")
apps = [
    p
    for p in properties_for_publisher_domain(authorized, "example.org", include_domainless=True)
    if p not in ours
]
```

## Permissive resolution is gated to the fetched domain

A bare `authorized_agents` entry has no typed binding, so nothing in the file
stops `attacker.example` from bare-listing an agent and claiming another
publisher's site, app or venue. `resolve_properties_for_agent(...,
mode="permissive")` therefore requires `publisher_domain=`, the domain the
document was fetched for. The bare-entry fallback then keeps only properties
whose identifiers are all `type: "domain"` and name that domain or a
subdomain. A property whose own `publisher_domain` names another domain is
dropped, as is any property with a bundle, store or venue identifier. Typed
selectors are unaffected. `allow_any_domain=True` keeps the unfiltered SDK 9
result for diagnostics.

## Verifying cross-domain claims

When a file served at `A` authorizes an agent for a property on another
publisher's domain `B`, `await verify_cross_domain_properties(properties,
source_domain=A, agent_url=...)` keeps the claim only when `B` vouches: the
document governing `B` (fetched with `fetch_adagents`, so `B`'s
`authoritative_location`, or its ads.txt `MANAGERDOMAIN` when `B` publishes no
file, is followed) authorizes the agent, under strict resolution and `B`'s
revocations, for every identifier of the property. A domain identifier is
vouched for by its registrable domain (`shop.b.com` by `b.com`); a declared
`publisher_domain` by that domain. Properties on `A` or its subdomains are
`A`'s own claims and need no fetch.

The result has `verified`, `unverified` (each an `UnverifiedProperty` with the
`domain` and a `reason`: `not_vouched`, `fetch_failed`, `invalid_domain` or
`domain_limit`) and `domainless`. The check fails closed: a property naming
several publishers needs each to vouch for all of it, a failed fetch or a bare
entry in `B`'s file vouches for nothing, and at most `max_domains` (default 50)
distinct domains are fetched per call. Pass `fetcher=` to reuse a cache; it
receives a domain and returns the parsed document.
