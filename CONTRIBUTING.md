# Contributing to AdCP Python Client

Thank you for your interest in contributing to the AdCP Python client!

## Development Setup

This repository expects `uv` on your `PATH` for the local contributor
environment because the pre-commit hooks run through `uv run` to match CI
dependencies.

1. Clone the repository:
```bash
git clone https://github.com/adcontextprotocol/adcp-client-python.git
cd adcp-client-python
```

2. Install dependencies and pre-commit hooks:
```bash
make bootstrap
```

3. Run tests:
```bash
make test
```

4. Format code:
```bash
make format
make lint
```

5. Type check:
```bash
make typecheck-all
```

For the core local CI-style pass before opening a PR, run:

```bash
make ci-local
```

This covers lint, all type-check contracts, tests, and generated-code
validation. GitHub Actions still runs specialized jobs such as storyboard
runners, Postgres conformance, and conventional-commit validation.

## Project Structure

```
src/adcp/
├── __init__.py           # Main exports
├── client.py             # ADCPClient & ADCPMultiAgentClient
├── canonical_formats/    # Canonical format fixtures and adapters
├── compat/               # Legacy protocol compatibility adapters
├── decisioning/          # DecisioningPlatform framework
├── protocols/            # A2A and MCP client adapters
├── server/               # Server framework, auth, routing, middleware
├── signing/              # Request signing, verification, JWKS, replay stores
├── testing/              # In-process test helpers and test agents
├── types/                # Public types, generated models, mypy plugin
├── utils/                # Shared helpers
└── validation/           # Schema validation hooks and loaders
```

## Guidelines

### Code Style
- Follow PEP 8
- Use type hints everywhere
- Max line length: 100 characters
- Use `black` for formatting
- Use `ruff` for linting

### Testing
- Write tests for all new features
- Use pytest fixtures for common setup
- Aim for >80% code coverage
- Use `pytest-asyncio` for async tests

### Type Safety
- All functions must have type hints
- Use Pydantic for data validation
- Run `make typecheck-all` before committing
- `tests/type_checks/` is the adopter-facing type contract suite. Fixtures must
  pass `mypy --strict` without `# type: ignore` suppressions.

### Adding a public type/export

`adcp` and `adcp.types` are lazy (PEP 562): `import adcp` is ~2ms and does not
build the generated Pydantic graph or import the client. The first access to any
AdCP type builds the full graph once (~1s per process). The runtime resolution
lives in a `__getattr__` under `if not TYPE_CHECKING:`; type checkers see the
surface only through an explicit `TYPE_CHECKING` re-export block. Because of that
split, a new public export must be added in **both** places — the lazy runtime
map and the `TYPE_CHECKING` block — or it silently breaks lazy resolution or
type-checker visibility. See the "Import Architecture for Generated Types" section
in `CLAUDE.md` for the layering this protects.

Pick the surface you are adding to:

- **Top-level `adcp` export** (`from adcp import Foo`): add the name to the owning
  module's tuple in `_LAZY_MODULES`, to the matching `from <module> import (...)`
  block under `TYPE_CHECKING`, and to `__all__` — all three in
  `src/adcp/__init__.py`.

- **`adcp.types` export** (`from adcp.types import Foo`): the name is bound in
  `src/adcp/types/_eager.py` (the eager body that realizes the graph). In
  `src/adcp/types/__init__.py`, add it to `__all__` and to the `from
  adcp.types._eager import (...)` block under `TYPE_CHECKING`. If it is an internal
  re-export helper that is intentionally *not* in `__all__`, add it to
  `_EAGER_ONLY_EXTRAS` instead (this constant must match `_eager`'s namespace
  exactly).

- **Curated partial module** (`adcp.types.media_buy`, `creative`, `signals`,
  `protocol`, `buyer`, `seller`): add the name to that module's `__all__` and its
  `from adcp.types import (...)` block under `TYPE_CHECKING`. The name must already
  be exported from `adcp.types` — partial modules only re-curate that surface; they
  never import the generated layer.

Never import from `adcp.types.generated_poc.*` or `adcp.types._generated` outside
the allowlisted layering modules (`_generated.py`, `aliases.py`, `_ergonomic.py`,
`_forward_compat.py`, `capabilities.py`, `canonical_decl.py`, `_eager.py`, and
`types/__init__.py`). The generated class names are unstable across schema regen.

After an intentional change to `adcp.__all__` or `adcp.types.__all__`, regenerate
the public-API snapshot:

```bash
python scripts/regenerate_public_api_snapshot.py
```

These guards enforce the contract and run in `make ci-local`:

- `tests/test_import_layering.py` — no new module may import the generated layer.
- `tests/test_lazy_types.py` — lazy/eager parity, fast-fail on unknown names, and
  `_EAGER_ONLY_EXTRAS` matching `_eager`.
- `tests/test_public_api.py` — the public-API snapshot.

### Documentation
- Add docstrings to all public functions
- Use Google-style docstrings
- Update README.md for new features
- Include usage examples

## Pull Request Process

1. Create a feature branch from `main`
2. Make your changes
3. Run tests and type checks
4. Update documentation
5. Submit PR with clear description

### PR Title Format

This repository uses squash merges. The PR title becomes the commit subject that
release-please reads to build the CHANGELOG and determine version bumps.

**The description portion of the commit subject — the text after `type(scope):` —
must not contain `(`, `)`, or `"` characters.** The release-please parser treats
those characters as grammar tokens when they appear in the description and silently
drops the commit from the CHANGELOG with no error signal. (The `type(scope)` prefix
itself is fine; only the description portion is constrained.)

**Wrong — parser drops these commits silently:**

```
fix(auth): synthesize AuthInfo(kind="bearer") in _build_request_context
feat(auth): serve(auth=BearerTokenAuth(...)) — A2A sibling shortcut
```

**Right — move code examples and parenthetical details to the PR body:**

```
fix(auth): synthesize bearer AuthInfo in _build_request_context
feat(auth): add A2A sibling and cross-transport shortcut for bearer auth
```

Place code snippets, type names with parens, and parenthetical clarifications in the PR
body (release-please reads body footers like `BREAKING CHANGE:` and `Fixes #N` but
otherwise ignores the body for CHANGELOG purposes).

A `commit-msg` pre-commit hook (`scripts/check-commit-msg.sh`) catches violations on
direct commits. It does **not** catch squash-merge subjects (those are set by GitHub on
merge from the PR title), so keeping the PR title clean is the primary responsibility.
Hook setup is included in step 2 of Development Setup above.

## Questions?

Open an issue or email maintainers@adcontextprotocol.org
