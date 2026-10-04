"""Adopter pattern: name the exact generated class a response field is typed with.

AdCP names an inline object after the property that holds it, so
``creative/list-creatives-response.json`` and ``core/tasks-list-response.json``
each produce a class called ``QuerySummary``, with different fields and
different required keys. ``adcp.types`` binds one of them to the bare name.

The module for each schema domain re-exports what that domain declares, so an
adopter reading ``ListCreativesResponse.query_summary`` annotates the variable
with the class that field actually holds. Under the bare import mypy accepts the
assignment of either class, and the one it hands over accepts a document the
real field rejects.

``adcp.types.error_details`` does the same for the structured error-details
family: each model, and the field types its annotations reference, so a seller
builds the payload with typed values instead of a dict.
"""

from __future__ import annotations

from typing_extensions import assert_type

from adcp.types.domains.core import DomainBreakdown as CoreDomainBreakdown
from adcp.types.domains.core import QuerySummary as CoreQuerySummary
from adcp.types.domains.creative import QuerySummary as CreativeQuerySummary
from adcp.types.error_details import (
    BillingNotSupportedDetails,
    ScopeFromBillingNotSupported,
    ScopeFromRateLimited,
    SupportedMajor,
    SupportedVersion,
    VersionUnsupportedDetails,
)

# --- The two QuerySummary classes are distinct types ---

listing = CreativeQuerySummary(total_matching=12, returned=10)
assert_type(listing.total_matching, int)
assert_type(listing.returned, int)

tasks = CoreQuerySummary()
assert_type(tasks.domain_breakdown, CoreDomainBreakdown | None)

# ``returned`` and ``total_matching`` are required on the listing variant and
# optional on the tasks variant, so the two are not interchangeable.
listings: list[CreativeQuerySummary] = [listing]

# --- Error details construct with typed values, not a dict ---

unsupported = VersionUnsupportedDetails(
    adcp_version="3.2",
    supported_versions=[SupportedVersion("3.1"), SupportedVersion("3.2")],
    supported_majors=[SupportedMajor(3)],
)
assert_type(unsupported.supported_versions, list[SupportedVersion])
assert_type(unsupported.supported_majors, list[SupportedMajor] | None)

# A nested name two error-details schemas both define carries its qualified
# name, so the two ``Scope`` enums cannot be confused for one another.
billing = BillingNotSupportedDetails(scope=ScopeFromBillingNotSupported.account)
assert_type(billing.scope, ScopeFromBillingNotSupported | None)

rate_limit_scopes: list[ScopeFromRateLimited] = [
    ScopeFromRateLimited.account
]
