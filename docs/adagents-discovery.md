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
result, none of it checked against the fetched domain.

To mark what the gate excludes instead of losing it silently, resolve both
ways and take the difference. The excluded properties are not authorized. A
property identified only by domains can be checked with
`verify_cross_domain_properties` (below), which asks the publishers it names
whether they vouch for it. A property carrying a bundle, store or venue
identifier stays unconfirmed: the gate refused it because a domain cannot
vouch for those identifiers, and the cross-domain check does not vouch for
them either.

```python
gated = resolve_properties_for_agent(
    document, agent_url, mode="permissive", publisher_domain="example.org"
)
everything = resolve_properties_for_agent(
    document, agent_url, mode="permissive", allow_any_domain=True
)
excluded = [p for p in everything if p not in gated]
domain_only = [
    p
    for p in excluded
    if p.get("identifiers") and all(i.get("type") == "domain" for i in p["identifiers"])
]
check = await verify_cross_domain_properties(
    domain_only, source_domain="example.org", agent_url=agent_url
)
unconfirmed = [p for p in excluded if p not in domain_only]  # never restore these
```

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

The result puts every property in exactly one list, and leaves what to do
with each to the caller:

| List | Meaning | `reason` |
| --- | --- | --- |
| `verified` | On `A`, or every named publisher vouched for it | — |
| `unverified` | A named publisher's file loaded and does not authorize the claim, or the claim names no usable hostname | `not_vouched`, `invalid_domain` |
| `undetermined` | Not checked: a file did not load, or the `max_domains` budget ran out | `fetch_failed`, `domain_limit` |
| `domainless` | Names no domain, so no publisher can vouch for it | — |

`unverified` and `undetermined` hold `UnverifiedProperty` records with the
`domain` and the `reason`. Only `verified` is vouched for: a property naming
several publishers needs each to vouch for all of it, a bare entry in `B`'s
file vouches for nothing, and a property one publisher refuses is
`unverified` even if another publisher's file failed to load. Only domain
claims are checked; a bundle, store or venue identifier on a verified
property is as trustworthy as the resolution that produced it.

A network's adagents.json can name thousands of publishers, and at most
`max_domains` (default 50) distinct domains are fetched per call: the first
ones named, in input order, whether or not your fetcher serves them from a
cache. Calling again with the same claims fetches the same domains again. For
a large file:

- run the check outside the sync path;
- pass a `fetcher=` that caches documents by domain (it receives a domain and
  returns the parsed document);
- treat `domain_limit` as "not checked yet", and call with slices whose claims
  name at most `max_domains` distinct publishers between them. A single
  property naming more than `max_domains` publishers needs a larger budget;
- retry `fetch_failed` claims on their own schedule, so a publisher whose file
  keeps failing does not use up every call's budget.
