"""
Example: Using Strongly-Typed Filter Classes

This example demonstrates how to use the new strongly-typed filter classes
(ProductFilters, CreativeFilters) instead of generic dict[str, Any] filters.

The strongly-typed filters provide:
- IDE autocomplete and type hints
- Compile-time type checking
- Clear documentation of available filter options
- Prevention of typos in filter field names
"""

import asyncio

from adcp import ADCPClient, AgentConfig, CreativeFilters, GetProductsRequest, ProductFilters


async def example_product_filters():
    """Example: Using ProductFilters for product discovery."""
    print("=" * 60)
    print("Example 1: ProductFilters for GetProductsRequest")
    print("=" * 60)

    # Create strongly-typed product filters
    filters = ProductFilters(
        delivery_type="guaranteed",
        format_types=["video", "display"],
        min_exposures=10000,
        standard_formats_only=True,
    )

    # Use filters in request
    request = GetProductsRequest(
        brief="Looking for premium video inventory",
        filters=filters.model_dump(exclude_none=True),  # Convert to dict for API
    )

    print(f"\n✓ Created ProductFilters with fields:")
    print(f"  - delivery_type: {filters.delivery_type}")
    print(f"  - format_types: {filters.format_types}")
    print(f"  - min_exposures: {filters.min_exposures}")
    print(f"  - standard_formats_only: {filters.standard_formats_only}")
    print(f"\n✓ Request ready to send with properly typed filters")


async def example_creative_filters():
    """Example: Using CreativeFilters for creative library queries."""
    print("\n" + "=" * 60)
    print("Example 2: CreativeFilters for ListCreativesRequest")
    print("=" * 60)

    # Create strongly-typed creative filters
    filters = CreativeFilters(
        status="approved",
        formats=["video", "display"],
        tags=["holiday", "2024"],
        created_after="2024-01-01T00:00:00Z",
        has_performance_data=True,
    )

    print(f"\n✓ Created CreativeFilters with fields:")
    print(f"  - status: {filters.status}")
    print(f"  - formats: {filters.formats}")
    print(f"  - tags: {filters.tags}")
    print(f"  - created_after: {filters.created_after}")
    print(f"  - has_performance_data: {filters.has_performance_data}")
    print(f"\n✓ Filters ready to use in ListCreativesRequest")


async def example_backward_compatibility():
    """Example: Backward compatibility with generic Filters alias."""
    print("\n" + "=" * 60)
    print("Example 3: Backward Compatibility")
    print("=" * 60)

    # The generic 'Filters' alias still works for backward compatibility
    from adcp import Filters

    # This is actually ProductFilters under the hood
    filters = Filters(
        delivery_type="non_guaranteed",
        is_fixed_price=False,
    )

    print(f"\n✓ Generic 'Filters' alias works (maps to ProductFilters):")
    print(f"  - delivery_type: {filters.delivery_type}")
    print(f"  - is_fixed_price: {filters.is_fixed_price}")
    print(f"\n✓ Existing code using 'Filters' continues to work")


async def example_ide_benefits():
    """Example: IDE autocomplete and type checking benefits."""
    print("\n" + "=" * 60)
    print("Example 4: IDE Benefits")
    print("=" * 60)

    # With strongly-typed filters, IDEs can provide:
    # 1. Autocomplete for field names
    # 2. Type checking (e.g., can't pass string where bool expected)
    # 3. Documentation on hover

    filters = CreativeFilters(
        # IDE will autocomplete these field names ↓
        status="approved",  # IDE knows this should be a CreativeStatus literal
        unassigned=True,  # IDE knows this is a boolean
        creative_ids=["id1", "id2"],  # IDE knows this is list[str]
    )

    print("\n✓ IDE provides:")
    print("  - Autocomplete for all 17 CreativeFilters fields")
    print("  - Type checking (status must be valid literal)")
    print("  - Inline documentation for each field")
    print("  - Error highlighting for invalid field names or types")


async def example_migration_path():
    """Example: Migration path from dict to typed filters."""
    print("\n" + "=" * 60)
    print("Example 5: Migration Path")
    print("=" * 60)

    print("\n❌ Old approach (generic dict, no type safety):")
    print('   filters = {"status": "approved", "format": "video"}')
    print("   # Typos not caught, no autocomplete, no type checking")

    print("\n✅ New approach (strongly-typed):")
    print("   filters = CreativeFilters(status='approved', format='video')")
    print("   # IDE catches typos, provides autocomplete, validates types")

    print("\n✅ Both work, but typed filters are safer and easier:")
    # Old way - prone to typos
    old_filters = {"status": "approved", "format": "video"}

    # New way - type-safe
    new_filters = CreativeFilters(status="approved", format="video")

    print(f"\n✓ Old dict approach: {old_filters}")
    print(f"✓ New typed approach: {new_filters.model_dump(exclude_none=True)}")
    print("✓ Both produce same output, but typed is safer!")


async def main():
    """Run all examples."""
    print("\n" + "=" * 60)
    print("Strongly-Typed Filter Classes Examples")
    print("=" * 60)

    await example_product_filters()
    await example_creative_filters()
    await example_backward_compatibility()
    await example_ide_benefits()
    await example_migration_path()

    print("\n" + "=" * 60)
    print("Summary")
    print("=" * 60)
    print("\nNew strongly-typed filter classes available:")
    print("  • ProductFilters  - for GetProductsRequest")
    print("  • CreativeFilters - for ListCreativesRequest")
    print("  • SignalFilters   - for GetSignalsRequest")
    print("  • Filters         - generic alias (backward compatible)")
    print("\nBenefits:")
    print("  ✓ IDE autocomplete and documentation")
    print("  ✓ Compile-time type checking")
    print("  ✓ Prevents typos in field names")
    print("  ✓ Clear available filter options")
    print("  ✓ Backward compatible with existing code")
    print()


if __name__ == "__main__":
    asyncio.run(main())
