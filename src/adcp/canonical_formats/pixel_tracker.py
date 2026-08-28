"""Bidirectional ``pixel_tracker`` ↔ v1 ``url`` asset projection.

Implements the normative downgrade/upgrade contract from
``schemas/cache/<version>/core/assets/pixel-tracker-asset.json``. The
downgrade table collapses the 7 v2 ``event`` values + 2 ``method``
values into the v1 ``{asset_type: url, url_type: tracker_pixel}`` shape
keyed on a small set of conventional ``asset_id`` slots. The upgrade
table infers event/method from the v1 ``asset_id`` convention.

Both directions are lossy-with-advisory:

* **Downgrade (v2 → v1)** emits ``PIXEL_TRACKER_LOSSY_DOWNGRADE`` on
  the response ``errors[]`` when the source pixel carries a viewability
  variant, the ``custom`` event, or ``method: js`` — those don't fit
  the single v1 slot they collapse onto. ``impression`` + ``click`` on
  ``method: img`` are the only no-loss combinations.

* **Upgrade (v1 → v2)** ALWAYS emits ``PIXEL_TRACKER_UPGRADE_INFERRED``
  because the v1 wire shape carries no explicit event/method — the
  inference is on the SDK, and consumers MUST see the advisory so they
  know the upgraded shape is a convention call rather than a wire fact.

Downgrade table (v2 → v1):

* ``event=impression, method=img`` → ``impression_tracker`` slot, no loss
* ``event=viewable_*`` or ``event=audible_video_complete``
  (any method=img) → ``viewability_tracker`` slot, event-variant LOST
* ``event=click, method=img`` → ``click_tracker`` slot, no loss
* ``event=custom, custom_event_name=X`` → ``impression_tracker`` slot,
  custom-event timing LOST
* ``method=js`` (any event) → same slot as method=img, JS execution LOST
* All lossy combinations emit ``PIXEL_TRACKER_LOSSY_DOWNGRADE``

Upgrade table (v1 → v2, all emit ``PIXEL_TRACKER_UPGRADE_INFERRED``):

* ``impression_tracker`` → ``event=impression, method=img``
* ``viewability_tracker`` → ``event=viewable_mrc_50, method=img``
  (50% is the dominant default in v1 catalogs)
* ``click_tracker`` → ``event=click, method=img``
* anything else → ``event=custom, custom_event_name=<original asset_id>,
  method=img`` (fallback preserves the original slot id)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

from adcp.canonical_formats.advisory import _echo_identifier, make_sdk_advisory
from adcp.types import Error, PixelTrackerAsset, PixelTrackerEvent, PixelTrackerMethod

# v1 conventional asset_id slots — these are the names a v1 catalog uses
# for renderer-fired tracker URLs. The downgrade table maps v2 events to
# one of these three (or a custom-event fallback that lands on the
# impression slot per the spec table).
_V1_ASSET_ID_IMPRESSION = "impression_tracker"
_V1_ASSET_ID_VIEWABILITY = "viewability_tracker"
_V1_ASSET_ID_CLICK = "click_tracker"

# Schemes the SDK will accept when upgrading a v1 url-tracker to a v2
# ``PixelTrackerAsset``. Buyer-side adopters consume the upgraded
# manifest in renderer pipelines; ``javascript:``, ``file:``, and
# ``data:`` URLs in tracker slots are operator-poisoning vectors that
# the SDK MUST refuse on the inbound boundary. The advisory carries
# the rejected scheme so consumers can audit the source seller.
_PIXEL_TRACKER_URL_ALLOWED_SCHEMES: frozenset[str] = frozenset({"http", "https"})


# Events that collapse onto the viewability slot on the v1 side. Together
# with the JS-method check and the custom-event check, this set fully
# determines which downgrades emit the LOSSY advisory.
_VIEWABILITY_EVENTS: frozenset[PixelTrackerEvent] = frozenset(
    {
        PixelTrackerEvent.viewable_mrc_50,
        PixelTrackerEvent.viewable_mrc_100,
        PixelTrackerEvent.viewable_video_50,
        PixelTrackerEvent.audible_video_complete,
    }
)


@dataclass
class V1UrlTracker:
    """v1 wire-shape projection of a single ``pixel_tracker``.

    Carries the projected ``asset_id`` + ``url`` plus a flag for whether
    the source pixel was a JS include (``method=js``). Adopters
    assembling a v1 ``assets[]`` array consume this directly:

    .. code-block:: python

        v1 = downgrade_pixel_tracker(pt).v1
        v1_asset = {
            "asset_type": "url",
            "url_type": "tracker_pixel",
            "asset_id": v1.asset_id,
            "url": v1.url,
        }

    The ``js_method`` flag is exposed so adopters with v1 catalogs that
    track a separate JS-tracker slot can still distinguish — the
    spec collapses both onto the same ``url_type`` on the wire, but
    nothing prevents an adopter from tracking the source method.
    """

    asset_id: str
    url: str
    js_method: bool = False


@dataclass
class PixelTrackerDowngrade:
    """Result of downgrading one ``PixelTrackerAsset`` to v1 wire shape."""

    v1: V1UrlTracker
    advisory: Error | None = None


@dataclass
class PixelTrackerUpgrade:
    """Result of upgrading one v1 url-tracker asset to v2 ``PixelTrackerAsset``.

    The upgrade ALWAYS carries an advisory per the spec — event/method
    are inferred, not declared.

    ``pixel_tracker`` is ``None`` when the upgrade was rejected (e.g.,
    the v1 URL used a disallowed scheme like ``javascript:`` or
    ``file:``). The advisory carries the rejection reason and the
    rejected scheme; consumers MUST treat the v1 entry as opaque and
    drop it from the upgraded manifest.
    """

    pixel_tracker: PixelTrackerAsset | None
    advisory: Error


# ---------------------------------------------------------------------------
# Downgrade — v2 PixelTrackerAsset → v1 url tracker
# ---------------------------------------------------------------------------


def _coerce_event(value: Any) -> PixelTrackerEvent | None:
    """Tolerate string or enum input for ``event``; ``None`` defaults to impression."""
    if value is None:
        return None
    if isinstance(value, PixelTrackerEvent):
        return value
    try:
        return PixelTrackerEvent(value)
    except (ValueError, TypeError):
        return None


def _coerce_method(value: Any) -> PixelTrackerMethod:
    if isinstance(value, PixelTrackerMethod):
        return value
    try:
        return PixelTrackerMethod(value)
    except (ValueError, TypeError):
        return PixelTrackerMethod.img


def _downgrade_slot(event: PixelTrackerEvent | None) -> str:
    """Map a v2 event to its v1 ``asset_id`` slot."""
    if event is PixelTrackerEvent.click:
        return _V1_ASSET_ID_CLICK
    if event in _VIEWABILITY_EVENTS:
        return _V1_ASSET_ID_VIEWABILITY
    # impression (None defaults here), custom, and anything else fall to the
    # impression slot per the spec table's `custom` row.
    return _V1_ASSET_ID_IMPRESSION


def downgrade_pixel_tracker(
    pixel: PixelTrackerAsset,
    *,
    field_path: str | None = None,
) -> PixelTrackerDowngrade:
    """Project a single :class:`PixelTrackerAsset` onto v1 wire shape.

    Lossy when the source pixel carries a viewability variant, the
    ``custom`` event, or ``method=js`` — those don't fit the single v1
    slot they collapse onto. The advisory carries the source
    ``event``, ``method``, and (when present) ``custom_event_name``
    under ``details`` so downstream consumers can reason about what
    was lost.

    Args:
        pixel: The v2 ``PixelTrackerAsset`` to downgrade.
        field_path: Optional JSONPath-lite pointer for the emitted
            advisory's ``field`` (e.g.,
            ``"creative_manifest.assets[2]"``).
    """
    event = _coerce_event(pixel.event)
    method = _coerce_method(pixel.method)
    url = str(pixel.url)
    js = method is PixelTrackerMethod.js
    custom_name = pixel.custom_event_name if hasattr(pixel, "custom_event_name") else None

    v1 = V1UrlTracker(asset_id=_downgrade_slot(event), url=url, js_method=js)

    # Determine whether this downgrade is lossy per the spec table.
    is_lossy_event = event in _VIEWABILITY_EVENTS or event is PixelTrackerEvent.custom
    is_lossy = is_lossy_event or js

    if not is_lossy:
        return PixelTrackerDowngrade(v1=v1, advisory=None)

    details: dict[str, Any] = {
        "source_event": event.value if event is not None else None,
        "source_method": method.value,
        "v1_asset_id": v1.asset_id,
    }
    if custom_name is not None:
        # ``custom_event_name`` is buyer-controlled and unbounded at the
        # Pydantic level — cap + scrub before echoing into multi-hop
        # ``errors[]`` per the half-1 ``_echo_identifier`` pattern.
        details["source_custom_event_name"] = _echo_identifier(custom_name)

    lost_axes: list[str] = []
    if is_lossy_event:
        lost_axes.append("event")
    if js:
        lost_axes.append("method_js_execution")
    details["lost"] = lost_axes

    advisory = make_sdk_advisory(
        code="PIXEL_TRACKER_LOSSY_DOWNGRADE",
        message=(
            f"Pixel tracker (event={event.value if event else 'impression'!r}, "
            f"method={method.value!r}) downgrades to v1 url-tracker slot "
            f"{v1.asset_id!r} with loss on {', '.join(lost_axes)!r}."
        ),
        field=field_path,
        details=details,
        suggestion=(
            "v1-only buyers will see the URL fire but cannot distinguish "
            "the original event variant or execute the JS body. Keep the "
            "v2 manifest in flight for 3.1+ buyers."
        ),
    )
    return PixelTrackerDowngrade(v1=v1, advisory=advisory)


# ---------------------------------------------------------------------------
# Upgrade — v1 url tracker → v2 PixelTrackerAsset
# ---------------------------------------------------------------------------


# Event/method mapping by v1 ``asset_id`` convention, per the spec
# table. Anything not in this map upgrades to a custom event whose
# name preserves the original v1 ``asset_id``.
_UPGRADE_TABLE: dict[str, tuple[PixelTrackerEvent, PixelTrackerMethod]] = {
    _V1_ASSET_ID_IMPRESSION: (PixelTrackerEvent.impression, PixelTrackerMethod.img),
    _V1_ASSET_ID_VIEWABILITY: (PixelTrackerEvent.viewable_mrc_50, PixelTrackerMethod.img),
    _V1_ASSET_ID_CLICK: (PixelTrackerEvent.click, PixelTrackerMethod.img),
}


def _url_scheme(url: str) -> str | None:
    """Extract the lowercase URL scheme; ``None`` on malformed input."""
    try:
        scheme = urlsplit(url).scheme
    except ValueError:
        return None
    return scheme.lower() if scheme else None


def upgrade_v1_tracker(
    *,
    asset_id: str,
    url: str,
    field_path: str | None = None,
) -> PixelTrackerUpgrade:
    """Project a v1 ``{asset_type: url, url_type: tracker_pixel}`` to v2.

    ALWAYS emits ``PIXEL_TRACKER_UPGRADE_INFERRED`` for accepted entries
    — the v1 wire shape carries no explicit event/method, so the
    inferred values are an SDK convention, not a wire fact. Consumers
    reading the advisory can decide whether to trust the convention or
    treat the pixel as opaque.

    Rejects URLs whose scheme is outside the SDK's allowlist
    (currently ``http``/``https``). Disallowed schemes get a
    ``pixel_tracker=None`` result + an advisory carrying the rejected
    scheme; callers MUST drop the source entry rather than substitute
    a value.

    Args:
        asset_id: v1 ``asset_id`` of the tracker slot (e.g.,
            ``"impression_tracker"``). Drives the inference.
        url: The tracker URL.
        field_path: Optional JSONPath-lite pointer for the emitted
            advisory's ``field``.
    """
    # --- URL scheme gate ---
    scheme = _url_scheme(url)
    if scheme not in _PIXEL_TRACKER_URL_ALLOWED_SCHEMES:
        return PixelTrackerUpgrade(
            pixel_tracker=None,
            advisory=make_sdk_advisory(
                code="PIXEL_TRACKER_UPGRADE_INFERRED",
                message=(
                    f"v1 url-tracker asset_id={_echo_identifier(asset_id)!r} "
                    f"REJECTED: scheme {scheme!r} not in allowed set "
                    f"{sorted(_PIXEL_TRACKER_URL_ALLOWED_SCHEMES)!r}."
                ),
                field=field_path,
                details={
                    "source_asset_id": _echo_identifier(asset_id),
                    "rejected_scheme": scheme,
                    "inference_basis": "rejected_disallowed_scheme",
                },
                suggestion=(
                    "Renderer-fired tracker URLs MUST use ``http`` or "
                    "``https``. Source v1 catalogs carrying javascript:, "
                    "file:, or data: schemes in tracker slots are operator-"
                    "poisoning vectors; drop them or replace with an "
                    "https tracker endpoint."
                ),
            ),
        )

    inferred = _UPGRADE_TABLE.get(asset_id)
    if inferred is None:
        # Fallback: preserve the original asset_id as the custom event
        # name so a downstream consumer who knows the seller's
        # convention can still bucket events correctly.
        event, method = PixelTrackerEvent.custom, PixelTrackerMethod.img
        custom_name: str | None = asset_id
        basis = "fallback_custom_event"
    else:
        event, method = inferred
        custom_name = None
        basis = "asset_id_convention"

    if custom_name is not None:
        pixel = PixelTrackerAsset.model_validate(
            {
                "asset_type": "pixel_tracker",
                "event": event,
                "method": method,
                "url": url,
                "custom_event_name": custom_name,
            }
        )
    else:
        pixel = PixelTrackerAsset.model_validate(
            {
                "asset_type": "pixel_tracker",
                "event": event,
                "method": method,
                "url": url,
            }
        )

    # ``asset_id`` and ``custom_event_name`` are seller-controlled and
    # unbounded at the v1 wire level — cap + scrub before echoing into
    # multi-hop ``errors[]`` per the half-1 ``_echo_identifier`` pattern.
    details: dict[str, Any] = {
        "source_asset_id": _echo_identifier(asset_id),
        "inferred_event": event.value,
        "inferred_method": method.value,
        "inference_basis": basis,
    }
    if custom_name is not None:
        details["inferred_custom_event_name"] = _echo_identifier(custom_name)

    advisory = make_sdk_advisory(
        code="PIXEL_TRACKER_UPGRADE_INFERRED",
        message=(
            f"v1 url-tracker asset_id={_echo_identifier(asset_id)!r} "
            f"upgraded to v2 pixel_tracker(event={event.value!r}, "
            f"method={method.value!r}) by {basis}."
        ),
        field=field_path,
        details=details,
        suggestion=(
            "Sellers SHOULD migrate v1 catalogs to v2 pixel_tracker so event "
            "/ method are declared on the wire rather than inferred from "
            "asset_id naming convention."
        ),
    )
    return PixelTrackerUpgrade(pixel_tracker=pixel, advisory=advisory)


@dataclass
class PixelTrackerBatchResult:
    """Aggregate downgrade or upgrade across a list of trackers."""

    items: list[Any] = field(default_factory=list)
    advisories: list[Error] = field(default_factory=list)


def downgrade_pixel_trackers(
    pixels: list[PixelTrackerAsset],
    *,
    field_path_prefix: str | None = None,
) -> PixelTrackerBatchResult:
    """Apply :func:`downgrade_pixel_tracker` across a list.

    Returns the projected v1 trackers + a deduplicated list of
    advisories. Advisories are deduplicated on
    ``(code, source_event, source_method, source_custom_event_name)``
    so a manifest with many viewability pixels surfaces ONE advisory
    per kind. Distinct custom events keep distinct advisories because
    losing their ``custom_event_name`` is exactly the information
    consumers need to act on.
    """
    out = PixelTrackerBatchResult()
    seen: set[tuple[str, str | None, str, str | None]] = set()
    for i, pt in enumerate(pixels):
        prefix = f"{field_path_prefix}[{i}]" if field_path_prefix else None
        result = downgrade_pixel_tracker(pt, field_path=prefix)
        out.items.append(result.v1)
        if result.advisory is not None:
            details = result.advisory.details or {}
            key = (
                result.advisory.code,
                details.get("source_event"),
                details.get("source_method", "img"),
                details.get("source_custom_event_name"),
            )
            if key not in seen:
                seen.add(key)
                out.advisories.append(result.advisory)
    return out


def upgrade_v1_trackers(
    v1_trackers: list[dict[str, Any]],
    *,
    field_path_prefix: str | None = None,
    quiet_inference: bool = False,
) -> PixelTrackerBatchResult:
    """Apply :func:`upgrade_v1_tracker` across a list of v1 url-tracker dicts.

    Each input MUST be a dict with ``asset_id`` + ``url`` keys (the v1
    wire shape). Advisories are deduplicated on ``(code, asset_id)``
    so many trackers under the same slot surface ONE advisory.

    The ``PIXEL_TRACKER_UPGRADE_INFERRED`` advisory fires on every
    accepted entry by default. Pass ``quiet_inference=True`` to
    suppress the advisory when the inference is unambiguous
    (``impression_tracker``, ``click_tracker``, ``viewability_tracker``
    via convention) — useful for high-volume buyer-side adopters
    reading v1 manifests at scale. The scheme-rejection advisory
    fires regardless of this flag (it carries a security signal).
    Entries the scheme gate rejects are NOT added to ``items`` —
    callers MUST drop them from the upgraded manifest.
    """
    out = PixelTrackerBatchResult()
    # Dedup includes ``inference_basis`` so a rejected upgrade and an
    # accepted upgrade on the same ``asset_id`` don't collapse — they
    # carry distinct semantics for the consumer.
    seen: set[tuple[str, str, str | None]] = set()
    for i, v1 in enumerate(v1_trackers):
        prefix = f"{field_path_prefix}[{i}]" if field_path_prefix else None
        asset_id = v1.get("asset_id")
        url = v1.get("url")
        if not isinstance(asset_id, str) or not isinstance(url, str):
            continue
        result = upgrade_v1_tracker(asset_id=asset_id, url=url, field_path=prefix)
        details = result.advisory.details or {}
        basis = details.get("inference_basis")
        is_rejection = basis == "rejected_disallowed_scheme"
        is_quietable = quiet_inference and not is_rejection and basis == "asset_id_convention"
        if result.pixel_tracker is not None:
            out.items.append(result.pixel_tracker)
        if is_quietable:
            continue
        key = (result.advisory.code, asset_id, basis)
        if key not in seen:
            seen.add(key)
            out.advisories.append(result.advisory)
    return out


__all__ = [
    "PixelTrackerBatchResult",
    "PixelTrackerDowngrade",
    "PixelTrackerUpgrade",
    "V1UrlTracker",
    "downgrade_pixel_tracker",
    "downgrade_pixel_trackers",
    "upgrade_v1_tracker",
    "upgrade_v1_trackers",
]
