# Fetching publisher manifests

`fetch_adagents` validates authorization entry structure by default. Applications
that need to diagnose publisher documents containing bare entries can explicitly
opt out and choose permissive property resolution:

```python
from adcp import fetch_adagents, validate_adagents_structure, resolve_properties_for_agent

async def inspect_publisher(domain: str, agent_url: str):
    document = await fetch_adagents(domain, validate_structure=False)
    report = validate_adagents_structure(document)
    properties = resolve_properties_for_agent(document, agent_url, mode="permissive")
    return report, properties
```

The opt-out preserves JSON-object and endpoint checks, authorization-array shape,
renderer catalog trust checks, URL/DNS SSRF gates, response size caps, and redirect
constraints. `fetch_adagents_with_cache` and `validate_adagents_domain` accept the
same options. A cached 304 continues to return the supplied cached body.

`AdagentsHTTPError` subclasses `AdagentsValidationError` and provides `status_code`
and `url`. Plain 403, rate limiting and server failures use this type. Existing
404, timeout and Cloudflare challenge exceptions retain their meanings. Statuses
other than 200 remain unsuccessful manifest responses; unsolicited 304 retains
its cache-specific validation error. Only a direct publisher 404 triggers the
best-effort MANAGERDOMAIN fallback.

`parse_managerdomains(text)` is public and returns directive values in source
order, lowercase, ignoring comments. Its output is not a trust decision: normal
fetching validates the selected manager before connecting.

For deterministic tests, `transport_factory` accepts an
`AdagentsTransportFactory`: a callable taking `(url: str, timeout: float)` and
returning an async context manager yielding `httpx.AsyncClient`. Each SDK-owned
hop gets a fresh context that is closed after the request. The SDK applies its
URL/DNS checks before invoking the factory and still disables automatic
redirects and limits response bodies. The default factory uses IP-pinned
transport with environment proxies disabled. Custom factories own DNS pinning,
proxy settings and credential policy; a mock factory should be used for tests.

An injected `client` remains caller-owned and receives only initial publisher
requests, including the initial ads.txt fallback. Redirect targets, authoritative
manifests and manager manifests use fresh factory contexts, so publisher client
headers, cookies and credentials are not forwarded to them. The SDK cannot
control the injected client's transport: its DNS pre-check alone cannot prevent
rebinding at connect time. Use the SDK's default transport in production when
that protection is required.

See the executable strict typing example in
[`tests/type_checks/adagents_fetch.py`](../tests/type_checks/adagents_fetch.py).

## ads.txt redirects

On a direct manifest 404, MANAGERDOMAIN discovery follows a manual HTTPS ads.txt
redirect walk. The [IAB ads.txt 1.1 access method, section 3.1](https://dev.iabtechlab.com/wp-content/uploads/2022/04/Ads.txt-1.1.pdf)
defines the root domain using the PSL and requires the single off-domain
location to be terminal. The SDK uses its bundled ICANN and PRIVATE PSL, so
`publisher.co.uk` and `attacker.co.uk`, or separate `github.io` tenants, have
different roots. Unknown publisher roots are refused when a redirect needs
root-domain accounting.

The walk allows up to five redirects (six requests), counting both same-root
hops and the one permitted off-root hop. Any redirect from an off-root target
is rejected, even to the same CDN host or back to the publisher. All targets
must be HTTPS without embedded credentials, pass the URL/IP gates and fresh
DNS validation, and use a new SDK-owned pinned transport after the initial URL.
The same safeguards apply when structure validation is disabled.

Relative and scheme-relative `Location` values resolve against the current URL.
Canonical repeated URLs, including changes only to host case, default port or
fragment, stop the walk. Missing/blank or malformed `Location`, downgrade,
private DNS/IP targets, network failures and exhausted hop/time budgets yield
no directives. One timeout budget covers the complete ads.txt walk, including
DNS checks, factory context entry and streamed reading; it is not reset per hop.
As with other SDK fetches, synchronous caller work cannot be preempted by an
async timeout, but an expired budget is checked before the next request.

Redirect and error bodies are closed without reading. Terminal 2xx bodies are
streamed with a 1 MiB cap (both Content-Length and accumulated decoded bytes).
Other terminal statuses produce no directives. These best-effort failures
preserve the original publisher manifest 404; debug logs explain walk failures.
MANAGERDOMAIN selection remains last-wins and manager-manifest delegation stays
one-hop. A successful manager manifest must still explicitly authorize the
publisher before applications use it for authorization decisions.

The reported `recipetineats.com` failure was also observed live on main on
2026-10-02. That observation is supplementary evidence; no test depends on the
publisher, its DNS or its CDN behaving the same way in the future. After the
repair, a supplementary fetch on the same date resolved one authorized agent.
The fixtures cover apex → www → CDN discovery entirely through deterministic
responses, including compressed terminal bodies and decoded-byte size limits.
