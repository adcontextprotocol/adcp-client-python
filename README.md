# adcp - Python Client for Ad Context Protocol

[![PyPI version](https://badge.fury.io/py/adcp.svg)](https://badge.fury.io/py/adcp)
[![License](https://img.shields.io/badge/License-Apache%202.0-blue.svg)](https://opensource.org/licenses/Apache-2.0)
[![Python](https://img.shields.io/badge/python-3.9+-blue.svg)](https://www.python.org/downloads/)

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
