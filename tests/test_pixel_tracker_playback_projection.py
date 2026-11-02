"""AdCP 3.3 playback ``pixel_tracker`` events across versions (issue 1643).

3.3 adds ``start``, ``first_quartile``, ``midpoint``, ``third_quartile`` and
``complete`` to the ``pixel_tracker`` ``event`` enum. ``event`` is required, so
a 3.2 peer cannot take them by dropping the field. The 3.3 → 3.2 conversion
rewrites each to the 3.2 interim form, ``event: custom`` with
``custom_event_name`` set to the 3.3 value. The downgrade to the 3.0
``tracker_pixel`` v1 shape drops them, the interim form included. Neither path
ever turns a playback tracker into an impression tracker: a quartile URL fired
at impression time would count every impression as a quartile view.
"""

from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from adcp import ADCPClient
from adcp.canonical_formats import downgrade_pixel_tracker, downgrade_pixel_trackers
from adcp.canonical_formats.pixel_tracker import _PLAYBACK_EVENTS
from adcp.compat.versions import (
    _PLAYBACK_PIXEL_EVENTS,
    CONVERSION_METADATA_KEY,
    ADCPVersionConversionError,
    convert_payload,
)
from adcp.server import ADCPHandler, ToolContext
from adcp.server.mcp_tools import create_tool_caller
from adcp.types import PixelTrackerAsset, SyncCreativesRequest
from adcp.types.core import AgentConfig, Protocol
from adcp.validation import ValidationHookConfig, validate_request, validate_response

SOURCE = "3.3-beta.3"
PLAYBACK = ["start", "first_quartile", "midpoint", "third_quartile", "complete"]
_SCHEMAS = Path(__file__).parent.parent / "schemas" / "cache"


def _tracker(event: str, **extra: Any) -> dict[str, Any]:
    return {
        "asset_type": "pixel_tracker",
        "event": event,
        "method": "img",
        "url": "https://measurement.example/q?cb={CACHEBUSTER}",
        **extra,
    }


def _assets(event: str, **extra: Any) -> dict[str, Any]:
    return {
        "video": {
            "asset_type": "video",
            "url": "https://cdn.example/spot.mp4",
            "width": 1920,
            "height": 1080,
            "duration_ms": 15000,
        },
        "playback_tracker": _tracker(event, **extra),
        "impression_tracker": {
            "asset_type": "pixel_tracker",
            "event": "impression",
            "url": "https://measurement.example/imp",
        },
    }


def _sync_creatives(event: str, **extra: Any) -> dict[str, Any]:
    return {
        "account": {"account_id": "acct_1"},
        "idempotency_key": "sync-creatives-0001",
        "creatives": [
            {
                "creative_id": "cr_1",
                "name": "Spot",
                "format_kind": "video_hosted",
                "assets": _assets(event, **extra),
            }
        ],
    }


def _build_creative_response(event: str) -> dict[str, Any]:
    return {
        "status": "completed",
        "creative_manifest": {
            "format_id": {
                "agent_url": "https://creative.adcontextprotocol.org",
                "id": "video_hosted",
            },
            "assets": _assets(event),
        },
    }


def _downgrade_request(payload: dict[str, Any], target: str = "3.2") -> Any:
    assert validate_request("sync_creatives", payload, version=SOURCE).valid
    result = convert_payload(
        "sync_creatives",
        payload,
        direction="request",
        source_version=SOURCE,
        target_version=target,
    )
    assert validate_request("sync_creatives", result.payload, version=target).valid
    return result


def _enum(version: str) -> set[str]:
    return set(
        json.loads((_SCHEMAS / version / "enums" / "pixel-tracking-event.json").read_text())["enum"]
    )


def test_playback_set_is_the_3_3_enum_delta() -> None:
    # Both rules key on the same five values, and they are exactly what 3.3
    # added to the enum.
    assert _PLAYBACK_PIXEL_EVENTS == _PLAYBACK_EVENTS == set(PLAYBACK)
    assert _enum("3.3.0-beta.3") - _enum("3.2") == set(PLAYBACK)


# ---------------------------------------------------------------------------
# 3.3 -> 3.2: rewritten to the interim ``custom`` form
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("event", PLAYBACK)
def test_request_rewrites_playback_event_to_custom_for_3_2(event: str) -> None:
    request = _sync_creatives(event)
    result = _downgrade_request(request)

    assets = result.payload["creatives"][0]["assets"]
    assert assets["playback_tracker"] == {
        **_tracker("custom"),
        "custom_event_name": event,
    }
    assert assets["impression_tracker"] == request["creatives"][0]["assets"]["impression_tracker"]
    assert [(loss.pointer, loss.reason) for loss in result.losses] == [
        ("/creatives/0/assets/playback_tracker/event", "playback_event_as_custom")
    ]
    # The caller's payload is untouched.
    assert request["creatives"][0]["assets"]["playback_tracker"]["event"] == event


def test_rewrite_survives_the_chain_to_3_1() -> None:
    result = _downgrade_request(_sync_creatives("midpoint"), target="3.1")
    tracker = result.payload["creatives"][0]["assets"]["playback_tracker"]
    assert (tracker["event"], tracker["custom_event_name"]) == ("custom", "midpoint")


@pytest.mark.parametrize(
    "event", ["impression", "click", "viewable_mrc_50", "audible_video_complete"]
)
def test_events_3_2_already_has_are_unchanged(event: str) -> None:
    request = _sync_creatives(event)
    result = _downgrade_request(request)
    assert result.payload["creatives"] == request["creatives"]
    assert result.losses == ()


def test_interim_custom_form_passes_through_unchanged() -> None:
    request = _sync_creatives("custom", custom_event_name="first_quartile")
    result = _downgrade_request(request)
    assert result.payload["creatives"] == request["creatives"]
    assert result.losses == ()


def test_upgrade_keeps_the_interim_form_as_sent() -> None:
    # Tracker execution selectors match exactly in 3.3, so the custom
    # selector and the 3.3 event are distinct; an upgrade does not merge them.
    request = _sync_creatives("custom", custom_event_name="complete")
    result = convert_payload(
        "sync_creatives",
        request,
        direction="request",
        source_version="3.2",
        target_version=SOURCE,
    )
    tracker = result.payload["creatives"][0]["assets"]["playback_tracker"]
    assert (tracker["event"], tracker["custom_event_name"]) == ("custom", "complete")


@pytest.mark.parametrize("event", PLAYBACK)
def test_response_rewrites_playback_event_to_custom_for_3_2(event: str) -> None:
    response = _build_creative_response(event)
    assert validate_response("build_creative", response, version=SOURCE).valid
    result = convert_payload(
        "build_creative",
        response,
        direction="sync",
        source_version=SOURCE,
        target_version="3.2",
    )
    assert validate_response("build_creative", result.payload, version="3.2").valid
    tracker = result.payload["creative_manifest"]["assets"]["playback_tracker"]
    assert (tracker["event"], tracker["custom_event_name"]) == ("custom", event)


async def test_a_3_3_seller_sends_the_interim_form_to_a_3_2_buyer() -> None:
    payload = _build_creative_response("third_quartile")

    class Seller(ADCPHandler):
        async def build_creative_legacy(self, params: dict[str, Any], context: ToolContext):
            return deepcopy(payload)

    context = ToolContext()
    caller = create_tool_caller(
        Seller(),
        "build_creative",
        validation=ValidationHookConfig(requests="off", responses="strict"),
    )
    response = await caller(
        {
            "adcp_version": "3.2",
            "idempotency_key": "build-creative-0001",
            "target_format_id": {
                "agent_url": "https://creative.adcontextprotocol.org",
                "id": "video_hosted",
            },
        },
        context,
    )
    assert validate_response("build_creative", response, version="3.2").valid
    tracker = response["creative_manifest"]["assets"]["playback_tracker"]
    assert (tracker["event"], tracker["custom_event_name"]) == ("custom", "third_quartile")
    losses = context.metadata[CONVERSION_METADATA_KEY]["losses"]
    assert {loss["reason"] for loss in losses} == {"playback_event_as_custom"}


async def test_a_3_3_buyer_sends_the_interim_form_to_a_3_2_seller() -> None:
    client = ADCPClient(
        AgentConfig(id="seller", agent_uri="https://seller.example/mcp/", protocol=Protocol.MCP),
        server_version="3.2",
    )
    tool_result = MagicMock(
        isError=False,
        content=[],
        structuredContent={"creatives": [{"creative_id": "cr_1", "action": "created"}]},
    )
    session = MagicMock()
    session.call_tool = AsyncMock(return_value=tool_result)
    client.adapter._get_session = AsyncMock(return_value=session)

    result = await client.sync_creatives(
        SyncCreativesRequest.model_validate(_sync_creatives("start"))
    )

    assert result.success, result.error
    sent = session.call_tool.await_args.args[1]
    assert validate_request("sync_creatives", sent, version="3.2").valid
    assets = sent["creatives"][0]["assets"]
    assert (
        assets["playback_tracker"]["event"],
        assets["playback_tracker"]["custom_event_name"],
    ) == (
        "custom",
        "start",
    )
    assert assets["impression_tracker"]["event"] == "impression"


# ---------------------------------------------------------------------------
# 3.0: dropped, the interim form included, never an impression tracker
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("method", ["img", "js"])
@pytest.mark.parametrize("event", PLAYBACK)
def test_v1_downgrade_drops_each_playback_event(event: str, method: str) -> None:
    pixel = PixelTrackerAsset.model_validate(_tracker(event, method=method))
    result = downgrade_pixel_tracker(pixel)
    assert result.v1 is None
    assert result.advisory is not None
    assert result.advisory.code == "PIXEL_TRACKER_LOSSY_DOWNGRADE"
    assert result.advisory.details["source_event"] == event
    assert result.advisory.details["v1_asset_id"] is None


@pytest.mark.parametrize("method", ["img", "js"])
@pytest.mark.parametrize("event", PLAYBACK)
def test_v1_downgrade_drops_the_interim_custom_form(event: str, method: str) -> None:
    # The playback row takes precedence over the generic ``custom`` and
    # ``method: js`` rows, which would land on impression_tracker.
    pixel = PixelTrackerAsset.model_validate(
        _tracker("custom", method=method, custom_event_name=event)
    )
    result = downgrade_pixel_tracker(pixel)
    assert result.v1 is None
    assert result.advisory is not None
    assert result.advisory.code == "PIXEL_TRACKER_LOSSY_DOWNGRADE"
    assert result.advisory.details["source_custom_event_name"] == event


def test_no_impression_slot_is_filled_from_a_playback_event() -> None:
    pixels = [PixelTrackerAsset.model_validate(_tracker(event)) for event in PLAYBACK]
    pixels += [
        PixelTrackerAsset.model_validate(_tracker("custom", custom_event_name=event))
        for event in PLAYBACK
    ]
    pixels.append(
        PixelTrackerAsset.model_validate(
            {"asset_type": "pixel_tracker", "event": "impression", "url": "https://m.example/imp"}
        )
    )
    result = downgrade_pixel_trackers(pixels)
    # Only the genuine impression tracker survives.
    assert [(item.asset_id, item.url) for item in result.items] == [
        ("impression_tracker", "https://m.example/imp")
    ]
    assert len(result.advisories) == 2 * len(PLAYBACK)


def test_3_0_conversion_never_emits_a_playback_tracker() -> None:
    # The schema-driven conversion has no 3.0 shape for pixel_tracker at all;
    # the v1 projection above is the 3.0 path. It refuses rather than send
    # the rewritten tracker on.
    request = _sync_creatives("first_quartile")
    creative = request["creatives"][0]
    del creative["format_kind"]
    creative["format_id"] = {
        "agent_url": "https://creative.adcontextprotocol.org",
        "id": "video_hosted",
    }
    with pytest.raises(ADCPVersionConversionError) as caught:
        convert_payload(
            "sync_creatives",
            request,
            direction="request",
            source_version=SOURCE,
            target_version="3.0",
        )
    assert caught.value.pointer == "/creatives/0/assets/playback_tracker"
