"""Bidirectional ``pixel_tracker`` ↔ v1 url-tracker projection."""

from __future__ import annotations

import pytest

from adcp.canonical_formats import (
    downgrade_pixel_tracker,
    downgrade_pixel_trackers,
    upgrade_v1_tracker,
    upgrade_v1_trackers,
)
from adcp.canonical_formats.advisory import SDK_ID
from adcp.types import PixelTrackerAsset, PixelTrackerEvent, PixelTrackerMethod


def _pt(event: str, method: str = "img", **extra) -> PixelTrackerAsset:
    return PixelTrackerAsset(
        asset_type="pixel_tracker",
        event=event,
        method=method,
        url="https://x.example/p",
        **extra,
    )


# ---------------------------------------------------------------------------
# Downgrade — no advisory cases
# ---------------------------------------------------------------------------


def test_impression_img_downgrades_with_no_advisory() -> None:
    result = downgrade_pixel_tracker(_pt("impression", "img"))
    assert result.v1.asset_id == "impression_tracker"
    assert result.v1.js_method is False
    assert result.advisory is None


def test_click_img_downgrades_with_no_advisory() -> None:
    result = downgrade_pixel_tracker(_pt("click", "img"))
    assert result.v1.asset_id == "click_tracker"
    assert result.advisory is None


# ---------------------------------------------------------------------------
# Downgrade — LOSSY advisory cases
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "event",
    ["viewable_mrc_50", "viewable_mrc_100", "viewable_video_50", "audible_video_complete"],
)
def test_viewability_variants_collapse_with_advisory(event: str) -> None:
    result = downgrade_pixel_tracker(_pt(event, "img"))
    assert result.v1.asset_id == "viewability_tracker"
    assert result.advisory is not None
    assert result.advisory.code == "PIXEL_TRACKER_LOSSY_DOWNGRADE"
    assert result.advisory.source.value == "sdk"
    assert result.advisory.sdk_id == SDK_ID
    assert result.advisory.details["source_event"] == event
    assert "event" in result.advisory.details["lost"]


def test_custom_event_collapses_to_impression_slot_with_advisory() -> None:
    pt = _pt("custom", "img", custom_event_name="my_thing")
    result = downgrade_pixel_tracker(pt)
    assert result.v1.asset_id == "impression_tracker"
    assert result.advisory is not None
    assert result.advisory.code == "PIXEL_TRACKER_LOSSY_DOWNGRADE"
    assert result.advisory.details["source_custom_event_name"] == "my_thing"


@pytest.mark.parametrize("event", ["impression", "click", "viewable_mrc_50", "custom"])
def test_js_method_always_lossy(event: str) -> None:
    extra = {"custom_event_name": "x"} if event == "custom" else {}
    result = downgrade_pixel_tracker(_pt(event, "js", **extra))
    assert result.advisory is not None
    assert result.advisory.code == "PIXEL_TRACKER_LOSSY_DOWNGRADE"
    assert result.v1.js_method is True
    assert "method_js_execution" in result.advisory.details["lost"]


def test_downgrade_batch_deduplicates_advisories() -> None:
    """3 viewability pixels of the same kind should yield 1 advisory, not 3."""
    pts = [_pt("viewable_mrc_50", "img") for _ in range(3)]
    result = downgrade_pixel_trackers(pts, field_path_prefix="manifest.assets")
    assert len(result.items) == 3
    assert len(result.advisories) == 1


def test_downgrade_batch_field_path_is_indexed() -> None:
    result = downgrade_pixel_trackers([_pt("viewable_mrc_50")], field_path_prefix="manifest.assets")
    assert result.advisories[0].field == "manifest.assets[0]"


# ---------------------------------------------------------------------------
# Upgrade — always emits PIXEL_TRACKER_UPGRADE_INFERRED
# ---------------------------------------------------------------------------


def test_impression_tracker_upgrades_to_impression_event() -> None:
    result = upgrade_v1_tracker(asset_id="impression_tracker", url="https://x/")
    assert result.pixel_tracker.event is PixelTrackerEvent.impression
    assert result.pixel_tracker.method is PixelTrackerMethod.img
    assert result.advisory.code == "PIXEL_TRACKER_UPGRADE_INFERRED"
    assert result.advisory.details["inference_basis"] == "asset_id_convention"


def test_viewability_tracker_upgrades_to_mrc_50_default() -> None:
    result = upgrade_v1_tracker(asset_id="viewability_tracker", url="https://x/")
    assert result.pixel_tracker.event is PixelTrackerEvent.viewable_mrc_50
    assert result.advisory.code == "PIXEL_TRACKER_UPGRADE_INFERRED"


def test_click_tracker_upgrades_to_click_event() -> None:
    result = upgrade_v1_tracker(asset_id="click_tracker", url="https://x/")
    assert result.pixel_tracker.event is PixelTrackerEvent.click


def test_unknown_asset_id_upgrades_to_custom_with_preserved_name() -> None:
    result = upgrade_v1_tracker(asset_id="vendor_xyz_tracker", url="https://x/")
    assert result.pixel_tracker.event is PixelTrackerEvent.custom
    assert result.pixel_tracker.custom_event_name == "vendor_xyz_tracker"
    assert result.advisory.details["inference_basis"] == "fallback_custom_event"


def test_upgrade_batch_deduplicates_advisories_per_asset_id() -> None:
    v1 = [
        {"asset_id": "impression_tracker", "url": "https://x/1"},
        {"asset_id": "impression_tracker", "url": "https://x/2"},
        {"asset_id": "click_tracker", "url": "https://x/3"},
    ]
    result = upgrade_v1_trackers(v1, field_path_prefix="manifest.assets")
    assert len(result.items) == 3
    # 2 distinct asset_ids = 2 advisories, not 3
    assert len(result.advisories) == 2


def test_upgrade_batch_skips_malformed_entries() -> None:
    """Entries missing ``asset_id`` or ``url`` are skipped (not raised)."""
    v1 = [
        {"asset_id": "impression_tracker", "url": "https://x/"},
        {"asset_id": "click_tracker"},  # missing url
        {"url": "https://x/2"},  # missing asset_id
        {"asset_id": "click_tracker", "url": "https://x/3"},
    ]
    result = upgrade_v1_trackers(v1)
    assert len(result.items) == 2  # only the two valid entries


# ---------------------------------------------------------------------------
# URL scheme allowlist on upgrade
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad_url",
    [
        "javascript:alert(1)",
        "file:///etc/passwd",
        "data:text/html,<script>x</script>",
        "ftp://attacker.example/pixel",
        "vbscript:foo",
        "",
    ],
)
def test_upgrade_rejects_disallowed_schemes(bad_url: str) -> None:
    result = upgrade_v1_tracker(asset_id="impression_tracker", url=bad_url)
    assert result.pixel_tracker is None
    assert result.advisory.code == "PIXEL_TRACKER_UPGRADE_INFERRED"
    assert result.advisory.details["inference_basis"] == "rejected_disallowed_scheme"
    # Allowed-schemes set is echoed for adopter clarity.
    assert "rejected_scheme" in result.advisory.details


@pytest.mark.parametrize("scheme", ["http", "https"])
def test_upgrade_accepts_http_and_https(scheme: str) -> None:
    result = upgrade_v1_tracker(asset_id="impression_tracker", url=f"{scheme}://x.example/p")
    assert result.pixel_tracker is not None
    assert result.advisory.details["inference_basis"] == "asset_id_convention"


def test_upgrade_batch_drops_rejected_entries_from_items() -> None:
    """Rejected v1 trackers must NOT appear in ``items`` — they're security-gated.

    Advisories for rejections always surface even in ``quiet_inference`` mode.
    """
    v1 = [
        {"asset_id": "impression_tracker", "url": "https://x/1"},
        {"asset_id": "impression_tracker", "url": "javascript:evil"},
        {"asset_id": "click_tracker", "url": "https://x/3"},
    ]
    result = upgrade_v1_trackers(v1)
    assert len(result.items) == 2
    # Exactly one rejection advisory (asset_id dedup applies).
    rejections = [
        a
        for a in result.advisories
        if (a.details or {}).get("inference_basis") == "rejected_disallowed_scheme"
    ]
    assert len(rejections) == 1


# ---------------------------------------------------------------------------
# Seller-controlled string scrubbing in advisory details
# ---------------------------------------------------------------------------


def test_upgrade_scrubs_control_chars_in_asset_id() -> None:
    """asset_id with embedded newlines must not echo verbatim to advisory."""
    result = upgrade_v1_tracker(
        asset_id="vendor\nFAKE LOG LINE\nimpression_tracker",
        url="https://x.example/p",
    )
    echoed = result.advisory.details["source_asset_id"]
    assert "\n" not in echoed
    assert "\\u000a" in echoed


def test_downgrade_scrubs_control_chars_in_custom_event_name() -> None:
    """custom_event_name with embedded ANSI escapes must not echo verbatim."""
    pt = PixelTrackerAsset(
        asset_type="pixel_tracker",
        event="custom",
        method="img",
        url="https://x/",
        custom_event_name="brand\x1b[31mlift\x1b[0m",
    )
    result = downgrade_pixel_tracker(pt)
    echoed = result.advisory.details["source_custom_event_name"]
    assert "\x1b" not in echoed
    assert "\\u001b" in echoed


def test_downgrade_dedup_preserves_distinct_custom_event_names() -> None:
    """Two custom-event downgrades with different custom_event_name MUST NOT
    collapse into a single advisory — the dedup key includes the name."""
    p1 = PixelTrackerAsset(
        asset_type="pixel_tracker",
        event="custom",
        method="img",
        url="https://x/1",
        custom_event_name="brand_lift",
    )
    p2 = PixelTrackerAsset(
        asset_type="pixel_tracker",
        event="custom",
        method="img",
        url="https://x/2",
        custom_event_name="viewability_3p",
    )
    result = downgrade_pixel_trackers([p1, p2])
    custom_advisories = [
        a
        for a in result.advisories
        if (a.details or {}).get("source_custom_event_name") is not None
    ]
    assert len(custom_advisories) == 2


# ---------------------------------------------------------------------------
# quiet_inference dedup mode
# ---------------------------------------------------------------------------


def test_upgrade_quiet_inference_suppresses_convention_advisories() -> None:
    v1 = [
        {"asset_id": "impression_tracker", "url": "https://x/1"},
        {"asset_id": "click_tracker", "url": "https://x/2"},
        {"asset_id": "viewability_tracker", "url": "https://x/3"},
    ]
    result = upgrade_v1_trackers(v1, quiet_inference=True)
    assert len(result.items) == 3
    assert result.advisories == []


def test_upgrade_quiet_inference_still_surfaces_fallback_custom() -> None:
    """Fallback custom-event inference is NOT convention; quiet mode keeps it."""
    v1 = [
        {"asset_id": "impression_tracker", "url": "https://x/1"},
        {"asset_id": "vendor_xyz_tracker", "url": "https://x/2"},  # fallback
    ]
    result = upgrade_v1_trackers(v1, quiet_inference=True)
    assert len(result.advisories) == 1
    assert result.advisories[0].details["inference_basis"] == "fallback_custom_event"


def test_upgrade_quiet_inference_still_surfaces_rejections() -> None:
    """Security signal: scheme rejections fire even in quiet mode."""
    v1 = [
        {"asset_id": "impression_tracker", "url": "https://x/1"},
        {"asset_id": "click_tracker", "url": "javascript:evil"},
    ]
    result = upgrade_v1_trackers(v1, quiet_inference=True)
    rejections = [
        a
        for a in result.advisories
        if (a.details or {}).get("inference_basis") == "rejected_disallowed_scheme"
    ]
    assert len(rejections) == 1
