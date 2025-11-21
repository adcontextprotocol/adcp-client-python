"""
Strongly-typed aliases and helper types for AdCP requests.

This module provides properly-typed Filter classes for different request types,
addressing the issue where schema generation creates generic dict[str, Any] filters.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

# ============================================================================
# FILTER TYPES - Strongly typed filters for different request types
# ============================================================================


class ProductFilters(BaseModel):
    """
    Filters for GetProductsRequest.

    Used when discovering available advertising products to narrow down
    the results based on delivery type, pricing model, format requirements, etc.
    """

    delivery_type: Literal["guaranteed", "non_guaranteed"] | None = Field(
        None, description="Filter by delivery type"
    )
    is_fixed_price: bool | None = Field(
        None, description="Filter for fixed price vs auction products"
    )
    format_types: list[Literal["video", "display", "audio"]] | None = Field(
        None, description="Filter by format types"
    )
    format_ids: list[str] | None = Field(
        None, description="Filter by specific format IDs"
    )
    standard_formats_only: bool | None = Field(
        None, description="Only return products accepting IAB standard formats"
    )
    min_exposures: int | None = Field(
        None, description="Minimum exposures/impressions needed for measurement validity", ge=1
    )


class CreativeFilters(BaseModel):
    """
    Filters for ListCreativesRequest.

    Used when querying creative assets from the centralized library to filter
    by format, status, tags, assignment status, and date ranges.
    """

    format: str | None = Field(
        None, description="Filter by creative format type (e.g., video, audio, display)"
    )
    formats: list[str] | None = Field(
        None, description="Filter by multiple creative format types"
    )
    status: (
        Literal["processing", "pending_review", "approved", "rejected"] | None
    ) = Field(None, description="Filter by creative approval status")
    statuses: (
        list[Literal["processing", "pending_review", "approved", "rejected"]] | None
    ) = Field(None, description="Filter by multiple creative statuses")
    tags: list[str] | None = Field(
        None, description="Filter by creative tags (all tags must match)"
    )
    tags_any: list[str] | None = Field(
        None, description="Filter by creative tags (any tag must match)"
    )
    name_contains: str | None = Field(
        None, description="Filter by creative names containing this text (case-insensitive)"
    )
    creative_ids: list[str] | None = Field(
        None, description="Filter by specific creative IDs", max_length=100
    )
    created_after: str | None = Field(
        None, description="Filter creatives created after this date (ISO 8601)"
    )
    created_before: str | None = Field(
        None, description="Filter creatives created before this date (ISO 8601)"
    )
    updated_after: str | None = Field(
        None, description="Filter creatives last updated after this date (ISO 8601)"
    )
    updated_before: str | None = Field(
        None, description="Filter creatives last updated before this date (ISO 8601)"
    )
    assigned_to_package: str | None = Field(
        None, description="Filter creatives assigned to this specific package"
    )
    assigned_to_packages: list[str] | None = Field(
        None, description="Filter creatives assigned to any of these packages"
    )
    unassigned: bool | None = Field(
        None,
        description="Filter for unassigned creatives when true, assigned creatives when false",
    )
    has_performance_data: bool | None = Field(
        None, description="Filter creatives that have performance data when true"
    )


class SignalFilters(BaseModel):
    """
    Filters for GetSignalsRequest.

    Used when discovering audience signals to refine the results based on
    catalog type, data providers, pricing, and coverage requirements.
    """

    catalog_types: list[Literal["marketplace", "custom", "owned"]] | None = Field(
        None, description="Filter by catalog type"
    )
    data_providers: list[str] | None = Field(
        None, description="Filter by specific data providers"
    )
    max_cpm: float | None = Field(None, description="Maximum CPM price filter", ge=0)
    min_coverage_percentage: float | None = Field(
        None, description="Minimum coverage requirement", ge=0, le=100
    )


# ============================================================================
# BACKWARD COMPATIBILITY ALIASES
# ============================================================================

# Generic Filters type alias - points to ProductFilters for backward compatibility
# Use specific filter types (ProductFilters, CreativeFilters) for new code
Filters = ProductFilters
