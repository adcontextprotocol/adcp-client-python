"""The exact public mutation variant supports adopter subclasses and all facades."""

from pydantic import Field
from typing_extensions import assert_type

from adcp import TargetingOverlayInput as RootInput
from adcp.types import PackageRequest, PackageUpdate, TargetingOverlay, TargetingOverlayInput
from adcp.types.buyer import TargetingOverlayInput as BuyerInput
from adcp.types.media_buy import TargetingOverlayInput as MediaBuyInput

root_type: type[TargetingOverlayInput] = RootInput
buyer_type: type[TargetingOverlayInput] = BuyerInput
media_buy_type: type[TargetingOverlayInput] = MediaBuyInput


class InternalTargetingInput(TargetingOverlayInput):
    workflow_id: str = Field(exclude=True)


overlay = InternalTargetingInput(geo_countries=None, workflow_id="wf-1181")
created = PackageRequest.model_validate(
    {"product_id": "product-1", "pricing_option_id": "price-1", "targeting_overlay": overlay}
)
updated = PackageUpdate.model_validate({"package_id": "package-1", "targeting_overlay": overlay})

# The canonical facade stubs expose the actual runtime readback union. Keep the
# extended object reference to access adopter-only attributes without a cast.
assert_type(created.targeting_overlay, TargetingOverlayInput | TargetingOverlay | None)
assert_type(updated.targeting_overlay, TargetingOverlayInput | TargetingOverlay | None)
assert created.targeting_overlay is overlay
assert updated.targeting_overlay is overlay
assert_type(overlay.workflow_id, str)


class LegacyTargeting(TargetingOverlay):
    workflow_id: str = Field(exclude=True)


legacy = LegacyTargeting(workflow_id="beta14-1181")
legacy_update = PackageUpdate.model_validate(
    {"package_id": "package-1", "targeting_overlay": legacy}
)
assert_type(legacy_update.targeting_overlay, TargetingOverlayInput | TargetingOverlay | None)
assert legacy_update.targeting_overlay is legacy
