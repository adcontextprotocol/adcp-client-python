# Reporting correlation on AdCP 3.2

AdCP 3.2.1 requires a buyer-supplied `operation_id` in scheduled delivery
webhook envelopes, but its `reporting_webhook` schema does not declare that
field. [adcp#7885](https://github.com/adcontextprotocol/adcp/issues/7885)
tracks the gap; [adcp#7895](https://github.com/adcontextprotocol/adcp/pull/7895)
proposes the field for 3.3.

The SDK restores an optional `ReportingWebhook.operation_id` as an interim
extension. The published registration schema permits additional fields.
Use it only after confirming seller support during integration onboarding;
a 3.2 seller may accept and ignore an unknown field. This agreement does not
change the released 3.2 schema or require all 3.2 implementations to support it.
It needs no `ext.{namespace}` wrapper inside `reporting_webhook`.

```python
from adcp.types import ReportingWebhook

registration = ReportingWebhook(
    url="https://buyer.example.com/webhooks/reporting",
    operation_id="op_reporting_001",
    authentication={
        "schemes": ["Bearer"],
        "credentials": "example_reporting_credential_32_chars",
    },
    reporting_frequency="daily",
)
wire_registration = registration.model_dump(mode="json")
```

Supply that registration through `buy_products`, `accept_proposal`,
`control_media_buy`, or the legacy `create_media_buy` / `update_media_buy`
API where supported. IDs use 1–255 characters from `A–Z`, `a–z`, `0–9`,
`_`, `.`, `:`, and `-`, matching `PushNotificationConfig.operation_id`.
Omission and `None` remain valid; default serialization omits an absent ID.
Explicit malformed values fail model validation. Historical version-scoped
models keep their pinned schema contracts, including the beta.6-required field
in `adcp.types.v32`.

For a seller implementing this agreement, persist the ID with the reporting
registration, then pass that exact value to `WebhookSender.send_mcp(operation_id=...)`
for every `media_buy_delivery` report. Preserve it on retries and keep existing
authentication, required envelope fields, and per-delivery idempotency handling.
The reporting-stream ID is independent of any task-status callback ID and does
not replace a delivery's `idempotency_key`. Never derive it from the URL or
fabricate it from a seller resource ID.

For existing registrations without a buyer-supplied ID, flag the missing value
and suppress scheduled sends on this interim path. Ask buyers to update their
registration with an ID before resuming delivery. `get_media_buy_delivery`
remains available while arranging support. Updating the SDK alone does not
backfill seller storage or restart reports.

Verify buyer serialization → seller validation → durable registration →
scheduled send → authenticated receipt end to end before deployment. When a
later protocol release defines the core field, its definition takes precedence
over this interim agreement; remove the compatibility patch once generated
models retain the field and the regression suite still passes.
