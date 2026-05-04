# Contributing to AdCP Python Client

Thank you for your interest in contributing to the AdCP Python client!

## Development Setup

1. Clone the repository:
```bash
git clone https://github.com/adcontextprotocol/adcp-client-python.git
cd adcp-client-python
```

2. Install dependencies:
```bash
pip install -e ".[dev]"
```

3. Run tests:
```bash
pytest
```

4. Format code:
```bash
black src/ tests/
ruff check src/ tests/ --fix
```

5. Type check:
```bash
mypy src/
```

## Project Structure

```
src/adcp/
├── __init__.py           # Main exports
├── client.py             # ADCPClient & ADCPMultiAgentClient
├── protocols/
│   ├── base.py          # Protocol interface
│   ├── a2a.py           # A2A adapter
│   └── mcp.py           # MCP adapter
├── types/
│   ├── core.py          # Core types
│   └── tools.py         # Generated from AdCP schema
└── utils/
    └── operation_id.py  # Utilities
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
- Run `mypy` before committing

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

**The description portion of the commit subject — everything after `type(scope):` —
must not contain `(`, `)`, or `"` characters.** The release-please parser treats
these as grammar tokens and silently drops the commit from the CHANGELOG with no
error signal.

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
description body, not the title. release-please reads body footers (`BREAKING CHANGE:`,
`Fixes #N`) but otherwise ignores the body for CHANGELOG purposes.

A `commit-msg` pre-commit hook (`scripts/check-commit-msg.sh`) catches violations on
direct commits. It does **not** catch squash-merge subjects (those are set by GitHub on
merge from the PR title), so keeping the PR title clean is the primary responsibility.
To enable the hook: `pre-commit install --hook-type commit-msg`.

## Questions?

Open an issue or email maintainers@adcontextprotocol.org
