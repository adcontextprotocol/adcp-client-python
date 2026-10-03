# Prepare notifications for durable delivery

`WebhookSender.prepare_*` methods build an immutable `PreparedWebhook` without
network I/O. Store its body bytes, destination, key and extra headers verbatim.
`send_prepared` signs each attempt afresh while preserving the body and key.
Receivers must deduplicate by the stable `idempotency_key`, scoped to the
authenticated sender. Delivery queues provide at-least-once delivery.

```python
from adcp import WebhookSender

prepared = WebhookSender.prepare_account_status_changed(
    url="https://buyer.example/hooks",
    payload={
        "notification_id": "account-transition-42",
        "subscriber_id": "buyer-primary",
        "account_id": "acct_1",
        "previous_status": "pending_approval",
        "status": "active",
        "observed_at": "2026-10-02T12:00:00Z",
        "reason_code": "seller_approved",
    },
)
# Persist prepared before delivery. A worker later calls:
# await sender.send_prepared(prepared)
```

Named builders accept wire mappings or Pydantic models. They fill absent
`notification_type`, `fired_at` and `idempotency_key` fields and validate the
complete envelope against the bundled canonical schema. An explicitly supplied
key takes precedence over a payload key. Caller-supplied timestamps and logical
`notification_id` values are preserved.

The named surface covers `prepare_account_status_changed`,
`prepare_account_change_recorded`, `prepare_capabilities_changed`,
`prepare_principal_changed`, `prepare_creative_status_changed`,
`prepare_creative_assignment_changed`, `prepare_creative_purged`, and
`prepare_indicators_changed`. Their `send_*` counterparts use the same preparation.

Existing one-shot notifications also have preparation counterparts:
`prepare_wholesale_feed`, `prepare_wholesale_feed_to_subscription`,
`prepare_collection_list_changed`, `prepare_property_list_changed`,
`prepare_artifact_webhook`, `prepare_revocation_notification`, and `prepare_raw`.
Wholesale preparation checks event type, entity type, cache scope and subscribed
filters before returning. Product notifications default to the `legacy` payload view;
`product_payload_view="canonical"` opts in to canonical fields, and subscription
preparation uses the registered view. Raw preparation validates recognized notification
shapes; extension payloads remain available through `prepare_raw`. A key is generated if neither the keyword nor payload
supplies one. Business authorization and subscription lookup remain the publisher's job.

Proof-of-control challenges remain immediate exchanges through
`send_webhook_challenge`; they have a response echo contract and deliberately
carry no notification idempotency field. Task publication continues through
`prepare_mcp` and the task outbox.

Known notification envelopes that previously passed through a sender without
validation now fail preparation when malformed. Fix invalid fields before
persisting them; retries cannot repair an invalid immutable envelope.
