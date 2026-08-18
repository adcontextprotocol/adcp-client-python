"""Static public-import contract for the collision-safe brand identity type."""

from adcp import BrandIdentity as RootBrandIdentity
from adcp.types import BrandIdentity
from adcp.types.buyer import BrandIdentity as BuyerBrandIdentity

root: RootBrandIdentity = RootBrandIdentity.model_construct()
typed: BrandIdentity = root
buyer: BuyerBrandIdentity = typed

assert isinstance(buyer, BrandIdentity)
