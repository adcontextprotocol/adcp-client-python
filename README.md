# adcp - Python Client for Ad Context Protocol

[![PyPI version](https://badge.fury.io/py/adcp.svg)](https://badge.fury.io/py/adcp)
[![License](https://img.shields.io/badge/License-Apache%202.0-blue.svg)](https://opensource.org/licenses/Apache-2.0)
[![Python](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)

Official Python client for the **Ad Context Protocol (AdCP)**. Build distributed advertising operations that work synchronously OR asynchronously with the same code.

## The Core Concept

AdCP operations are **distributed and asynchronous by default**. An agent might:
- Complete your request **immediately** (synchronous)
- Need time to process and **send results via webhook** (asynchronous)
- Ask for **clarifications** before proceeding
- Send periodic **status updates** as work progresses

**Your code stays the same.** You write handlers once, and they work for both sync completions and webhook deliveries.

## Installation

```bash
pip install adcp
```

## Quick Start: Distributed Operations

```python
from adcp import ADCPMultiAgentClient
from adcp.types import AgentConfig

# Configure agents and handlers
client = ADCPMultiAgentClient(
    agents=[
        AgentConfig(
            id="agent_x",
            agent_uri="https://agent-x.com",
            protocol="a2a"
        ),
        AgentConfig(
            id="agent_y",
            agent_uri="https://agent-y.com/mcp/",
            protocol="mcp"
        )
    ],
    # Webhook URL template (macros: {agent_id}, {task_type}, {operation_id})
    webhook_url_template="https://myapp.com/webhook/{task_type}/{agent_id}/{operation_id}",

    # Activity callback - fires for ALL events
    on_activity=lambda activity: print(f"[{activity.type}] {activity.task_type}"),

    # Status change handlers
    handlers={
        "on_get_products_status_change": lambda response, metadata: (
            db.save_products(metadata.operation_id, response.products)
            if metadata.status == "completed" else None
        )
    }
)

# Execute operation - library handles operation IDs, webhook URLs, context management
agent = client.agent("agent_x")
result = await agent.get_products(brief="Coffee brands")

# Check result
if result.status == "completed":
    # Agent completed synchronously!
    print(f"✅ Sync completion: {len(result.data.products)} products")

if result.status == "submitted":
    # Agent will send webhook when complete
    print(f"⏳ Async - webhook registered at: {result.submitted.webhook_url}")
```

## Features

### Full Protocol Support
- **A2A Protocol**: Native support for Agent-to-Agent protocol
- **MCP Protocol**: Native support for Model Context Protocol
- **Auto-detection**: Automatically detect which protocol an agent uses

### Type Safety
Full type hints with Pydantic validation:

```python
result = await agent.get_products(brief="Coffee brands")
# result: TaskResult[GetProductsResponse]

if result.success:
    for product in result.data.products:
        print(product.name, product.price)  # Full IDE autocomplete!
```

### Multi-Agent Operations
Execute across multiple agents simultaneously:

```python
# Parallel execution across all agents
results = await client.get_products(brief="Coffee brands")

for result in results:
    if result.status == "completed":
        print(f"Sync: {len(result.data.products)} products")
    elif result.status == "submitted":
        print(f"Async: webhook to {result.submitted.webhook_url}")
```

### Webhook Handling
Single endpoint handles all webhooks:

```python
from fastapi import FastAPI, Request

app = FastAPI()

@app.post("/webhook/{task_type}/{agent_id}/{operation_id}")
async def webhook(task_type: str, agent_id: str, operation_id: str, request: Request):
    payload = await request.json()
    payload["task_type"] = task_type
    payload["operation_id"] = operation_id

    # Route to agent client - handlers fire automatically
    agent = client.agent(agent_id)
    await agent.handle_webhook(
        payload,
        request.headers.get("x-adcp-signature")
    )

    return {"received": True}
```

### Security
Webhook signature verification built-in:

```python
client = ADCPMultiAgentClient(
    agents=agents,
    webhook_secret=os.getenv("WEBHOOK_SECRET")
)
# Signatures verified automatically on handle_webhook()
```

### Debug Mode

Enable debug mode to see full request/response details:

```python
agent_config = AgentConfig(
    id="agent_x",
    agent_uri="https://agent-x.com",
    protocol="mcp",
    debug=True  # Enable debug mode
)

result = await client.agent("agent_x").get_products(brief="Coffee brands")

# Access debug information
if result.debug_info:
    print(f"Duration: {result.debug_info.duration_ms}ms")
    print(f"Request: {result.debug_info.request}")
    print(f"Response: {result.debug_info.response}")
```

Or use the CLI:

```bash
uvx adcp --debug myagent get_products '{"brief":"TV ads"}'
```

### Error Handling

The library provides a comprehensive exception hierarchy with helpful error messages:

```python
from adcp.exceptions import (
    ADCPError,               # Base exception
    ADCPConnectionError,     # Connection failed
    ADCPAuthenticationError, # Auth failed (401, 403)
    ADCPTimeoutError,        # Request timed out
    ADCPProtocolError,       # Invalid response format
    ADCPToolNotFoundError,   # Tool not found
    ADCPWebhookSignatureError  # Invalid webhook signature
)

try:
    result = await client.agent("agent_x").get_products(brief="Coffee")
except ADCPAuthenticationError as e:
    # Exception includes agent context and helpful suggestions
    print(f"Auth failed for {e.agent_id}: {e.message}")
    print(f"Suggestion: {e.suggestion}")
except ADCPTimeoutError as e:
    print(f"Request timed out after {e.timeout}s")
except ADCPConnectionError as e:
    print(f"Connection failed: {e.message}")
    print(f"Agent URI: {e.agent_uri}")
except ADCPError as e:
    # Catch-all for other AdCP errors
    print(f"AdCP error: {e.message}")
```

All exceptions include:
- **Contextual information**: agent ID, URI, and operation details
- **Actionable suggestions**: specific steps to fix common issues
- **Error classification**: proper HTTP status code handling

## Available Tools

All AdCP tools with full type safety:

**Media Buy Lifecycle:**
- `get_products()` - Discover advertising products
- `list_creative_formats()` - Get supported creative formats
- `create_media_buy()` - Create new media buy
- `update_media_buy()` - Update existing media buy
- `sync_creatives()` - Upload/sync creative assets
- `list_creatives()` - List creative assets
- `get_media_buy_delivery()` - Get delivery performance

**Audience & Targeting:**
- `list_authorized_properties()` - Get authorized properties
- `get_signals()` - Get audience signals
- `activate_signal()` - Activate audience signals
- `provide_performance_feedback()` - Send performance feedback

## Property Discovery (AdCP v2.2.0)

Build agent registries by discovering properties agents can sell:

```python
from adcp.discovery import PropertyCrawler, get_property_index

# Crawl agents to discover properties
crawler = PropertyCrawler()
await crawler.crawl_agents([
    {"agent_url": "https://agent-x.com", "protocol": "a2a"},
    {"agent_url": "https://agent-y.com/mcp/", "protocol": "mcp"}
])

index = get_property_index()

# Query 1: Who can sell this property?
matches = index.find_agents_for_property("domain", "cnn.com")

# Query 2: What can this agent sell?
auth = index.get_agent_authorizations("https://agent-x.com")

# Query 3: Find by tags
premium = index.find_agents_by_property_tags(["premium", "ctv"])
```

## CLI Tool

The `adcp` command-line tool provides easy interaction with AdCP agents without writing code.

### Installation

```bash
# Install globally
pip install adcp

# Or use uvx to run without installing
uvx adcp --help
```

### Quick Start

```bash
# Save agent configuration
uvx adcp --save-auth myagent https://agent.example.com mcp

# List tools available on agent
uvx adcp myagent list_tools

# Execute a tool
uvx adcp myagent get_products '{"brief":"TV ads"}'

# Use from stdin
echo '{"brief":"TV ads"}' | uvx adcp myagent get_products

# Use from file
uvx adcp myagent get_products @request.json

# Get JSON output
uvx adcp --json myagent get_products '{"brief":"TV ads"}'

# Enable debug mode
uvx adcp --debug myagent get_products '{"brief":"TV ads"}'
```

### Configuration Management

```bash
# Save agent with authentication
uvx adcp --save-auth myagent https://agent.example.com mcp
# Prompts for optional auth token

# List saved agents
uvx adcp --list-agents

# Remove saved agent
uvx adcp --remove-agent myagent

# Show config file location
uvx adcp --show-config
```

### Direct URL Access

```bash
# Use URL directly without saving
uvx adcp https://agent.example.com/mcp list_tools

# Override protocol
uvx adcp --protocol a2a https://agent.example.com list_tools

# Pass auth token
uvx adcp --auth YOUR_TOKEN https://agent.example.com list_tools
```

### Examples

```bash
# Get products from saved agent
uvx adcp myagent get_products '{"brief":"Coffee brands for digital video"}'

# Create media buy
uvx adcp myagent create_media_buy '{
  "name": "Q4 Campaign",
  "budget": 50000,
  "start_date": "2024-01-01",
  "end_date": "2024-03-31"
}'

# List creative formats with JSON output
uvx adcp --json myagent list_creative_formats | jq '.data'

# Debug connection issues
uvx adcp --debug myagent list_tools
```

### Configuration File

Agent configurations are stored in `~/.adcp/config.json`:

```json
{
  "agents": {
    "myagent": {
      "agent_uri": "https://agent.example.com",
      "protocol": "mcp",
      "auth_token": "optional-token"
    }
  }
}
```

## Environment Configuration

```bash
# .env
WEBHOOK_URL_TEMPLATE="https://myapp.com/webhook/{task_type}/{agent_id}/{operation_id}"
WEBHOOK_SECRET="your-webhook-secret"

ADCP_AGENTS='[
  {
    "id": "agent_x",
    "agent_uri": "https://agent-x.com",
    "protocol": "a2a",
    "auth_token_env": "AGENT_X_TOKEN"
  }
]'
AGENT_X_TOKEN="actual-token-here"
```

```python
# Auto-discover from environment
client = ADCPMultiAgentClient.from_env()
```

## Development

```bash
# Install with dev dependencies
pip install -e ".[dev]"

# Run tests
pytest

# Type checking
mypy src/

# Format code
black src/ tests/
ruff check src/ tests/
```

## Contributing

Contributions welcome! See [CONTRIBUTING.md](CONTRIBUTING.md) for guidelines.

## License

Apache 2.0 License - see [LICENSE](LICENSE) file for details.

## Support

- **Documentation**: [docs.adcontextprotocol.org](https://docs.adcontextprotocol.org)
- **Issues**: [GitHub Issues](https://github.com/adcontextprotocol/adcp-client-python/issues)
- **Protocol Spec**: [AdCP Specification](https://github.com/adcontextprotocol/adcp)
