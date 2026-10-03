"""The complete non-task preparation surface validates before persistence."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
from pydantic import BaseModel

from adcp.webhooks import WebhookSender

URL = "https://buyer.example/webhooks"
KEY = "whk_notification_123456789"
AT = "2026-10-02T12:00:00Z"


def notification_payload(kind: str) -> dict[str, Any]:
    common = {"notification_id": "transition_1", "subscriber_id": "buyer_1", "fired_at": AT}
    fields = {
        "account_status_changed": {
            "account_id": "acct_1",
            "previous_status": "pending_approval",
            "status": "active",
            "observed_at": AT,
            "reason_code": "seller_approved",
        },
        "account_change_recorded": {
            "account_id": "acct_1",
            "change_id": "change_1",
            "recorded_at": AT,
            "resource": {"type": "creative", "resource_id": "cr_1"},
            "action": "updated",
        },
        "capabilities_changed": {
            "agent_url": "https://seller.example",
            "changed_at": AT,
            "reason": "configuration_changed",
            "capabilities_version": "v2",
        },
        "principal_changed": {
            "agent_url": "https://seller.example",
            "changed_at": AT,
            "reason": "destination_state_changed",
        },
        "creative_status_changed": {
            "account_id": "acct_1",
            "creative_id": "cr_1",
            "transition": {"from": "pending_review", "to": "approved", "observed_at": AT},
            "reason_code": "review_passed",
            "initiator": "seller",
        },
        "creative_assignment_changed": {
            "account_id": "acct_1",
            "media_buy_id": "mb_1",
            "package_id": "pkg_1",
            "creative_id": "cr_1",
            "change_kind": "assigned",
            "observed_at": AT,
        },
        "creative_purged": {
            "account_id": "acct_1",
            "creative_id": "cr_1",
            "purge_kind": "hard",
            "purged_at": AT,
            "reason_code": "legal_erasure",
            "initiator": "seller",
        },
        "indicators_changed": {
            "account_id": "acct_1",
            "media_buy_id": "mb_1",
            "relationship_kind": "media_buy",
            "change_kind": "updated",
            "changed_indicator_types": ["pacing_risk"],
            "observed_at": AT,
        },
    }
    return {**common, **fields[kind]}


TYPED_KINDS = [
    "account_status_changed",
    "account_change_recorded",
    "capabilities_changed",
    "principal_changed",
    "creative_status_changed",
    "creative_assignment_changed",
    "creative_purged",
    "indicators_changed",
]


def preparation_cases() -> list[tuple[str, dict[str, Any]]]:
    cases = [(kind, {"payload": notification_payload(kind)}) for kind in TYPED_KINDS]
    cases += [
        (
            "revocation_notification",
            {"rights_id": "rights_1", "brand_id": "brand_1", "reason": "ended", "effective_at": AT},
        ),
        (
            "artifact_webhook",
            {"media_buy_id": "mb_1", "batch_id": "batch_1", "timestamp": AT, "artifacts": []},
        ),
        ("collection_list_changed", {"list_id": "list_1", "resolved_at": AT, "signature": "sig"}),
        ("property_list_changed", {"list_id": "list_1", "resolved_at": AT, "signature": "sig"}),
        ("raw", {"payload": {"notification_type": "extension.changed", "nested": {"v": 1}}}),
    ]
    for kind, entity in [("product.removed", "product"), ("signal.removed", "signal")]:
        event = {
            "event_id": "018f13f8-7b40-7000-8000-000000000123",
            "event_type": kind,
            "entity_type": entity,
            "entity_id": "entity_1",
            "created_at": AT,
            "payload": {
                "applies_to": {"scope": "public"},
                ("product_id" if entity == "product" else "signal_agent_segment_id"): "entity_1",
            },
        }
        args = {
            "account_id": "acct_1",
            "notification_type": kind,
            "wholesale_feed_version": "wf_1",
            "cache_scope": "public",
            "event": event,
            "fired_at": AT,
        }
        cases.append(("wholesale_feed", {"subscriber_id": "buyer_1", **args}))
        cases.append(
            (
                "wholesale_feed_to_subscription",
                {
                    "subscription": {"subscriber_id": "buyer_1", "url": URL, "event_types": [kind]},
                    **args,
                },
            )
        )
    return cases


@pytest.mark.parametrize("kind,args", preparation_cases())
async def test_prepare_and_one_shot_send_have_identical_bytes(
    kind: str, args: dict[str, Any]
) -> None:
    # Fixed timestamp/key make the byte comparison meaningful for retry storage.
    requests: list[httpx.Request] = []
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: requests.append(r) or httpx.Response(204))
    ) as client:
        sender = WebhookSender.from_bearer_token("test", client=client)
        kwargs = {"idempotency_key": KEY, "extra_headers": {"X-Trace": "trace"}, **args}
        if kind != "wholesale_feed_to_subscription":
            kwargs["url"] = URL
        prepared = getattr(sender, "prepare_" + kind)(**kwargs)
        result = await getattr(sender, "send_" + kind)(**kwargs)
        assert result.ok
        assert requests[0].content == prepared.body
        assert json.loads(prepared.body)["idempotency_key"] == KEY
        assert requests[0].headers["X-Trace"] == "trace"


@pytest.mark.parametrize("kind", TYPED_KINDS)
def test_invalid_typed_envelopes_fail_during_preparation(kind: str) -> None:
    with pytest.raises(ValueError, match="schema validation"):
        getattr(WebhookSender, "prepare_" + kind)(url=URL, payload={})


def test_preparation_freezes_nested_input_and_generates_key() -> None:
    payload = notification_payload("account_change_recorded")
    headers = {"X-Trace": "original"}
    prepared = WebhookSender.prepare_account_change_recorded(
        url=URL, payload=payload, extra_headers=headers
    )
    payload["resource"]["resource_id"] = "changed"
    headers["X-Trace"] = "changed"
    assert json.loads(prepared.body)["resource"]["resource_id"] == "cr_1"
    assert prepared.extra_headers["X-Trace"] == "original"
    assert len(prepared.idempotency_key) >= 16


def test_conditional_creative_transition_is_validated() -> None:
    payload = notification_payload("creative_status_changed")
    payload["transition"]["to"] = "suspended"  # pending_review cannot become suspended.
    with pytest.raises(ValueError, match="schema validation"):
        WebhookSender.prepare_creative_status_changed(url=URL, payload=payload)


def test_type_and_wholesale_filter_validation_happen_before_enqueue() -> None:
    with pytest.raises(ValueError, match="does not match"):
        WebhookSender.prepare_principal_changed(
            url=URL, payload={"notification_type": "capabilities.changed"}
        )
    _, args = next((k, a) for k, a in preparation_cases() if k == "wholesale_feed")
    with pytest.raises(ValueError, match="subscription"):
        WebhookSender.prepare_wholesale_feed(
            url=URL, subscription_event_types=["signal.removed"], **args
        )


def test_pydantic_payload_is_supported() -> None:
    class Payload(BaseModel):
        notification_id: str = "transition_1"
        subscriber_id: str = "buyer_1"
        agent_url: str = "https://seller.example"
        changed_at: str = AT
        reason: str = "other"

    prepared = WebhookSender.prepare_principal_changed(url=URL, payload=Payload())
    assert json.loads(prepared.body)["notification_type"] == "principal.changed"


@pytest.mark.parametrize(
    "kind,args",
    [
        (k, a)
        for k, a in preparation_cases()
        if k
        in {
            "artifact_webhook",
            "revocation_notification",
            "property_list_changed",
            "collection_list_changed",
        }
    ],
)
def test_legacy_envelopes_validate_dates(kind: str, args: dict[str, Any]) -> None:
    field = {"artifact_webhook": "timestamp", "revocation_notification": "effective_at"}.get(
        kind, "resolved_at"
    )
    with pytest.raises(ValueError, match="schema validation"):
        getattr(WebhookSender, "prepare_" + kind)(url=URL, **{**args, field: "bad-date"})


def test_bulk_feed_and_generated_raw_keys() -> None:
    prepared = WebhookSender.prepare_wholesale_feed(
        url=URL,
        subscriber_id="buyer_1",
        account_id="acct_1",
        notification_type="wholesale_feed.bulk_change",
        wholesale_feed_version="wf_2",
        cache_scope="public",
        event={
            "event_id": "018f13f8-7b40-7000-8000-000000000123",
            "event_type": "wholesale_feed.bulk_change",
            "entity_type": "feed",
            "entity_id": "feed_1",
            "created_at": AT,
            "payload": {
                "summary": "refresh",
                "affected_entity_type": "product",
                "affected_count": 20,
                "applies_to": {"scope": "public"},
            },
        },
    )
    assert json.loads(prepared.body)["notification_type"] == "wholesale_feed.bulk_change"
    raw = WebhookSender.prepare_raw(url=URL, payload={"notification_type": "extension.changed"})
    assert json.loads(raw.body)["idempotency_key"] == raw.idempotency_key
    assert len(raw.idempotency_key) >= 16


def test_preparation_covers_all_notification_sender_methods() -> None:
    sends = {name.removeprefix("send_") for name in vars(WebhookSender) if name.startswith("send_")}
    prepares = {
        name.removeprefix("prepare_") for name in vars(WebhookSender) if name.startswith("prepare_")
    }
    assert sends - {"prepared", "webhook_challenge"} <= prepares
