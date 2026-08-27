"""Static contract for the generated version-scoped model stubs."""

from typing import Literal

from typing_extensions import assert_type

from adcp.types.v31 import BuildCreativeRequest as BuildCreativeRequest31
from adcp.types.v31 import CreateMediaBuyResponse as CreateMediaBuyResponse31
from adcp.types.v31 import GetBrandIdentityResponse as GetBrandIdentityResponse31
from adcp.types.v31 import GetSignalsResponse as GetSignalsResponse31
from adcp.types.v31 import ListCreativesRequest as ListCreativesRequest31
from adcp.types.v31 import PackageRequest as PackageRequest31
from adcp.types.v32 import AcceptProposalRequest as AcceptProposalRequest32
from adcp.types.v32 import AcceptProposalResponse as AcceptProposalResponse32
from adcp.types.v32 import BuyProductsResponse as BuyProductsResponse32
from adcp.types.v32 import ListCreativesRequest as ListCreativesRequest32
from adcp.types.v32 import PackageRequest as PackageRequest32

list_request31 = ListCreativesRequest31(include_assignments=True)
assert_type(list_request31.include_assignments, bool)
ListCreativesRequest31(context={"trace_id": "trace-1"})
ListCreativesRequest31(account={"account_id": "account-1"})
ListCreativesRequest31(account={"brand": {"domain": "example.com"}, "operator": "agency.example"})
BuildCreativeRequest31(
    idempotency_key="build-1",
    signal_conditions=[
        {
            "value_type": "binary",
            "value": True,
            "signal_ref": {"scope": "product", "signal_id": "weather"},
            "signal_agent_segment_id": "segment-1",
        }
    ],
)

list_request32 = ListCreativesRequest32(assignment_projection="matching", assignment_limit=10)
assert_type(list_request32.assignment_projection, Literal["all", "matching"])
assert_type(list_request32.assignment_limit, int)

package31 = PackageRequest31(
    product_id="product-1",
    pricing_option_id="fixed",
    budget=100.0,
)
assert_type(package31.budget, float)

# Budget is optional in 3.2, and the constructor signature preserves that
# version delta for type checkers.
package32 = PackageRequest32(product_id="product-1", pricing_option_id="fixed")
assert_type(package32.budget, float | None)

success_response31 = CreateMediaBuyResponse31(
    status="completed",
    media_buy_id="buy-1",
    confirmed_at=None,
    revision=1,
    packages=[],
)
assert success_response31.media_buy_id is not None
assert_type(success_response31.media_buy_id, str)

error_response31 = CreateMediaBuyResponse31(
    status="failed",
    errors=[{"code": "INVALID_REQUEST", "message": "bad request"}],
)
assert error_response31.errors is not None
assert_type(error_response31.errors[0]["code"], str)

signals_response31 = GetSignalsResponse31(
    status="completed",
    signals=[],
)
assert signals_response31.signals is not None
assert_type(signals_response31.signals[0]["name"], str)
coverage_point31 = signals_response31.signals[0]["coverage_forecast"]["points"][0]
assert_type(coverage_point31["metrics"]["coverage_rate"]["mid"], float)
assert_type(
    coverage_point31["dimensions"][0]["kind"],
    Literal["geo", "placement", "device_type", "device_platform", "audience", "signal"],
)

brand_response31 = GetBrandIdentityResponse31(
    status="completed",
    brand_id="brand-1",
    house={"domain": "example.com", "name": "Example"},
    names=[],
    fonts={"primary": "Inter"},
)
assert brand_response31.fonts is not None
primary_font31 = brand_response31.fonts["primary"]
if isinstance(primary_font31, str):
    assert_type(primary_font31, str)
else:
    assert_type(primary_font31["family"], str)

accept_error32 = AcceptProposalResponse32(
    status="failed",
    errors=[{"code": "INVALID_REQUEST", "message": "bad request"}],
)
assert accept_error32.errors is not None
assert_type(accept_error32.errors[0]["message"], str)

buy_error32 = BuyProductsResponse32(
    status="failed",
    errors=[{"code": "INVALID_REQUEST", "message": "bad request"}],
)
assert buy_error32.errors is not None
assert_type(buy_error32.errors[0]["message"], str)

accept_request32 = AcceptProposalRequest32(
    adcp_version="3.2-beta.4",
    idempotency_key="accept-1",
    account={},
    proposal_id="proposal-1",
    proposal_terms_digest="sha256:terms",
    reporting_webhook={
        "url": "https://buyer.example/reporting",
        "operation_id": "reporting.accept-1",
        "authentication": {
            "schemes": ["Bearer"],
            "credentials": "buyer-reporting-token-1234567890",
        },
        "reporting_frequency": "daily",
    },
)
assert accept_request32.adcp_version is not None
assert_type(accept_request32.adcp_version, str)
