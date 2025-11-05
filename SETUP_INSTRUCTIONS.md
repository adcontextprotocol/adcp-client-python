# Setup Instructions for Python AdCP Client

## Copy Files to New Repository

1. **Copy all files from this temp directory** to your Python workspace:
```bash
cp -r temp-python-client/* /Users/brianokelley/conductor/adcp-client-python/.conductor/lincoln-v5/
```

2. **Navigate to the Python workspace**:
```bash
cd /Users/brianokelley/conductor/adcp-client-python/.conductor/lincoln-v5
```

3. **Install development dependencies**:
```bash
pip install -e ".[dev]"
```

4. **Run initial tests**:
```bash
pytest
```

## Project Structure

```
adcp-client-python/
├── pyproject.toml              # Project configuration
├── README.md                   # User documentation
├── LICENSE                     # Apache 2.0 license
├── CONTRIBUTING.md             # Contributor guide
├── .gitignore                 # Python gitignore
│
├── src/adcp/                  # Main package
│   ├── __init__.py           # Package exports
│   ├── client.py             # ADCPClient & ADCPMultiAgentClient
│   │
│   ├── protocols/            # Protocol adapters
│   │   ├── __init__.py
│   │   ├── base.py          # Protocol interface
│   │   ├── a2a.py           # A2A adapter (needs official SDK)
│   │   └── mcp.py           # MCP adapter (needs official SDK)
│   │
│   ├── types/                # Type definitions
│   │   ├── __init__.py
│   │   └── core.py          # Core types (Pydantic models)
│   │
│   └── utils/                # Utilities
│       ├── __init__.py
│       └── operation_id.py  # Operation ID generation
│
└── tests/                    # Test suite
    ├── __init__.py
    └── test_client.py       # Basic client tests
```

## Next Steps

### 1. Find or Create Python SDK Clients

**A2A Protocol:**
- Search for official Python A2A SDK
- If not available, we need to implement proper A2A client
- Current implementation in `src/adcp/protocols/a2a.py` is a placeholder

**MCP Protocol:**
- Search for official Python MCP SDK
- The TypeScript version uses `@modelcontextprotocol/sdk`
- Current implementation in `src/adcp/protocols/mcp.py` is a placeholder

### 2. Generate Types from AdCP Schema

Similar to the TypeScript version, you'll want to:
1. Fetch the AdCP JSON schema
2. Generate Pydantic models from it
3. Place generated types in `src/adcp/types/tools.py`

You can use tools like:
- `datamodel-code-generator` (generates Pydantic from JSON Schema)
- Custom script similar to TypeScript version

### 3. Implement Missing Features

Current implementation includes:
- ✅ Basic project structure
- ✅ Core client classes (ADCPClient, ADCPMultiAgentClient)
- ✅ Protocol adapter interface
- ✅ Type definitions with Pydantic
- ✅ Basic tests
- ⚠️  Protocol adapters (placeholders - need official SDKs)
- ❌ Full tool implementations (only get_products, list_creative_formats, create_media_buy shown)
- ❌ Webhook signature verification
- ❌ Property discovery (PropertyCrawler)
- ❌ Generated types from AdCP schema

### 4. Testing

Add comprehensive tests:
- Unit tests for each client method
- Protocol adapter tests (with mocking)
- Integration tests with test agents
- Webhook handling tests

### 5. Documentation

- Add more usage examples to README
- Create API documentation (Sphinx)
- Add type stubs for better IDE support

## Development Commands

```bash
# Install in development mode
pip install -e ".[dev]"

# Run tests
pytest

# Run tests with coverage
pytest --cov=adcp --cov-report=html

# Type checking
mypy src/

# Format code
black src/ tests/

# Lint code
ruff check src/ tests/

# Fix linting issues automatically
ruff check src/ tests/ --fix
```

## Publishing to PyPI

When ready to publish:

1. Update version in `pyproject.toml`
2. Build the package:
```bash
python -m build
```

3. Upload to PyPI:
```bash
python -m twine upload dist/*
```

## Questions?

- Check the TypeScript implementation for reference: `/Users/brianokelley/conductor/adcp-client-1`
- Review AdCP protocol spec
- Email: maintainers@adcontextprotocol.org
