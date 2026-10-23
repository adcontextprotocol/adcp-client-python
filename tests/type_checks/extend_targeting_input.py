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

# The static type now CARRIES the beta.14 arm: ``TargetingOverlayInput |
# TargetingOverlay | None``. This block previously asserted the narrow
# ``TargetingOverlayInput | None`` and explained that the wider union was "a
# runtime mutation no source annotation expresses, so no type checker sees it".
# That is no longer true, measured on the model rather than read off the source:
#
#   upstream 65626be29  static TargetingOverlayInput | None
#                       runtime TargetingOverlayInput | None
#   here                static TargetingOverlayInput | TargetingOverlay | None
#                       runtime TargetingOverlayInput
#                               | Annotated[TargetingOverlay, SkipJsonSchema()]
#                               | None
#
# So the two views agree now where they used to diverge, and asserting the
# narrow union would assert the old gap back into existence. ``SkipJsonSchema``
# is a runtime-only marker a type checker does not see, which is the one
# remaining and expected difference between the two columns.
assert_type(created.targeting_overlay, TargetingOverlayInput | TargetingOverlay | None)
assert_type(updated.targeting_overlay, TargetingOverlayInput | TargetingOverlay | None)
assert_type(overlay.workflow_id, str)
assert created.targeting_overlay is overlay
assert updated.targeting_overlay is overlay


class LegacyTargeting(TargetingOverlay):
    workflow_id: str = Field(exclude=True)


legacy = LegacyTargeting(workflow_id="beta14-1181")
legacy_update = PackageUpdate.model_validate(
    {"package_id": "package-1", "targeting_overlay": legacy}
)
assert_type(legacy_update.targeting_overlay, TargetingOverlayInput | TargetingOverlay | None)
# The ``cast(object, ...)`` this line used to need is gone with the widening it
# worked around: the annotation now carries the beta.14 arm, so a LegacyTargeting
# identity check no longer reads as non-overlapping and can be asserted directly.
assert legacy_update.targeting_overlay is legacy
