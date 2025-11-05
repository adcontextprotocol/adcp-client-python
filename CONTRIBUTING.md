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

## Questions?

Open an issue or email maintainers@adcontextprotocol.org
