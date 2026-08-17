# Migrating from Python SDK 7 to 8

SDK 8 makes the legacy `ADCPClient.handle_webhook()` convenience path fail
closed. Calls without a configured `webhook_secret` no longer accept unsigned
MCP callbacks.

For AdCP-conformant public endpoints, migrate delivery to `WebhookReceiver`.
It verifies RFC 9421 signatures, deduplicates retries, and parses the
authenticated raw body:

```python
from adcp.webhooks import WebhookReceiver

outcome = await receiver.receive(
    method=request.method,
    url=str(request.url),
    headers=dict(request.headers),
    body=await request.body(),
)
```

If a 3.x registration explicitly selects the deprecated `HMAC-SHA256`
fallback, configure the same shared secret on `ADCPClient` and pass the raw
request body to `handle_webhook()`. An endpoint that is isolated from untrusted
networks may temporarily retain unsigned legacy callbacks with
`allow_unauthenticated_webhooks=True`; multi-agent clients must scope this
escape by agent ID.
