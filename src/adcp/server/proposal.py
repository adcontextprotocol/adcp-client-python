"""Proposal generation helpers.

Provides utilities for building ADCP Proposals in get_products responses.
Proposals represent recommended media plans with budget allocations across products.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import uuid4

from pydantic import BaseModel

from adcp.types import Error


class ProposalNotSupported(BaseModel):
    """Response indicating proposal generation is not supported.

    Use this when your agent supports get_products but not proposal generation.
    """

    proposals_supported: bool = False
    reason: str = "This agent does not generate proposals"
    error: Error | None = None


def proposals_not_supported(
    reason: str = "This agent does not generate proposals",
) -> ProposalNotSupported:
    """Create a response indicating proposals are not supported.

    Args:
        reason: Human-readable explanation

    Returns:
        ProposalNotSupported response
    """
    return ProposalNotSupported(
        proposals_supported=False,
        reason=reason,
        error=Error(
            code="PROPOSALS_NOT_SUPPORTED",
            message=reason,
        ),
    )


class AllocationBuilder:
    """Builder for product allocations within a proposal."""

    def __init__(
        self,
        product_id: str,
        allocation_percentage: float,
    ):
        """Create an allocation builder.

        Args:
            product_id: ID of the product (must match a product in the response)
            allocation_percentage: Percentage of budget (0-100)
        """
        self._data: dict[str, Any] = {
            "product_id": product_id,
            "allocation_percentage": allocation_percentage,
        }

    def with_pricing_option(self, pricing_option_id: str) -> AllocationBuilder:
        """Specify which pricing option to use.

        Args:
            pricing_option_id: ID from the product's pricing_options array
        """
        self._data["pricing_option_id"] = pricing_option_id
        return self

    def with_rationale(self, rationale: str) -> AllocationBuilder:
        """Add explanation for this allocation.

        Args:
            rationale: Why this product/allocation is recommended
        """
        self._data["rationale"] = rationale
        return self

    def with_sequence(self, sequence: int) -> AllocationBuilder:
        """Set ordering hint for multi-line-item plans.

        Args:
            sequence: 1-based ordering position
        """
        self._data["sequence"] = sequence
        return self

    def with_tags(self, tags: list[str]) -> AllocationBuilder:
        """Add categorical tags.

        Args:
            tags: Tags like 'desktop', 'mobile', 'german'
        """
        self._data["tags"] = tags
        return self

    def build(self) -> dict[str, Any]:
        """Build the allocation dict."""
        return self._data.copy()


class ProposalBuilder:
    """Builder for ADCP Proposals.

    Helps construct valid proposals for get_products responses. Proposals
    represent recommended media plans with budget allocations.

    Example:
        proposal = (
            ProposalBuilder("Q1 Brand Campaign")
            .with_description("Balanced awareness campaign")
            .add_allocation("product-1", 60)
                .with_rationale("High-impact display")
            .add_allocation("product-2", 40)
                .with_rationale("Contextual targeting")
            .with_budget_guidance(min=10000, recommended=25000, max=50000)
            .build()
        )
    """

    def __init__(self, name: str, proposal_id: str | None = None):
        """Create a new proposal builder.

        Args:
            name: Human-readable name for the proposal
            proposal_id: Unique ID (auto-generated if not provided)
        """
        self._name = name
        self._proposal_id = proposal_id or f"proposal-{uuid4().hex[:8]}"
        self._description: str | None = None
        self._brief_alignment: str | None = None
        self._expires_at: datetime | None = None
        self._allocations: list[dict[str, Any]] = []
        self._budget_guidance: dict[str, Any] | None = None
        self._current_allocation: AllocationBuilder | None = None
        self._ext: dict[str, Any] | None = None

    def with_description(self, description: str) -> ProposalBuilder:
        """Add description explaining the proposal strategy.

        Args:
            description: What the proposal achieves
        """
        self._finalize_allocation()
        self._description = description
        return self

    def with_brief_alignment(self, alignment: str) -> ProposalBuilder:
        """Explain how proposal aligns with campaign brief.

        Args:
            alignment: Alignment explanation
        """
        self._finalize_allocation()
        self._brief_alignment = alignment
        return self

    def expires_in(self, days: int = 7) -> ProposalBuilder:
        """Set expiration relative to now.

        Args:
            days: Number of days until expiration
        """
        self._finalize_allocation()
        self._expires_at = datetime.now(timezone.utc) + timedelta(days=days)
        return self

    def expires_at(self, expires: datetime) -> ProposalBuilder:
        """Set absolute expiration time.

        Args:
            expires: When the proposal expires
        """
        self._finalize_allocation()
        self._expires_at = expires
        return self

    def add_allocation(
        self,
        product_id: str,
        allocation_percentage: float,
    ) -> ProposalBuilder:
        """Add a product allocation.

        After calling this, chain allocation methods (with_rationale, etc.)
        before adding another allocation or calling build().

        Args:
            product_id: ID of the product
            allocation_percentage: Percentage of budget (0-100)

        Returns:
            Self for method chaining
        """
        self._finalize_allocation()
        self._current_allocation = AllocationBuilder(product_id, allocation_percentage)
        return self

    def with_pricing_option(self, pricing_option_id: str) -> ProposalBuilder:
        """Set pricing option for current allocation."""
        if self._current_allocation:
            self._current_allocation.with_pricing_option(pricing_option_id)
        return self

    def with_rationale(self, rationale: str) -> ProposalBuilder:
        """Add rationale for current allocation."""
        if self._current_allocation:
            self._current_allocation.with_rationale(rationale)
        return self

    def with_sequence(self, sequence: int) -> ProposalBuilder:
        """Set sequence for current allocation."""
        if self._current_allocation:
            self._current_allocation.with_sequence(sequence)
        return self

    def with_tags(self, tags: list[str]) -> ProposalBuilder:
        """Add tags for current allocation."""
        if self._current_allocation:
            self._current_allocation.with_tags(tags)
        return self

    def with_budget_guidance(
        self,
        *,
        min: float | None = None,
        recommended: float | None = None,
        max: float | None = None,
        currency: str = "USD",
    ) -> ProposalBuilder:
        """Add budget guidance for the proposal.

        Args:
            min: Minimum recommended budget
            recommended: Optimal budget
            max: Maximum before diminishing returns
            currency: ISO 4217 currency code
        """
        self._finalize_allocation()
        self._budget_guidance = {
            "currency": currency,
        }
        if min is not None:
            self._budget_guidance["min"] = min
        if recommended is not None:
            self._budget_guidance["recommended"] = recommended
        if max is not None:
            self._budget_guidance["max"] = max
        return self

    def with_extension(self, ext: dict[str, Any]) -> ProposalBuilder:
        """Add extension data.

        Args:
            ext: Extension object
        """
        self._finalize_allocation()
        self._ext = ext
        return self

    def _finalize_allocation(self) -> None:
        """Finalize current allocation and add to list."""
        if self._current_allocation:
            self._allocations.append(self._current_allocation.build())
            self._current_allocation = None

    def build(self) -> dict[str, Any]:
        """Build the proposal dict.

        Returns:
            Proposal as a dict ready for use in get_products response

        Raises:
            ValueError: If allocations don't sum to 100
        """
        self._finalize_allocation()

        if not self._allocations:
            raise ValueError("Proposal must have at least one allocation")

        total = sum(a["allocation_percentage"] for a in self._allocations)
        if abs(total - 100.0) > 0.01:
            raise ValueError(
                f"Allocation percentages must sum to 100, got {total}"
            )

        proposal: dict[str, Any] = {
            "proposal_id": self._proposal_id,
            "name": self._name,
            "allocations": self._allocations,
        }

        if self._description:
            proposal["description"] = self._description
        if self._brief_alignment:
            proposal["brief_alignment"] = self._brief_alignment
        if self._expires_at:
            proposal["expires_at"] = self._expires_at.isoformat()
        if self._budget_guidance:
            proposal["total_budget_guidance"] = self._budget_guidance
        if self._ext:
            proposal["ext"] = self._ext

        return proposal

    def validate(self) -> list[str]:
        """Validate the proposal without building.

        Returns:
            List of validation errors (empty if valid)
        """
        errors: list[str] = []

        if self._current_allocation:
            allocations = self._allocations + [self._current_allocation.build()]
        else:
            allocations = self._allocations

        if not allocations:
            errors.append("Proposal must have at least one allocation")
        else:
            total = sum(a["allocation_percentage"] for a in allocations)
            if abs(total - 100.0) > 0.01:
                errors.append(f"Allocation percentages must sum to 100, got {total}")

        return errors
