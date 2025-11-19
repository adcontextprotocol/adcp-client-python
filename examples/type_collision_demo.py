#!/usr/bin/env python3
"""
Demonstration of type collision handling in adcp-client-python.

This example shows:
1. Why Package type collisions exist
2. How the qualified naming strategy works
3. Type checker behavior with colliding types
4. Best practices for using these types
"""

from __future__ import annotations

# ============================================================================
# CORRECT: Import from public API
# ============================================================================

from adcp import Package  # This is _PackageFromPackage (full definition)
from adcp.types._generated import (
    _PackageFromCreateMediaBuyResponse,  # Internal: minimal Package in response
    _PackageFromPackage,  # Internal: full Package definition
)


def demonstrate_collision():
    """Show that both Package types exist and are distinct."""

    # These are the SAME type (both point to full Package definition)
    print("=== Type Identity ===")
    print(f"Package from public API: {Package.__module__}.{Package.__name__}")
    print(f"_PackageFromPackage: {_PackageFromPackage.__module__}.{_PackageFromPackage.__name__}")
    print(f"Same type? {Package is _PackageFromPackage}")
    print()

    # These are DIFFERENT types (defined in different modules)
    print("=== Type Collision ===")
    print(f"Full Package: {_PackageFromPackage.__module__}.{_PackageFromPackage.__name__}")
    print(
        f"Response Package: {_PackageFromCreateMediaBuyResponse.__module__}.{_PackageFromCreateMediaBuyResponse.__name__}"
    )
    print(f"Same type? {_PackageFromPackage is _PackageFromCreateMediaBuyResponse}")
    print()

    # Both have same __name__ but are distinct types
    print("=== Name vs. Identity ===")
    print(f"Full Package.__name__: {_PackageFromPackage.__name__}")
    print(f"Response Package.__name__: {_PackageFromCreateMediaBuyResponse.__name__}")
    print("Both are named 'Package', but they're distinct types!")
    print()


def demonstrate_field_differences():
    """Show the structural differences between Package types."""

    full_fields = set(_PackageFromPackage.model_fields.keys())
    response_fields = set(_PackageFromCreateMediaBuyResponse.model_fields.keys())

    print("=== Field Comparison ===")
    print(f"Full Package fields: {len(full_fields)}")
    print(f"Response Package fields: {len(response_fields)}")
    print()

    print("Fields only in Full Package:")
    for field in sorted(full_fields - response_fields):
        print(f"  - {field}")
    print()

    print("Fields only in Response Package:")
    for field in sorted(response_fields - full_fields):
        print(f"  - {field}")
    print()

    print("Shared fields:")
    for field in sorted(full_fields & response_fields):
        print(f"  - {field}")
    print()


def demonstrate_type_safety():
    """Show how type checkers handle these distinct types."""

    # Create minimal package (as returned in create response)
    minimal = _PackageFromCreateMediaBuyResponse(
        buyer_ref="buyer-123",
        package_id="pkg-001",
    )

    # Create full package (note: needs required fields based on schema)
    full = _PackageFromPackage(
        package_id="pkg-001",
        buyer_ref="buyer-123",
        product_id="prod-001",
        pricing_option_id="pricing-001",
        status="draft",  # Required field
        impressions=10000,
        budget=500.0,
        # ... many more fields available
    )

    print("=== Type Safety ===")
    print(f"Minimal package type: {type(minimal).__name__}")
    print(f"Full package type: {type(full).__name__}")
    print()

    # isinstance() works correctly (checks actual type, not name)
    print("isinstance checks:")
    print(f"  minimal is Response Package? {isinstance(minimal, _PackageFromCreateMediaBuyResponse)}")
    print(f"  minimal is Full Package? {isinstance(minimal, _PackageFromPackage)}")
    print(f"  full is Full Package? {isinstance(full, _PackageFromPackage)}")
    print(f"  full is Response Package? {isinstance(full, _PackageFromCreateMediaBuyResponse)}")
    print()


def demonstrate_best_practices():
    """Show the recommended way to work with Package types."""

    print("=== Best Practices ===")
    print()

    print("✅ CORRECT: Import from public API")
    print("   from adcp import Package")
    print("   pkg = Package(package_id='...', ...)")
    print()

    print("✅ CORRECT: Use in type hints")
    print("   def process_package(pkg: Package) -> None: ...")
    print()

    print("❌ WRONG: Import from internal modules")
    print("   from adcp.types.generated_poc.package import Package  # NO!")
    print("   from adcp.types._generated import Package  # NO!")
    print()

    print("ℹ️  ADVANCED: If you need to distinguish response vs full package:")
    print("   from adcp.types._generated import (")
    print("       _PackageFromPackage as FullPackage,")
    print("       _PackageFromCreateMediaBuyResponse as CreatedPackage,")
    print("   )")
    print("   def handle_response(pkg: CreatedPackage) -> None: ...")
    print()


def demonstrate_pitfall():
    """Show a potential pitfall with runtime type checking."""

    minimal = _PackageFromCreateMediaBuyResponse(
        buyer_ref="buyer-123", package_id="pkg-001"
    )

    full = _PackageFromPackage(
        package_id="pkg-001",
        buyer_ref="buyer-123",
        status="draft",  # Required
    )

    print("=== Potential Pitfall ===")
    print()
    print("❌ BAD: Checking type by __name__ (doesn't distinguish variants)")
    print(f"   type(minimal).__name__ == 'Package': {type(minimal).__name__ == 'Package'}")
    print(f"   type(full).__name__ == 'Package': {type(full).__name__ == 'Package'}")
    print("   Both return True, even though they're different types!")
    print()

    print("✅ GOOD: Using isinstance() (correctly distinguishes variants)")
    print(f"   isinstance(minimal, _PackageFromCreateMediaBuyResponse): {isinstance(minimal, _PackageFromCreateMediaBuyResponse)}")
    print(f"   isinstance(full, _PackageFromPackage): {isinstance(full, _PackageFromPackage)}")
    print()


def why_collisions_exist():
    """Explain the root cause of Package collision."""

    print("=== Why Do Collisions Exist? ===")
    print()
    print("The AdCP JSON schemas define 'Package' in two different contexts:")
    print()
    print("1. package.json: Full package definition with all fields")
    print("   - Used for complete package data")
    print("   - Has fields like: impressions, budget, pacing, targeting, etc.")
    print("   - This is what you work with in your application")
    print()
    print("2. create-media-buy-response.json: Minimal package info")
    print("   - Used in API response to confirm package creation")
    print("   - Only has: buyer_ref, package_id")
    print("   - This is what the API returns when creating a media buy")
    print()
    print("The code generator creates both types with the name 'Package'")
    print("because that's how they're named in the schemas.")
    print()
    print("Our solution: Import both, export full definition as 'Package',")
    print("and provide qualified names for internal use if needed.")
    print()


def main():
    """Run all demonstrations."""
    why_collisions_exist()
    print("=" * 80)
    print()

    demonstrate_collision()
    print("=" * 80)
    print()

    demonstrate_field_differences()
    print("=" * 80)
    print()

    demonstrate_type_safety()
    print("=" * 80)
    print()

    demonstrate_best_practices()
    print("=" * 80)
    print()

    demonstrate_pitfall()


if __name__ == "__main__":
    main()
