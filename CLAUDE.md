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

**Import Architecture for Generated Types**
The type system has a strict layering to prevent brittleness:

```
generated_poc/*.py (internal, auto-generated from schemas)
    ↓
_generated.py (internal consolidation)
    ↓
stable.py + aliases.py + _ergonomic.py (public API / internal infrastructure)
    ↓
__init__.py (user-facing exports)
```

Only these modules may import from `generated_poc/` or `_generated.py`:
- `stable.py`: Re-exports base types with clean names
- `aliases.py`: Creates semantic aliases for numbered discriminated union types
- `_ergonomic.py`: Applies BeforeValidator coercion for type ergonomics

All other source code should import from `adcp.types` (the public API).

**Type Checking Best Practices**
- Use `TYPE_CHECKING` for optional dependencies to avoid runtime import errors
- Use `cast()` for JSON deserialization to satisfy mypy's `no-any-return` checks
- Add specific `type: ignore` comments (e.g., `# type: ignore[no-any-return]`) rather than blanket ignores
- Test type checking in CI across multiple Python versions (3.10+)

## ctx_metadata: write-only credentials prohibited

`RequestContext.metadata` (populated from the wire request's `context` extension)
is **echoed back into responses** per the AdCP context-echo contract. Adopters who
treat `metadata` as a generic KV bucket and store a credential there will discover
it round-trips to the buyer — and lands in the idempotency replay cache.

The dispatcher fail-closes on credential-shaped keys at `_build_request_context`.
If you see a `ValueError` like `ctx_metadata may not contain credential-shaped
keys`, migrate the value to `AuthInfo.credential` or a typed credential class.

**Wrong** — credential stored in metadata, round-trips into response context:

```python
ctx = RequestContext(metadata={"upstream.api_token": secret})  # ValueError
```

**Right** — credential stored in the typed `AuthInfo.credential` field:

```python
auth = AuthInfo(
    kind="api_key",
    key_id="kid_1",
    principal="agent.example.com",
    credential=ApiKeyCredential(kind="api_key", key_id="kid_1"),
)
ctx = RequestContext(auth_info=auth, metadata={"correlation_id": "req_xyz"})
```

The credential-shaped key suffix list is in
`adcp.decisioning.dispatch._CREDENTIAL_SHAPED_KEY_SUFFIXES` and matches
case-insensitively at any nesting depth: `credential`, `credentials`, `token`,
`secret`, `api_key`, `apikey`, `password`, `bearer`. Keys that don't match
(`correlation_id`, `feature_flag.beta_pricing`, `trace_id`) pass through.

For credentials the framework propagates to upstream calls (governance agents,
signal providers, audience activations), use the typed credential classes from
`adcp.decisioning`: `ApiKeyCredential`, `OAuthCredential`, `HttpSigCredential`.
The framework dispatch threads these explicitly without going through the
context-echo path.

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

## Pre-Commit Checks

Run these three checks locally before every commit — they mirror CI exactly:

```bash
ruff check src/           # Linter
mypy src/adcp/            # Type checker
pytest tests/ -v          # Tests
```

All three must pass. CI runs them across Python 3.10–3.13; locally running on your current version catches most issues.

## Parallel Agent Isolation (git worktrees)

When multiple agents work in the same checkout simultaneously, they clobber each other's
branches — there is no error, the work is silently lost. Use `git worktree` to give each
agent an isolated checkout.

> **Note:** Conductor worktrees handle this automatically via `.conductor.json`
> (runs `setup_conductor_env.py` + `pre-commit install` on create). Use the
> manual steps below only for raw `git worktree` outside of Conductor.
> See `CONDUCTOR.md` for Conductor-specific setup and troubleshooting.

**Create a worktree:**

```bash
git worktree add /tmp/claude-issue-<N>-<slug> -b claude/issue-<N>-<slug> main
```

**Setup checklist (run inside the new worktree):**

```bash
cd /tmp/claude-issue-<N>-<slug>
cp "$(git rev-parse --git-common-dir)/../.env" .env   # .env is not inherited
pre-commit install                 # hooks are not inherited from parent worktree
pip install -e .[dev]              # install in this worktree's context
```

**Teardown (after branch is merged):**

```bash
git worktree remove /tmp/claude-issue-<N>-<slug>
# or: git worktree prune   # removes all stale worktrees at once
```

**Branch naming:** always follow `claude/issue-<N>-<short-slug>` — branch-protection
rules enforce this pattern and PRs from non-conforming names may be rejected.

## Parallel Agent Coordination

When spawning parallel sub-agents, each agent must receive an explicit write-scope
declaration in its prompt. Agents do not detect or refuse out-of-scope writes at
runtime; this contract is the only enforcement mechanism.

**Prompt template for a parallel sub-agent:**

```
Task: <what this agent should do>

Read scope (consult freely):
- <file or glob pattern>
- <file or glob pattern>

Write scope (the ONLY files this agent may create or modify — exact paths, no globs):
- <exact file path>
- <exact file path>

Do not edit files outside your write scope even if you believe the change
would be an improvement. If you discover during execution that you need to write
a file outside your scope, stop and record it in your reply instead.
```

**Pre-spawn checklist:**

1. List every file any agent in the group is expected to write. If an agent may
   discover additional files during execution, note that in your scope planning —
   do not silently expand the write scope at runtime.
2. Partition that set so each file appears in exactly one agent's write scope.
   For files both agents need to write, assign one owner; have the other emit the
   required change as a note in its reply.
3. Pass each agent its partition explicitly (see template above).
4. After all agents complete, check for collisions:
   `git log --name-only --oneline -<N>` (N = number of agent commits), then look
   for the same file appearing in more than one entry.

## Additional Important Reminders

**NEVER**:
- Assume a "typo" without checking the actual secret name in GitHub settings

**ALWAYS**:
- Verify secret names match repository settings before "fixing" them
