"""Strict adopter-facing type checks for proposal-bound action helpers."""

from __future__ import annotations

from datetime import datetime, timezone

from typing_extensions import assert_type

from adcp import (
    ActionAvailabilityStatus,
    ActionIntent,
    ActionTask,
    BudgetChangeConstraints,
    CanonicalMediaBuyActionMode,
    CanonicalMediaBuyActionName,
    CanonicalProductAction,
    ChangeTermSelection,
    Duration,
    FlightChangeConstraints,
    MediaBuyActionAssessment,
    MediaBuyActionMode,
    MediaBuyActionProjection,
    MediaBuyChangeTerm,
    MediaBuyValidAction,
    PackageCountChangeConstraints,
    ProductAllowedAction,
    assess_media_buy_action,
    materialize_change_terms,
    project_available_actions,
    route_media_buy_action,
)
from adcp.types import DurationUnit
from adcp.types.media_buy import EffectiveTimingChangeConstraints

product_action = CanonicalProductAction(
    action=CanonicalMediaBuyActionName.pause,
    modes=[CanonicalMediaBuyActionMode.self_serve],
)
legacy_product_action = ProductAllowedAction(
    action=MediaBuyValidAction.pause,
    modes=[MediaBuyActionMode.self_serve],
)
terms = materialize_change_terms(
    [product_action],
    [ChangeTermSelection(action="pause", term_id="right_pause")],
)
assert_type(terms, tuple[MediaBuyChangeTerm, ...])

term = MediaBuyChangeTerm(
    term_id="right_pause",
    action=CanonicalMediaBuyActionName.pause,
    service_mode=CanonicalMediaBuyActionMode.self_serve,
)
projection = project_available_actions([term], "active")
assert_type(projection, MediaBuyActionProjection)

assessment = assess_media_buy_action(
    "pause",
    proposal={"commercial_terms": {"change_terms": [term]}},
    media_buy={"status": "active", "available_actions": projection.to_wire()},
    intent=ActionIntent(effective_at=datetime(2026, 8, 29, tzinfo=timezone.utc)),
)
assert_type(assessment, MediaBuyActionAssessment)
assert_type(assessment.status, ActionAvailabilityStatus)
assert_type(route_media_buy_action("pause"), ActionTask | None)

assert_type(
    BudgetChangeConstraints(kind="budget", max_delta_percent=20),
    BudgetChangeConstraints,
)
assert_type(
    FlightChangeConstraints(kind="flight", max_change=Duration(interval=1, unit=DurationUnit.days)),
    FlightChangeConstraints,
)
assert_type(
    PackageCountChangeConstraints(kind="package_count", max_additions=2),
    PackageCountChangeConstraints,
)
assert_type(
    EffectiveTimingChangeConstraints(
        kind="effective_timing",
        minimum_notice=Duration(interval=1, unit=DurationUnit.hours),
    ),
    EffectiveTimingChangeConstraints,
)

_ = legacy_product_action
