"""Offline wire validation shared by notification preparation and replay."""

from __future__ import annotations

import json
from typing import Any

NOTIFICATION_SCHEMAS = {
    "account.status_changed": "core/account-status-changed-webhook.json",
    "account.change_recorded": "core/account-change-recorded-webhook.json",
    "capabilities.changed": "core/capabilities-changed-webhook.json",
    "principal.changed": "core/principal-changed-webhook.json",
    "creative.status_changed": "creative/creative-status-changed-webhook.json",
    "creative.assignment_changed": "creative/creative-assignment-changed-webhook.json",
    "creative.purged": "creative/creative-purged-webhook.json",
    "indicators.changed": "core/indicators-changed-webhook.json",
    "collection_list_changed": "collection/collection-list-changed-webhook.json",
    "property_list_changed": "property/property-list-changed-webhook.json",
    "artifact-webhook": "content-standards/artifact-webhook-payload.json",
    "revocation-notification": "brand/revocation-notification.json",
    **{
        kind: "core/wholesale-feed-webhook.json"
        for kind in (
            "product.created",
            "product.updated",
            "product.priced",
            "product.removed",
            "signal.created",
            "signal.updated",
            "signal.priced",
            "signal.removed",
            "wholesale_feed.bulk_change",
        )
    },
}
MAX_NOTIFICATION_BODY_BYTES = 10 * 1024 * 1024


def validate_notification_body(
    body: bytes,
    *,
    idempotency_key: str | None = None,
    notification_type: str | None = None,
    account_id: str | None = None,
) -> dict[str, Any]:
    """Reject malformed envelopes before committing and again before HTTP.

    Legacy notifications without a type discriminator use their schema/event
    names as routing labels. Extension notifications must carry a matching
    ``notification_type``. Errors deliberately omit untrusted field values.
    """
    if not body or len(body) > MAX_NOTIFICATION_BODY_BYTES:
        raise ValueError("notification body is empty or exceeds the 10MB cap")
    payload = json.loads(body)
    if not isinstance(payload, dict):
        raise ValueError("notification body must be a JSON object")
    key = payload.get("idempotency_key")
    if not isinstance(key, str) or not key:
        raise ValueError("notification idempotency_key must be non-empty")
    if idempotency_key is not None and key != idempotency_key:
        raise ValueError("notification idempotency_key does not match its binding")
    kind = payload.get("notification_type", payload.get("event"))
    if kind is None:
        if "rights_id" in payload and "brand_id" in payload:
            kind = "revocation-notification"
        elif "artifacts" in payload and "batch_id" in payload:
            kind = "artifact-webhook"
    if notification_type is not None and kind != notification_type:
        raise ValueError("notification type does not match its binding")
    if "account_id" in payload and account_id is not None and payload["account_id"] != account_id:
        raise ValueError("notification account_id does not match its binding")
    if not isinstance(kind, str) or kind not in NOTIFICATION_SCHEMAS:
        return payload  # Raw extension payloads keep their existing escape hatch.
    from adcp.validation.schema_loader import get_named_validator

    validator = get_named_validator(NOTIFICATION_SCHEMAS[kind])
    if validator is None:
        raise ValueError("bundled notification schema is unavailable")
    error = next(validator.iter_errors(payload), None)
    if error is not None:
        raise ValueError(f"notification envelope failed schema validation ({error.validator})")
    if kind.startswith(("product.", "signal.")) or kind == "wholesale_feed.bulk_change":
        event = payload["event"]
        if payload["notification_id"] != event["event_id"]:
            raise ValueError("notification_id must match event.event_id")
        if event["event_type"] != kind:
            raise ValueError("notification_type must match event.event_type")
        expected_entity = kind.split(".", 1)[0] if kind != "wholesale_feed.bulk_change" else "feed"
        if event["entity_type"] != expected_entity:
            raise ValueError("event.entity_type does not match notification_type")
        if event["payload"]["applies_to"]["scope"] != payload["cache_scope"]:
            raise ValueError("cache_scope must match event.payload.applies_to.scope")
    return payload
