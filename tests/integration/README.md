# Integration Tests

Integration tests verify the SDK works correctly against real AdCP agents.

## Test Coverage

### test-agent.adcontextprotocol.org
- **Protocols**: A2A and MCP
- **Purpose**: General AdCP protocol testing
- **Tests**:
  - `test_get_products_mcp` - Get products via MCP
  - `test_get_products_a2a` - Get products via A2A
  - `test_protocol_equivalence` - Verify both protocols work similarly
  - `test_simple_api_mcp` - Simple API with MCP
  - `test_simple_api_a2a` - Simple API with A2A
  - `test_error_handling_mcp` - Error handling with MCP
  - `test_error_handling_a2a` - Error handling with A2A

### creative.adcontextprotocol.org
- **Protocols**: MCP only
- **Purpose**: Creative preview functionality
- **Tests**:
  - `test_list_creative_formats` - List available creative formats
  - `test_error_handling` - Error handling for invalid endpoints

## Running Tests

### Run all integration tests
```bash
pytest tests/integration/ -v -m integration
```

### Run specific agent tests
```bash
# Test-agent only
pytest tests/integration/test_reference_agents.py::TestTestAgent -v -m integration

# Creative agent only
pytest tests/integration/test_reference_agents.py::TestCreativeAgent -v -m integration
```

### Run specific protocol tests
```bash
# MCP only
pytest tests/integration/ -v -m integration -k "mcp"

# A2A only
pytest tests/integration/ -v -m integration -k "a2a"
```

### Run with verbose output
```bash
pytest tests/integration/ -v -m integration -s
```

## CI Integration

Integration tests are:
- **Excluded by default** in local test runs (via `pytest.ini` marker configuration)
- **Run nightly in CI** against reference agents
- **Can be triggered manually** via GitHub Actions workflow

## Authentication

Currently, the test-agent and creative agent do not require authentication for basic operations. If authentication is added in the future:

1. Set environment variables:
   ```bash
   export ADCP_TEST_AGENT_TOKEN="your-token"
   ```

2. Update test fixtures in `conftest.py` to use tokens

## Adding New Tests

When adding new integration tests:

1. Mark with `@pytest.mark.integration`
2. Use async test functions (`async def test_...`)
3. Use context managers (`async with ADCPClient(config) as client:`)
4. Include timeout handling for robustness
5. Test both success and error cases
6. Add to appropriate test class (TestTestAgent or TestCreativeAgent)

## Troubleshooting

### Tests are skipped
Integration tests are excluded by default. Use `-m integration` to run them:
```bash
pytest tests/integration/ -m integration
```

### Connection errors
- Check if reference agents are accessible
- Verify network connectivity
- Check if agent URLs are correct

### Timeout errors
- Increase timeout in agent config: `timeout=30.0`
- Check network latency
- Verify agent is responding

## Reference Agent URLs

- Test Agent: https://test-agent.adcontextprotocol.org
- Creative Agent: https://creative.adcontextprotocol.org
