"""Opaque positions bind the whole frozen vector and semantic request."""

import base64
import json
from copy import deepcopy
from dataclasses import replace

import pytest

from adcp.reporting.feed import ReportingFeedError
from adcp.reporting.feed.request import FeedRequest
from adcp.reporting.feed.snapshot import token_position
from adcp.reporting.ledger.store import encode_cursor
from adcp.types import GetReportingStatusRequest, GetReportingStatusResponse

from ._feed_support import feed_request, feeds, mixed_case, restart, walk, without_feed

__all__ = ["feeds"]


async def test_every_semantic_filter_and_both_principal_coordinates_are_bound(feeds):
    h = feeds
    s, _, _ = await mixed_case(h)
    request = feed_request(s)
    page = await h.store.read_reporting_feed(request, caller=s.binding.principal)
    tokens = (page["pagination"]["cursor"], page["changes_checkpoint"])
    variations = [
        {"delivery_config_ids": ["other"]},
        {"media_buy_ids": ["other"]},
        {"feed_purposes": ["analytics"]},
        {"health": ["healthy"]},
        {"finality": ["snapshot"]},
        {"period": {"start": "2026-09-01T00:00:00Z", "end": "2026-09-02T00:00:00Z"}},
        {"ext": {"vendor": {"scope": "changed"}}},
    ]
    before = await h.image()
    for token, parameter in zip(tokens, ("cursor", "changes_after")):
        for changes in variations:
            changed = feed_request(s, **changes)
            if parameter == "cursor":
                changed["pagination"]["cursor"] = token
            else:
                changed[parameter] = token
            with pytest.raises(ReportingFeedError) as error:
                await h.store.read_reporting_feed(changed, caller=s.binding.principal)
            assert error.value.code == "INVALID_CHECKPOINT"
        for caller in (
            replace(s.binding.principal, account_id="other-account"),
            replace(s.binding.principal, consumer_id="other-consumer"),
        ):
            changed = feed_request(s)
            if parameter == "cursor":
                changed["pagination"]["cursor"] = token
            else:
                changed[parameter] = token
            with pytest.raises(ReportingFeedError) as error:
                await h.store.read_reporting_feed(changed, caller=caller)
            assert error.value.code == "INVALID_CHECKPOINT"
    assert await h.image() == before


async def test_tampered_positions_cannot_rebind_offset_boundary_count_or_last_key(feeds):
    h = feeds
    s, _, _ = await mixed_case(h)
    page = await h.store.read_reporting_feed(feed_request(s), caller=s.binding.principal)
    original = page["pagination"]["cursor"]
    packed = base64.urlsafe_b64decode(original[5:] + "=" * (-len(original[5:]) % 4))
    fields = json.loads(packed[:-32])
    for index, value in [
        (0, True),
        (0, 2),
        (1, "checkpoint"),
        (2, "rpfs_" + "0" * 32),
        (3, -1),
        (3, True),
        (3, 2),
        (4, "f" * 64),
        (5, "0" * 64),
    ]:
        changed = deepcopy(fields)
        changed[index] = value
        forged = "rpf1." + base64.urlsafe_b64encode(
            json.dumps(changed, separators=(",", ":")).encode() + packed[-32:]
        ).decode().rstrip("=")
        req = feed_request(s, pagination={"cursor": forged})
        with pytest.raises(ReportingFeedError) as error:
            await h.store.read_reporting_feed(req, caller=s.binding.principal)
        assert error.value.code == "INVALID_CHECKPOINT"
    # Either type in the wrong field fails, even if fully authenticated.
    for req in (
        feed_request(s, changes_after=original),
        feed_request(s, pagination={"cursor": page["changes_checkpoint"]}),
    ):
        with pytest.raises(ReportingFeedError) as error:
            await h.store.read_reporting_feed(req, caller=s.binding.principal)
        assert error.value.code == "INVALID_CHECKPOINT"


async def test_unbound_legacy_and_malformed_tokens_are_explicitly_rejected(feeds):
    h = feeds
    s, _, _ = await mixed_case(h)
    before = await h.image()
    for token in (
        encode_cursor({"seq": 999999}),
        encode_cursor({"feed": "reporting-reconciliation-v1", "seq": 0}),
        "rpf1.invalid",
        "rpf1." + "a" * 2043,
        "",
        "x" * 2049,
    ):
        with pytest.raises(ReportingFeedError) as error:
            await h.store.read_reporting_feed(
                feed_request(s, changes_after=token), caller=s.binding.principal
            )
        assert error.value.code in {"INVALID_REQUEST", "INVALID_CHECKPOINT"}
    assert await h.image() == before


async def test_page_size_echo_context_and_normalized_filter_order_do_not_rebind(feeds):
    h = feeds
    s, _, _ = await mixed_case(h)
    original = feed_request(
        s,
        delivery_config_ids=["daily", "unused"],
        context={"trace": "first", "sample": 0.125},
        ext={"vendor": {"threshold": 0.5}},
    )
    first = await h.store.read_reporting_feed(original, caller=s.binding.principal)
    continued = feed_request(
        s,
        delivery_config_ids=["unused", "daily", "daily"],
        context={"trace": "second", "sample": 0.75},
        ext={"vendor": {"threshold": 0.5}},
        pagination={"cursor": first["pagination"]["cursor"], "max_results": 100},
    )
    # Duplicates are normalized only when the protocol accepts them. Generated
    # schemas may require uniqueItems, so compare the canonical reordered set.
    continued["delivery_config_ids"] = ["unused", "daily"]
    second = await (await restart(h)).read_reporting_feed(
        continued, caller=s.binding.principal, consumer_status_enabled=True
    )
    assert second["changes_checkpoint"] == first["changes_checkpoint"]
    assert second["ledger_snapshot_id"] == first["ledger_snapshot_id"]
    assert second["context"] == {"trace": "second", "sample": 0.75}
    assert second["pagination"] == {"total_count": 6, "has_more": False}


async def test_actual_generated_client_tokens_fit_maximum_supported_url_principal(feeds):
    h = feeds
    prefix = "https://buyer.example.test/"
    consumer = prefix + "a" * (2048 - len(prefix))
    s, _, _ = await mixed_case(h, consumer_id=consumer)
    request = GetReportingStatusRequest.model_validate(feed_request(s)).model_dump(
        mode="json", exclude_unset=True
    )
    page = GetReportingStatusResponse.model_validate(
        await h.store.read_reporting_feed(request, caller=s.binding.principal)
    )
    assert 0 < len(page.changes_checkpoint) <= 2048
    assert 0 < len(page.pagination.cursor) <= 2048
    assert consumer not in page.pagination.cursor
    again = GetReportingStatusRequest.model_validate(
        feed_request(s, pagination={"cursor": page.pagination.cursor})
    ).model_dump(mode="json", exclude_unset=True)
    tail = GetReportingStatusResponse.model_validate(
        await (await restart(h)).read_reporting_feed(again, caller=s.binding.principal)
    )
    assert tail.changes_checkpoint == page.changes_checkpoint
    assert tail.pagination.has_more is False
    checkpoint_request = GetReportingStatusRequest.model_validate(
        feed_request(s, changes_after=tail.changes_checkpoint)
    ).model_dump(mode="json", exclude_unset=True)
    empty = await h.store.read_reporting_feed(checkpoint_request, caller=s.binding.principal)
    assert empty["pagination"]["total_count"] == 0


async def test_original_changes_after_may_accompany_continuation_but_a_different_boundary_cannot(
    feeds,
):
    h = feeds
    s, _, _ = await mixed_case(h)
    # An empty filtered feed can advance through the same public vector, but
    # changing filters never grants an incremental position in another scope.
    from ._receipt_support import extra_materialization

    _, _, previous = await walk(h.store, feed_request(s), s.binding.principal)
    await extra_materialization(h, s, "position-next-materialization", 2)
    req = feed_request(s, changes_after=previous)
    first = await h.store.read_reporting_feed(req, caller=s.binding.principal)
    pages, _, checkpoint = await walk(h.store, req, s.binding.principal, first=first)
    assert len(pages) == 3
    changed = feed_request(
        s, changes_after=checkpoint, pagination={"cursor": first["pagination"]["cursor"]}
    )
    before = without_feed(await h.image())
    with pytest.raises(ReportingFeedError) as error:
        await h.store.read_reporting_feed(changed, caller=s.binding.principal)
    assert error.value.code == "INVALID_CHECKPOINT"
    assert without_feed(await h.image()) == before


@pytest.mark.parametrize("value", [None, False, 0, 101, 1.0, "1"])
def test_page_limit_requires_a_bounded_json_integer(value):
    with pytest.raises(ReportingFeedError):
        FeedRequest.parse(
            {
                "view": "periods",
                "account": {"account_id": "acct_a"},
                "pagination": {"max_results": value},
            }
        )


def test_decoding_an_opaque_position_does_not_authorize_it():
    with pytest.raises(ReportingFeedError):
        token_position(encode_cursor({"seq": 1}))
