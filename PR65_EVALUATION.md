# PR #65 Architecture Evaluation - Executive Summary

**Evaluation Date**: 2025-11-18
**PR**: Type Import Consolidation (#65)
**Reviewer**: Python Architecture Analysis
**Status**: ✅ APPROVED with recommendations

## TL;DR

The architecture is **Pythonic, type-safe, and production-ready**. The qualified naming strategy (`_PackageFromX`) elegantly solves a real upstream schema collision problem. Recommended improvements are minor refinements, not fundamental changes.

**Recommendation: Merge with high-priority improvements below.**

---

## Quick Reference

### Is this Pythonic? ✅ YES

- Uses leading underscore for internal modules (`_generated.py`)
- Follows PEP 8 naming conventions
- Provides stable public API via `__init__.py`
- Internal implementation details properly hidden

### Will it work with type checkers? ✅ YES

- Tested: No circular imports
- Types are distinct at runtime (`isinstance()` works correctly)
- Type checkers see `_PackageFromX` as different types (correct)
- No `Any` types or unsafe casts

### Are there better alternatives? ⚠️ NO SIGNIFICANT ALTERNATIVES

- Considered: namespace packages, multiple subpackages, dynamic generation
- Current approach is simpler and appropriate for auto-generated code
- Qualified naming is standard practice in Python for collision resolution

### Could it cause issues? ⚠️ MINOR CONCERNS

Three minor gotchas (all documented):
1. Runtime `type(obj).__name__` checks don't distinguish variants (use `isinstance()` instead)
2. Pickle serialization may break if paths change (use Pydantic JSON instead)
3. Collisions are detected but don't fail build (should fail on unexpected collisions)

---

## The Architecture

```
┌─────────────────────────────────────┐
│  User Code                          │
│  from adcp import Package, Product  │
└──────────────┬──────────────────────┘
               │
               ↓
┌─────────────────────────────────────┐
│  __init__.py (PUBLIC API)           │
│  - Re-exports stable types          │
│  - Semantic aliases (e.g., Package) │
└──────────────┬──────────────────────┘
               │
               ↓
┌──────────────────────────────────────┐
│  _generated.py (INTERNAL)            │
│  - Consolidates all generated types  │
│  - Handles name collisions           │
│  - Qualified names (_PackageFromX)   │
└──────────────┬───────────────────────┘
               │
               ↓
┌──────────────────────────────────────┐
│  generated_poc/*.py (INTERNAL)       │
│  - Auto-generated from JSON schemas  │
│  - Never modified manually           │
└──────────────────────────────────────┘
```

**Why this works:**
- Clear unidirectional dependencies (DAG, no cycles)
- Internal vs. public distinction via naming
- Allows schema evolution without breaking user code
- Type-safe collision resolution

---

## Key Design Decisions

### 1. Qualified Naming: `_PackageFromX`

**Decision**: Use underscore-prefixed qualified names for colliding types

```python
from adcp.types._generated import (
    _PackageFromPackage,                  # Full package definition
    _PackageFromCreateMediaBuyResponse,   # Minimal package in response
)
```

**Why it works:**
- Makes collision source explicit (from which module)
- Maintains all type information for type checkers
- Standard Python pattern (similar to `_` prefix for private)

**Why not alternatives:**
- ✗ Multiple subpackages: More complex, harder to maintain
- ✗ Dynamic generation: Overkill for current needs
- ✗ Ignore collision: Would lose one of the types

**Trade-offs:**
- 👍 Explicit and clear
- 👍 Type-safe
- 👎 Slightly verbose (but only for internal use)
- 👎 Couples name to module (acceptable for generated code)

### 2. Leading Underscore: `_generated.py`

**Decision**: Use `_generated.py` (not `generated.py`) for internal module

**Rationale:**
- PEP 8: "single leading underscore: weak 'internal use' indicator"
- Signals to users: "Don't import from here"
- Type checkers can warn on underscore imports
- Follows stdlib conventions (`_thread`, `_collections`)

**Status**: ✅ Already implemented correctly

### 3. Consolidation Layer

**Decision**: Single `_generated.py` re-exports all `generated_poc/*` types

**Why:**
- One place to handle collisions
- Simpler import paths for internal code
- Easier to maintain than scattered imports
- Can add logic (aliases, deprecations) in one place

**Alternative rejected**: Import directly from `generated_poc/*`
- Would leak internal structure
- No central place to handle collisions
- Harder to evolve schema structure

---

## Collision Handling Deep Dive

### The Problem

AdCP schemas define `Package` in two contexts:

```
package.json                        create-media-buy-response.json
┌──────────────────────┐           ┌─────────────────────┐
│ class Package:       │           │ class Package:      │
│   package_id: str    │           │   package_id: str   │
│   buyer_ref: str     │           │   buyer_ref: str    │
│   status: Status     │  ←→ NOT   │                     │
│   impressions: float │    SAME   └─────────────────────┘
│   budget: float      │            (2 fields)
│   ... 8 more fields  │
└──────────────────────┘
(12 fields)
```

These are **genuinely different types**, not duplicates:
- Full definition: Complete package with targeting, pacing, status
- Response version: Minimal acknowledgment (just IDs)

### The Solution

```python
# _generated.py
from adcp.types.generated_poc.package import Package as _PackageFromPackage
from adcp.types.generated_poc.create_media_buy_response import Package as _PackageFromCreateMediaBuyResponse

__all__ = [
    # ... other types
    "_PackageFromPackage",
    "_PackageFromCreateMediaBuyResponse",
]

# stable.py (public API)
from adcp.types._generated import _PackageFromPackage as Package
# Users get the full definition by default

# __init__.py
from adcp.types.stable import Package
# Final export to users
```

### Type Safety Verification

Run `examples/type_collision_demo.py` to see:

```python
minimal = _PackageFromCreateMediaBuyResponse(buyer_ref="x", package_id="y")
full = _PackageFromPackage(package_id="y", buyer_ref="x", status="draft")

# isinstance() correctly distinguishes them:
isinstance(minimal, _PackageFromCreateMediaBuyResponse)  # True
isinstance(minimal, _PackageFromPackage)                  # False
isinstance(full, _PackageFromPackage)                     # True
isinstance(full, _PackageFromCreateMediaBuyResponse)     # False
```

**Type checkers see these as different types** because they're defined in different modules.

---

## Testing Results

### ✅ Import Tests (No Circular Dependencies)

```bash
$ uv run python3 -c "import sys; import importlib;
  modules = ['adcp', 'adcp.types', 'adcp.types._generated', 'adcp.types.aliases', 'adcp.types.stable'];
  [importlib.import_module(m) for m in modules];
  print('All modules imported successfully')"
All modules imported successfully
```

### ✅ Type Identity Tests

```python
# Both have same __name__ but are distinct types
_PackageFromPackage.__name__                      # "Package"
_PackageFromCreateMediaBuyResponse.__name__       # "Package"
_PackageFromPackage is _PackageFromCreateMediaBuyResponse  # False ✓
```

### ✅ Field Verification

```
Full Package fields: 12
Response Package fields: 2

Fields only in Full Package:
  - bid_price, budget, creative_assignments, format_ids_to_provide,
    impressions, pacing, pricing_option_id, product_id, status, targeting_overlay

Shared fields:
  - buyer_ref, package_id
```

### ✅ Runtime Type Checking

```python
isinstance(minimal, _PackageFromCreateMediaBuyResponse)  # ✅ True
isinstance(minimal, _PackageFromPackage)                  # ✅ False
```

---

## Recommended Improvements

### High Priority (Before Merge)

#### 1. Rename `generated_poc/` → `_generated_poc/`

**Why:** Consistency with `_generated.py` convention

```bash
git mv src/adcp/types/generated_poc src/adcp/types/_generated_poc
# Update all imports
sed -i 's/from adcp.types.generated_poc/from adcp.types._generated_poc/g' src/adcp/types/_generated.py
```

**Impact:**
- Makes internal vs public distinction clearer
- Follows Python conventions throughout
- One-time migration cost

**Files affected:** 3-5 Python files, 2-3 docs

#### 2. Explicit Collision Policy in `consolidate_exports.py`

**Current issue:** Collisions are logged but don't fail build

**Proposed fix:**

```python
# scripts/consolidate_exports.py

KNOWN_COLLISIONS = {
    "Package": {
        "canonical": "package",  # Export as Package
        "variants": {
            "create_media_buy_response": "_PackageFromCreateMediaBuyResponse",
        },
        "reason": "Full package definition vs response acknowledgment",
    },
    "Asset": {
        "canonical": "brand_manifest",
        "variants": {
            "format": "_AssetFromFormat",
        },
        "reason": "Asset differs between brand manifests and format definitions",
    },
}

# In generate function:
if export_name in export_to_module and export_name not in KNOWN_COLLISIONS:
    # Unexpected collision - FAIL THE BUILD
    raise ValueError(
        f"Unexpected collision: {export_name} in {module_name} and {export_to_module[export_name]}. "
        f"Add to KNOWN_COLLISIONS in scripts/consolidate_exports.py if intentional."
    )
```

**Benefits:**
- Documents why collisions exist
- Fails on unexpected collisions (forces conscious decision)
- Makes collision handling maintainable

#### 3. Add Circular Import Test to CI

```python
# tests/test_import_architecture.py

def test_no_circular_imports():
    """Verify the import hierarchy has no circular dependencies."""
    import sys
    import importlib

    # Test imports in dependency order
    modules = [
        'adcp.types.base',
        'adcp.types._generated_poc.package',  # Sample from generated
        'adcp.types._generated',
        'adcp.types.aliases',
        'adcp.types.stable',
        'adcp',
    ]

    # Clear cache
    for mod in list(sys.modules.keys()):
        if mod.startswith('adcp'):
            del sys.modules[mod]

    # Import should succeed without circular dependency errors
    for mod_name in modules:
        importlib.import_module(mod_name)
```

Add to CI:
```yaml
# .github/workflows/ci.yml
- name: Test import architecture
  run: uv run pytest tests/test_import_architecture.py -v
```

### Medium Priority (Next Iteration)

#### 4. Document Qualified Naming in `_generated.py` Docstring

```python
# src/adcp/types/_generated.py
"""INTERNAL: Consolidated generated types.

DO NOT import from this module directly.
Use 'from adcp import Type' or 'from adcp.types.stable import Type' instead.

## Qualified Names for Colliding Types

Some types have name collisions in upstream schemas. These are exported with
qualified names indicating their source module:

- `_PackageFromPackage`: Full package definition from package.json
  - Fields: package_id, buyer_ref, status, impressions, budget, pacing, etc.
  - Use: When working with complete package data

- `_PackageFromCreateMediaBuyResponse`: Minimal package from create-media-buy-response.json
  - Fields: package_id, buyer_ref
  - Use: When handling API response acknowledgments

The public API exports `Package` → `_PackageFromPackage` (full definition).
Use qualified names only if you need to distinguish response vs full package.
"""
```

#### 5. Add Import Pattern Examples to README

```markdown
## Import Patterns

### ✅ Recommended: Public API

```python
from adcp import Product, Package, Format, BrandManifest
from adcp.types import MediaBuy, Creative
from adcp.types.stable import Property
```

### ❌ Avoid: Internal Modules

```python
# DON'T DO THIS
from adcp.types.generated_poc.product import Product  # ❌
from adcp.types._generated import Product              # ❌
from adcp.types._generated import _PackageFromPackage  # ❌ (unless you know why)
```

### ℹ️  Advanced: Discriminated Unions

For types with multiple variants, use semantic aliases:

```python
from adcp import (
    UrlPreviewRender,   # instead of PreviewRender1
    HtmlPreviewRender,  # instead of PreviewRender2
)
```
```

### Low Priority (Nice to Have)

#### 6. Lint Rule to Prevent Internal Imports

Custom ruff rule or pre-commit hook:

```python
# .pre-commit-hooks/check_internal_imports.py
"""Prevent imports from internal modules in user-facing code."""

def check_file(filepath: str) -> list[str]:
    if "test_" in filepath or filepath.endswith(("_generated.py", "aliases.py", "stable.py")):
        return []  # Allow in tests and internal modules

    with open(filepath) as f:
        for line_no, line in enumerate(f, 1):
            if "from adcp.types._generated_poc" in line:
                return [f"{filepath}:{line_no}: Don't import from _generated_poc - use public API"]
            if "from adcp.types._generated import" in line:
                return [f"{filepath}:{line_no}: Don't import from _generated - use public API"]
    return []
```

---

## Known Gotchas & Mitigations

### Gotcha 1: Runtime Type Name Checking

**Problem:**
```python
type(obj).__name__ == "Package"  # Matches BOTH Package types
```

**Mitigation:**
- Document to use `isinstance()` instead
- Both types have same `__name__` but different identity

**Impact:** Low (bad practice anyway)

### Gotcha 2: Pickle Serialization

**Problem:**
```python
import pickle
pkg = _PackageFromPackage(...)
data = pickle.dumps(pkg)  # Stores full module path
# If module path changes, unpickling fails
```

**Mitigation:**
- Use Pydantic's JSON serialization instead
- More portable across schema versions
- Recommended in docs

**Impact:** Low (Pydantic JSON is better choice)

### Gotcha 3: Silent Collision Handling

**Problem:** New collisions don't fail build (just logged)

**Mitigation:** Implement explicit collision policy (see Recommendation #2)

**Impact:** Medium (addressed by high-priority recommendation)

---

## Comparison to Alternative Approaches

### Alternative A: Multiple Subpackages

```python
from adcp.types.models import Package
from adcp.types.responses import CreatedPackage
```

**Pros:** More semantic organization
**Cons:** Many modules to maintain, unclear where shared types go
**Verdict:** Current approach is simpler for auto-generated code ✅

### Alternative B: Ignore Collisions (First-Wins)

```python
# Just export one Package, ignore the other
from adcp.types.generated_poc.package import Package
# create_media_buy_response.Package is silently ignored
```

**Pros:** Simpler code
**Cons:** Lose information, type mismatch errors at runtime
**Verdict:** Would break type safety ❌

### Alternative C: Namespace Packages

```python
from adcp.types.package import full as Package
from adcp.types.package import response as PackageResponse
```

**Pros:** Very explicit
**Cons:** Requires restructuring, harder to generate
**Verdict:** Overkill for current needs ❌

---

## Final Verdict

### Architecture Quality: 8/10

**Strengths:**
- ✅ Type-safe collision resolution
- ✅ Clear internal vs public API separation
- ✅ No circular imports
- ✅ Pythonic naming conventions
- ✅ Works correctly with type checkers
- ✅ Allows schema evolution

**Weaknesses:**
- ⚠️ Collision handling could be more explicit (addressed by recommendations)
- ⚠️ Missing some documentation (addressed by recommendations)
- ⚠️ `generated_poc` naming inconsistent with `_generated.py` (addressed by recommendations)

### Recommendation: ✅ APPROVE

Merge PR #65 with high-priority improvements:
1. Rename `generated_poc/` → `_generated_poc/`
2. Add explicit collision policy to build
3. Add circular import test to CI

This is a solid, production-ready architecture that properly separates concerns and maintains type safety.

---

## Resources

- **Full Analysis**: See `ARCHITECTURE_REVIEW.md` for detailed analysis
- **Demo**: Run `examples/type_collision_demo.py` to see collision handling in action
- **Tests**: All import tests passing, no circular dependencies detected

## Questions?

**Q: Should I import from `_generated.py`?**
A: No. Import from `adcp` or `adcp.types` or `adcp.types.stable`.

**Q: What if I need the response Package variant?**
A: Use `from adcp.types._generated import _PackageFromCreateMediaBuyResponse`, but this is rare.

**Q: Will schema updates break my code?**
A: Not if you import from public API. Internal changes don't affect `from adcp import Package`.

**Q: Why qualified names with underscores?**
A: Signals "internal use only" and makes collision source explicit.

**Q: Is this pattern common in Python?**
A: Yes. Similar to stdlib (`_thread`, `_collections`) and many projects with generated code.
