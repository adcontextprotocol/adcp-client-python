# Migrating from Python SDK 7 to 8

SDK 8 beta also updates the generated protocol surface from AdCP 3.1.15 to
AdCP 3.2.0-beta.0 and adds the compact product/media-buy lifecycle. The old
3.x lifecycle remains supported. See
[Migrating an integration from AdCP 3.1 to 3.2 beta](MIGRATION_ADCP_3.1_TO_3.2.md)
for lifecycle selection, capability declarations, and the compatibility test
matrix.

SDK 8 makes the legacy `ADCPClient.handle_webhook()` convenience path fail
closed. Calls without a configured `webhook_secret` no longer accept unsigned
MCP callbacks.

For AdCP-conformant public endpoints, migrate delivery to `WebhookReceiver`.
It verifies RFC 9421 signatures, deduplicates retries, and parses the
authenticated raw body. Construct it using the
[complete receiver quickstart](README.md#signed-webhooks-adcp-30-receiver-quickstart),
then pass the unchanged request to it:

```python
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

## Webhook activity metadata

`ActivityType.WEBHOOK_RECEIVED` no longer copies the complete callback into
`Activity.metadata["payload"]`. The metadata now contains only `task_id`,
`status`, and `protocol`; `operation_id` and `task_type` remain top-level
activity fields. Update telemetry consumers that read results or tokens from
the old payload field. Process business data from the verified webhook result
instead of exporting it through activity telemetry.
