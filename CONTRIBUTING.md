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
