# Python SDK Development Learnings

## Type Safety & Code Generation

**Auto-generate from specs when possible**
- Download schemas from canonical source (e.g., adcontextprotocol.org/schemas)
- Generate Pydantic models automatically - keeps types in sync with spec
- Validate generated code in CI (syntax check + import test)
- For missing upstream types, add type aliases with clear comments explaining why

**Handling Missing Schema Types**
When schemas reference types that don't exist upstream:
```python
# MISSING SCHEMA TYPES (referenced but not provided by upstream)
# These types are referenced in schemas but don't have schema files
FormatId = str
PackageRequest = dict[str, Any]
```

**Type Checking Best Practices**
- Use `TYPE_CHECKING` for optional dependencies to avoid runtime import errors
- Use `cast()` for JSON deserialization to satisfy mypy's `no-any-return` checks
- Add specific `type: ignore` comments (e.g., `# type: ignore[no-any-return]`) rather than blanket ignores
- Test type checking in CI across multiple Python versions (3.10+)

## Testing Strategy

**Mock at the Right Level**
- For HTTP clients: Mock `_get_client()` method, not the httpx class directly
- For async operations: Use `AsyncMock` for async functions, `MagicMock` for sync methods
- Remember: httpx's `response.json()` is SYNCHRONOUS, not async

**Test API Changes Properly**
- When API changes from kwargs to typed objects, update tests to match
- Remove tests for non-existent methods rather than keep failing tests
- Test the API as it exists, not as we wish it existed

## CI/CD & Release Automation

**GitHub Actions Secrets**
- Secret names matter! Check actual secret name in repository settings
- Common pattern: `PYPY_API_TOKEN` (not `PYPI_API_TOKEN`) for PyPI publishing
- Test locally with `python -m build` before relying on CI

**Release Please Workflow**
- Runs automatically on push to main
- Creates release PR with version bump and changelog
- When release PR is merged, automatically publishes to PyPI
- Requires proper `[project.scripts]` entry point in pyproject.toml for CLI tools

**Entry Points for CLI Tools**
```toml
[project.scripts]
toolname = "package.__main__:main"
```
This enables `uvx toolname` and `pip install toolname` to work correctly.

## Python-Specific Patterns

**Optional Dependencies with TYPE_CHECKING**
```python
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from optional_lib import SomeType

try:
    from optional_lib import SomeType as _SomeType
    AVAILABLE = True
except ImportError:
    AVAILABLE = False
```

**Atomic File Operations**
For config files with sensitive data:
```python
temp_file = CONFIG_FILE.with_suffix(".tmp")
with open(temp_file, "w") as f:
    json.dump(config, f, indent=2)
temp_file.replace(CONFIG_FILE)  # Atomic rename
```

**Connection Pooling**
```python
# Reuse HTTP client across requests
self._client: httpx.AsyncClient | None = None

async def _get_client(self) -> httpx.AsyncClient:
    if self._client is None:
        limits = httpx.Limits(
            max_keepalive_connections=10,
            max_connections=20,
        )
        self._client = httpx.AsyncClient(limits=limits)
    return self._client
```

## Common Pitfalls to Avoid

**String Escaping in Code Generation**
Always escape in this order:
1. Backslashes first: `\\` → `\\\\`
2. Then quotes: `"` → `\"`
3. Then control chars (newlines, tabs)

Wrong order creates invalid escape sequences!

**Python Version Requirements**
- Union syntax `str | None` requires Python 3.10+
- Always include `from __future__ import annotations` at top of files
- Use `target-version = "py310"` in ruff/black config
- Test in CI across all supported Python versions

**Test Fixtures vs. Mocks**
- Don't over-mock - it hides serialization bugs
- Test actual API calls when possible
- Use real Pydantic validation in tests
- Mock external services, not internal logic

## Additional Important Reminders

**NEVER**:
- Assume a "typo" without checking the actual secret name in GitHub settings

**ALWAYS**:
- Verify secret names match repository settings before "fixing" them
