"""Typed GAMRecipe Pydantic model — Phase 1B falsification target.

Constructed from salesagent's actual implementation_config shape as
specified by GAMProductConfigService (`src/services/gam_product_config_service.py`).
All fields the service generates, validates, or parses are represented;
no escape hatches intended.

Source-of-truth fields enumerated from:
- generate_default_config() — the auto-generated defaults
- validate_config() — the required/validated fields
- parse_form_config() — the full set of user-configurable fields

Q2 question: can this typed shape carry every salesagent-shaped
implementation_config without `extra: dict[str, Any]`,
`__pydantic_extra__`, or `# type: ignore`?
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

# ---------------------------------------------------------------------------
# Sub-models
# ---------------------------------------------------------------------------


class CreativePlaceholder(BaseModel):
    """A creative placeholder declares an expected creative size + count."""

    model_config = ConfigDict(extra="forbid")

    width: int = Field(ge=1)
    height: int = Field(ge=1)
    expected_creative_count: int = Field(ge=1, default=1)
    is_native: bool = False


class FrequencyCap(BaseModel):
    """A frequency cap limits impressions per buyer."""

    model_config = ConfigDict(extra="forbid")

    max_impressions: int = Field(ge=1)
    time_unit: Literal["MINUTE", "HOUR", "DAY", "WEEK", "MONTH", "LIFETIME"]
    time_range: int = Field(ge=1)


# ---------------------------------------------------------------------------
# Enums (string-Literal style — strict against GAM API documented values)
# ---------------------------------------------------------------------------

LineItemType = Literal[
    "STANDARD",
    "SPONSORSHIP",
    "NETWORK",
    "BULK",
    "PRICE_PRIORITY",
    "HOUSE",
]

CostType = Literal["CPM", "CPC", "CPD", "VCPM"]

CreativeRotationType = Literal["EVEN", "OPTIMIZED", "MANUAL", "SEQUENTIAL"]

DeliveryRateType = Literal[
    "EVENLY",
    "FRONTLOADED",
    "AS_FAST_AS_POSSIBLE",
]

PrimaryGoalType = Literal["NONE", "DAILY", "LIFETIME"]

PrimaryGoalUnitType = Literal["IMPRESSIONS", "CLICKS", "VIEWABLE_IMPRESSIONS"]

EnvironmentType = Literal["BROWSER", "VIDEO_PLAYER"]

CompanionDeliveryOption = Literal["OPTIONAL", "AT_LEAST_ONE", "ALL"]

NonGuaranteedAutomation = Literal[
    "manual",
    "confirmation_required",
    "automatic",
]

DiscountType = Literal["PERCENTAGE", "ABSOLUTE_VALUE"]


# ---------------------------------------------------------------------------
# The recipe
# ---------------------------------------------------------------------------


class GAMRecipe(BaseModel):
    """Typed GAM implementation_config — the recipe a GAMPlatform consumes.

    Maps 1:1 against salesagent's `implementation_config` shape from
    `gam_product_config_service.py`. Every field salesagent's service
    generates, validates, or parses has a typed slot here.

    Test invariant: every implementation_config value salesagent
    constructs MUST round-trip through GAMRecipe.model_validate(...) and
    .model_dump() without loss.
    """

    model_config = ConfigDict(extra="forbid")

    # --- Core line-item settings -------------------------------------------
    line_item_type: LineItemType
    priority: int = Field(ge=1, le=16)
    cost_type: CostType = "CPM"

    # --- Delivery settings -------------------------------------------------
    creative_rotation_type: CreativeRotationType = "EVEN"
    delivery_rate_type: DeliveryRateType = "EVENLY"
    primary_goal_type: PrimaryGoalType = "DAILY"
    primary_goal_unit_type: PrimaryGoalUnitType = "IMPRESSIONS"

    # --- Automation policy -------------------------------------------------
    non_guaranteed_automation: NonGuaranteedAutomation = "confirmation_required"

    # --- Inventory targeting (optional) ------------------------------------
    targeted_ad_unit_ids: list[str] | None = None
    targeted_placement_ids: list[str] | None = None
    include_descendants: bool = True

    # --- Creative placeholders (REQUIRED) ----------------------------------
    creative_placeholders: list[CreativePlaceholder] = Field(min_length=1)

    # --- Frequency caps (optional) -----------------------------------------
    frequency_caps: list[FrequencyCap] | None = None

    # --- Competition / exclusions ------------------------------------------
    competitive_exclusion_labels: list[str] | None = None

    # --- Custom targeting (the borderline case) ----------------------------
    # GAM's API accepts custom targeting as key→value(s).
    # Typed strictly per GAM API docs: keys are strings, values are
    # strings or lists of strings. Anything more nested rejects.
    # This is "strict typing matching GAM API," NOT an escape hatch.
    custom_targeting_keys: dict[str, str | list[str]] | None = None

    # --- Environment + advanced --------------------------------------------
    environment_type: EnvironmentType = "BROWSER"
    discount_type: DiscountType | None = None
    discount_value: float | None = None
    allow_overbook: bool = False
    skip_inventory_check: bool = False
    disable_viewability_avg_revenue_optimization: bool = False

    # --- Video-specific (only when environment_type == "VIDEO_PLAYER") -----
    companion_delivery_option: CompanionDeliveryOption | None = None
    video_max_duration: int | None = Field(default=None, ge=0)  # milliseconds
    skip_offset: int | None = Field(default=None, ge=0)  # milliseconds

    # --- Native -------------------------------------------------------------
    native_style_id: str | None = None

    # --- Teams --------------------------------------------------------------
    applied_team_ids: list[str] | None = None
