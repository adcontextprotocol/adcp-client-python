"""The exact public mutation variant supports adopter subclasses and all facades."""

from pydantic import Field

from adcp import TargetingOverlayInput as RootInput
from adcp.types import PackageRequest, PackageUpdate, TargetingOverlayInput
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
