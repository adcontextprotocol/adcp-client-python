# 📦 Python AdCP Client - Ready to Copy!

This directory contains the initial structure for the Python AdCP client library.

## Quick Copy Command

```bash
# Copy everything to your Python workspace
cp -r /Users/brianokelley/conductor/adcp-client-1/.conductor/valencia-v4/temp-python-client/* \
     /Users/brianokelley/conductor/adcp-client-python/.conductor/lincoln-v5/
```

## What's Included

### ✅ Complete Files
- `pyproject.toml` - Modern Python project configuration
- `README.md` - User-facing documentation
- `LICENSE` - Apache 2.0 license
- `CONTRIBUTING.md` - Contributor guidelines
- `.gitignore` - Python-specific gitignore
- `SETUP_INSTRUCTIONS.md` - Detailed setup guide

### ✅ Core Library (`src/adcp/`)
- `client.py` - ADCPClient & ADCPMultiAgentClient classes
- `types/core.py` - Pydantic models for type safety
- `protocols/base.py` - Protocol adapter interface
- `protocols/a2a.py` - A2A protocol adapter (placeholder)
- `protocols/mcp.py` - MCP protocol adapter (placeholder)
- `utils/operation_id.py` - Operation ID generation

### ✅ Tests (`tests/`)
- `test_client.py` - Basic client tests

### ✅ Examples (`examples/`)
- `basic_usage.py` - Simple single-agent example
- `multi_agent.py` - Multi-agent parallel execution example

## File Count

```
Total Python files: 12
Total documentation files: 5
Total test files: 1
Example files: 2
```

## Next Steps After Copying

1. **Navigate to Python workspace**:
   ```bash
   cd /Users/brianokelley/conductor/adcp-client-python/.conductor/lincoln-v5
   ```

2. **Install in development mode**:
   ```bash
   pip install -e ".[dev]"
   ```

3. **Run tests**:
   ```bash
   pytest
   ```

4. **Key TODOs** (see SETUP_INSTRUCTIONS.md for details):
   - [ ] Find or implement official Python A2A SDK client
   - [ ] Find or implement official Python MCP SDK client
   - [ ] Generate types from AdCP JSON schema → `src/adcp/types/tools.py`
   - [ ] Implement all AdCP tools (currently only 3 shown as examples)
   - [ ] Add webhook signature verification
   - [ ] Implement PropertyCrawler for discovery
   - [ ] Add comprehensive tests

## Architecture Highlights

### Type Safety with Pydantic
```python
from adcp.types import AgentConfig, TaskResult

config = AgentConfig(
    id="agent",
    agent_uri="https://...",
    protocol="a2a"  # ← Validated by Pydantic
)
```

### Protocol Adapter Pattern
Similar to TypeScript version - clean separation of concerns:
```python
class ProtocolAdapter(ABC):
    async def call_tool(self, tool_name: str, params: Dict) -> TaskResult
```

### Async/Await Throughout
Modern Python async patterns:
```python
result = await client.get_products(brief="Coffee")
results = await multi_client.get_products(brief="Coffee")  # Parallel!
```

## Dependencies

**Runtime:**
- `httpx>=0.24.0` - Async HTTP client
- `pydantic>=2.0.0` - Data validation
- `typing-extensions>=4.5.0` - Type hints backport

**Development:**
- `pytest>=7.0.0` - Testing framework
- `pytest-asyncio>=0.21.0` - Async test support
- `mypy>=1.0.0` - Static type checking
- `black>=23.0.0` - Code formatting
- `ruff>=0.1.0` - Fast linting

## Design Principles

Following the same principles as TypeScript version:
1. **Protocol-agnostic API** - Same interface for A2A and MCP
2. **Type safety** - Full type hints + Pydantic validation
3. **Async by default** - Native Python async/await
4. **Activity observability** - Events for all operations
5. **Webhook handling** - Built-in support for async completions

## Support

Once you copy these files and start development:
- Read `SETUP_INSTRUCTIONS.md` for detailed next steps
- Reference TypeScript implementation for behavior
- Check AdCP protocol specification
- Email: maintainers@adcontextprotocol.org

---

**Ready to go! Just run the copy command above and start developing.** 🚀
