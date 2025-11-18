# API Documentation Strategy

## Current State

**What we have:**
- ✅ Excellent README with examples and usage patterns
- ✅ One doc in `docs/extending-types.md`
- ✅ Full type hints in code (docstrings on many classes/methods)
- ✅ Pydantic models with Field descriptions auto-generated from JSON Schema
- ⚠️ No auto-generated API reference documentation

**What users see on GitHub:**
- README.md renders beautifully on GitHub homepage
- Users can browse code with type hints in their IDE
- Field descriptions from JSON Schema visible in IDE tooltips

## Recommended: pdoc3 for API Reference

**Why pdoc3:**
1. **Zero configuration** - Works with existing docstrings
2. **GitHub Pages ready** - Single command generates static HTML
3. **Type hint aware** - Shows full type signatures from annotations
4. **Pydantic friendly** - Extracts Field descriptions from Pydantic models
5. **Fast** - Regenerates docs in ~1 second
6. **Pythonic** - Uses Python's own introspection, no special markup

**Alternatives considered:**
- **Sphinx** - Overkill for a single library, requires RST or complex setup
- **MkDocs** - Better for narrative docs, less ideal for API reference
- **pdoc (modern)** - Similar but pdoc3 has better Pydantic support

## Implementation Plan

### 1. Add pdoc3 Dependency

```toml
# pyproject.toml
[project.optional-dependencies]
docs = [
    "pdoc3>=0.10.0",
]
```

### 2. Generate Documentation

```bash
# Install docs dependencies
uv pip install -e ".[docs]"

# Generate docs (output to docs/api/)
pdoc --html --output-dir docs/api adcp

# Or serve locally for development
pdoc --http :8080 adcp
```

### 3. GitHub Pages Setup

**Option A: GitHub Actions (Recommended)**

Create `.github/workflows/docs.yml`:

```yaml
name: Build and Deploy Docs

on:
  push:
    branches: [main]
  release:
    types: [published]

jobs:
  docs:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4

      - uses: actions/setup-python@v5
        with:
          python-version: '3.11'

      - name: Install dependencies
        run: |
          pip install -e ".[docs]"

      - name: Build docs
        run: |
          pdoc --html --output-dir docs/api adcp

      - name: Deploy to GitHub Pages
        uses: peaceiris/actions-gh-pages@v3
        with:
          github_token: ${{ secrets.GITHUB_TOKEN }}
          publish_dir: docs/api/adcp
```

**Option B: Manual**

```bash
# Build docs
pdoc --html --output-dir docs/api adcp

# Commit to gh-pages branch
git checkout gh-pages
cp -r docs/api/adcp/* .
git add .
git commit -m "Update API docs"
git push origin gh-pages
```

### 4. Add Link to README

```markdown
## Documentation

- **API Reference**: [docs.adcontextprotocol.org/python](https://docs.adcontextprotocol.org/python) or [GitHub Pages](https://adcontextprotocol.github.io/adcp-client-python/)
- **Protocol Spec**: [AdCP Specification](https://github.com/adcontextprotocol/adcp)
- **Examples**: See [examples/](examples/) directory
```

## What Users Will See

### Main Package (`adcp`)

**Module: `adcp`**
- All exported types with descriptions
- `ADCPClient` class with all methods
- `ADCPMultiAgentClient` for multi-agent operations
- Test helpers (test_agent, test_agent_a2a, etc.)
- Exception hierarchy

### Type Reference (`adcp.types`)

**Module: `adcp.types`**
- Link to stable API layer
- Semantic aliases documentation
- Import guidelines

**Module: `adcp.types.stable`**
- All stable types with full descriptions from JSON Schema
- Request/Response types for all operations
- Domain types (BrandManifest, Product, etc.)
- Pricing options with discriminators
- Status enums

### Examples in Docstrings

Current docstrings are good. Example of well-documented method:

```python
async def get_products(
    self,
    request: GetProductsRequest
) -> TaskResult[GetProductsResponse]:
    """Get available advertising products from the agent.

    Args:
        request: Product discovery request with brief and filters

    Returns:
        TaskResult containing GetProductsResponse with available products

    Example:
        >>> request = GetProductsRequest(brief="Coffee brands")
        >>> result = await client.get_products(request)
        >>> if result.success:
        ...     print(f"Found {len(result.data.products)} products")
    """
```

## Benefits for Users

1. **Browseable API Reference** - Can explore all types and methods in browser
2. **Search** - pdoc3 adds search functionality across all docs
3. **Type Visibility** - Full type signatures visible (not just in IDE)
4. **Field Descriptions** - Pydantic Field descriptions from JSON Schema shown
5. **Link Stability** - URLs like `/adcp.html#adcp.ADCPClient.get_products` are stable
6. **Mobile Friendly** - Responsive HTML works on phones

## Maintenance

**Regeneration:**
- Automatically on PR merge to main (via GitHub Actions)
- Manually when cutting releases
- No maintenance needed - just keep docstrings up to date

**Versioning:**
- Each release can have its own docs (e.g., `/v2.4.0/`)
- Latest always at root
- Archive old versions for reference

## Quick Start for Contributors

```bash
# Install docs dependencies
uv pip install -e ".[docs]"

# Build and serve locally
pdoc --http :8080 adcp

# Open http://localhost:8080 in browser
# Edit docstrings, refresh page to see changes
```

## Example Output Structure

```
docs/api/
└── adcp/
    ├── index.html              # Main package overview
    ├── client.html             # ADCPClient class
    ├── exceptions.html         # Exception hierarchy
    ├── testing.html            # Test helpers
    ├── types/
    │   ├── index.html          # Types overview
    │   ├── stable.html         # Stable API types
    │   ├── aliases.html        # Semantic aliases
    │   └── generated.html      # Generated types (internal)
    └── validation.html         # Validation utilities
```

## Cost

- **Setup time**: ~30 minutes
- **Maintenance**: Near zero (auto-generated from docstrings)
- **CI time**: +1 minute per build
- **Hosting**: Free (GitHub Pages)

## Recommendation

**Implement pdoc3 documentation:**
1. Add to dev dependencies
2. Set up GitHub Actions workflow
3. Enable GitHub Pages
4. Update README with link

This gives users a professional, searchable API reference with minimal overhead.
