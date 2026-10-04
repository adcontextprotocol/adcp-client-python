"""A directly imported base retains inherited pinned protocol field types."""

from pydantic import Field
from typing_extensions import assert_type

from adcp.types.versioned_bases.v31 import ListCreativesRequestBase, PackageRequestBase
from adcp.types.versioned_bases.v32 import PackageRequestBase as PackageRequest32Base


class SellerListCreatives(ListCreativesRequestBase):
    tenant_id: str | None = Field(default=None, exclude=True)


class SellerPackage(PackageRequestBase):
    inventory_key: str | None = Field(default=None, exclude=True)


def read_request(req: SellerListCreatives, package: SellerPackage) -> bool:
    assert_type(req.include_assignments, bool)
    assert_type(package.budget, float)
    if req.pagination is not None and "max_results" in req.pagination:
        assert_type(req.pagination["max_results"], int)
    return req.include_assignments


def read_new_package(package: PackageRequest32Base) -> float | None:
    assert_type(package.budget, float | None)
    return package.budget


request = SellerListCreatives(include_assignments=True, tenant_id="tenant-1")
assert_type(request.include_assignments, bool)
package = SellerPackage(
    product_id="p1", pricing_option_id="fixed", budget=100.0, inventory_key="slot-1"
)
assert_type(package.budget, float)
