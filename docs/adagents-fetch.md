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
