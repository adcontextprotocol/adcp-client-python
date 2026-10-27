# Reporting correlation on AdCP 3.2

Every `media_buy_delivery` webhook envelope requires `operation_id`, and the
seller must echo a value the buyer supplied. The published 3.2.x
`reporting_webhook` registration schema has no field for it, so a buyer that
registers only `reporting_webhook` had no way to supply one.

AdCP 3.3 fixes this. [adcp#7895](https://github.com/adcontextprotocol/adcp/pull/7895)
(closing [adcp#7885](https://github.com/adcontextprotocol/adcp/issues/7885)) adds
an optional `reporting_webhook.operation_id` with the same constraints as
`push_notification_config.operation_id`: 1–255 characters from `A–Z`, `a–z`,
`0–9`, `_`, `.`, `:` and `-`. The seller MUST echo it verbatim on every
`media_buy_delivery` report to that registration and MUST NOT derive it from the
URL. A seller MUST NOT send `media_buy_delivery` reports to a registration that
omits it. The field is expected to become required in 4.0.

SDK 9 is pinned to AdCP 3.2, so it carries `ReportingWebhook.operation_id` as an
SDK extension with that exact name, optionality and set of constraints. The
3.2.x schema permits additional fields here, so no `ext.{namespace}` wrapper is
needed. On 3.2 the field is opt-in: a 3.2 seller may accept and ignore it, so
confirm seller support during onboarding. When the SDK moves to AdCP 3.3, the
generated model declares the field and the extension stops applying, with no
wire change.

## Buyers: set `operation_id` on `reporting_webhook`

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
```

Generate a unique value per reporting registration (a UUID works). It is
independent of any `push_notification_config.operation_id` on the same request,
and it is not a per-delivery `idempotency_key`. Pass the registration to
`buy_products`, `accept_proposal` or `control_media_buy`, or to the legacy
`create_media_buy` / `update_media_buy` where supported.

Correlate received reports by the payload's `operation_id`, not by URL. If you
already registered `reporting_webhook` without an ID, update the registration
with one. A seller that follows the fail-closed rule sends you no scheduled
reports until you do. Use `get_media_buy_delivery` in the meantime.

Omitting the field, or passing `None`, is valid, and default serialization
leaves an absent ID out. A malformed value fails model validation. Historical
version-scoped models keep their pinned schemas, including the beta.6 contract
in `adcp.types.v32`, where the field is required.

## Sellers: echo the stored value, skip registrations without one

Store the registration as the buyer sent it, for example with
`registration.model_dump_json()`. When a report is due, build the webhook from
that stored registration. The URL, `operation_id` and `token` then all come from
the same record:

```python
from adcp.webhooks import ReportingWebhookOperationIdMissingError, WebhookSender

async with WebhookSender.from_bearer_token(credentials) as sender:
    for buy in due_media_buys:
        try:
            result = await sender.send_media_buy_delivery(
                registration=buy.reporting_webhook,  # stored model or JSON dict
                task_id=f"delivery_report_{buy.media_buy_id}_{buy.sequence:06d}",
                result=report_for(buy),  # MediaBuyDeliveryWebhookResult or dict
            )
        except ReportingWebhookOperationIdMissingError:
            flag_for_buyer(buy)  # registration has no operation_id; no request was sent
            continue
        if not result.ok:
            await sender.resend(result)
```

For the legacy Bearer/HMAC path, pass the same registration to both helpers:
`deliver(registration, create_media_buy_delivery_webhook_payload(registration, task_id=..., result=...))`.
Durable outboxes can call `WebhookSender.prepare_media_buy_delivery(...)`,
persist the returned `PreparedWebhook`, and send it later with `send_prepared`.

What the helpers guarantee:

- `operation_id` and `token` are copied verbatim from the registration. The SDK
  never derives an ID from the URL, `media_buy_id` or anything else.
- A registration without `operation_id` raises
  `ReportingWebhookOperationIdMissingError` before any network I/O. The SDK also
  logs one WARNING on the `adcp.webhooks` logger, with
  `extra={"adcp_event": "reporting_webhook.operation_id_missing", "task_type":
  "media_buy_delivery", "notification_type": ..., "media_buy_ids": (...)}`. The
  URL and credentials are never logged. Use this record for a metric or alert,
  and keep the exception for the per-registration flag.
- A stored `operation_id` that is malformed (for example, edited by hand) fails
  `ReportingWebhook` validation instead of being sent.
- The envelope is `task_type="media_buy_delivery"` and `status="completed"`,
  with your `task_id`. `notification_id` is omitted, because delivery reports
  are point-in-time events deduped by `idempotency_key`.
  `create_mcp_webhook_payload` no longer gives `media_buy_delivery` the
  `"{task_id}.terminal"` default.

### Migrating existing registrations

Upgrading the SDK does not backfill seller storage or restart reports.
Registrations stored before buyers could send `operation_id` have none, and the
helpers skip them. Do not fill one in. Tell the affected buyers to update
`reporting_webhook` with an `operation_id`, through `update_media_buy` or
`control_media_buy`, and resume scheduled sends once the updated registration is
stored. Before deploying, test the whole path: buyer serialization, seller
validation, durable storage, scheduled send, and authenticated receipt.
