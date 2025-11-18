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

> **Note**: This client requires Python 3.10 or later and supports both synchronous and asynchronous workflows.

## Quick Start: Test Helpers

The fastest way to get started is using pre-configured test agents with the **`.simple` API**:

```python
from adcp.testing import test_agent

# Zero configuration - just import and call with kwargs!
products = await test_agent.simple.get_products(
    brief='Coffee subscription service for busy professionals'
)

print(f"Found {len(products.products)} products")
```

### Simple vs. Standard API

**Every ADCPClient** includes both API styles via the `.simple` accessor:

**Simple API** (`client.simple.*`) - Recommended for examples/prototyping:
```python
from adcp.testing import test_agent

# Kwargs and direct return - raises on error
products = await test_agent.simple.get_products(brief='Coffee brands')
print(products.products[0].name)
```

**Standard API** (`client.*`) - Recommended for production:
```python
from adcp.testing import test_agent
from adcp import GetProductsRequest

# Explicit request objects and TaskResult wrapper
request = GetProductsRequest(brief='Coffee brands')
result = await test_agent.get_products(request)

if result.success and result.data:
    print(result.data.products[0].name)
else:
    print(f"Error: {result.error}")
```

**When to use which:**
- **Simple API** (`.simple`): Quick testing, documentation, examples, notebooks
- **Standard API**: Production code, complex error handling, webhook workflows

### Available Test Helpers

Pre-configured agents (all include `.simple` accessor):
- **`test_agent`**: MCP test agent with authentication
- **`test_agent_a2a`**: A2A test agent with authentication
- **`test_agent_no_auth`**: MCP test agent without authentication
- **`test_agent_a2a_no_auth`**: A2A test agent without authentication
- **`creative_agent`**: Reference creative agent for preview functionality
- **`test_agent_client`**: Multi-agent client with both protocols

> **Note**: Test agents are rate-limited and for testing/examples only. DO NOT use in production.

See [examples/simple_api_demo.py](examples/simple_api_demo.py) for a complete comparison.

> **Tip**: Import types from the main `adcp` package (e.g., `from adcp import GetProductsRequest`) rather than `adcp.types.generated` for better API stability.

## Quick Start: Distributed Operations

For production use, configure your own agents:

```python
from adcp import ADCPMultiAgentClient, AgentConfig, GetProductsRequest

# Configure agents and handlers (context manager ensures proper cleanup)
async with ADCPMultiAgentClient(
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
) as client:
    # Execute operation - library handles operation IDs, webhook URLs, context management
    agent = client.agent("agent_x")
    request = GetProductsRequest(brief="Coffee brands")
    result = await agent.get_products(request)

    # Check result
    if result.status == "completed":
        # Agent completed synchronously!
        print(f"✅ Sync completion: {len(result.data.products)} products")

    if result.status == "submitted":
        # Agent will send webhook when complete
        print(f"⏳ Async - webhook registered at: {result.submitted.webhook_url}")
# Connections automatically cleaned up here
```

## Features

### Test Helpers

Pre-configured test agents for instant prototyping and testing:

```python
from adcp.testing import (
    test_agent, test_agent_a2a,
    test_agent_no_auth, test_agent_a2a_no_auth,
    creative_agent, test_agent_client, create_test_agent
)
from adcp import GetProductsRequest, PreviewCreativeRequest

# 1. Single agent with authentication (MCP)
result = await test_agent.get_products(
    GetProductsRequest(brief="Coffee brands")
)

# 2. Single agent with authentication (A2A)
result = await test_agent_a2a.get_products(
    GetProductsRequest(brief="Coffee brands")
)

# 3. Single agent WITHOUT authentication (MCP)
# Useful for testing unauthenticated behavior
result = await test_agent_no_auth.get_products(
    GetProductsRequest(brief="Coffee brands")
)

# 4. Single agent WITHOUT authentication (A2A)
result = await test_agent_a2a_no_auth.get_products(
    GetProductsRequest(brief="Coffee brands")
)

# 5. Creative agent (preview functionality, no auth required)
result = await creative_agent.preview_creative(
    PreviewCreativeRequest(
        manifest={"format_id": "banner_300x250", "assets": {...}}
    )
)

# 6. Multi-agent (parallel execution with both protocols)
results = await test_agent_client.get_products(
    GetProductsRequest(brief="Coffee brands")
)

# 7. Custom configuration
from adcp.client import ADCPClient
config = create_test_agent(id="my-test", timeout=60.0)
client = ADCPClient(config)
```

**Use cases:**
- Quick prototyping and experimentation
- Example code and documentation
- Integration testing without mock servers
- Testing authentication behavior (comparing auth vs no-auth results)
- Learning AdCP concepts

**Important:** Test agents are public, rate-limited, and for testing only. Never use in production.

### Full Protocol Support
- **A2A Protocol**: Native support for Agent-to-Agent protocol
- **MCP Protocol**: Native support for Model Context Protocol
- **Auto-detection**: Automatically detect which protocol an agent uses

### Type Safety

Full type hints with Pydantic validation and auto-generated types from the AdCP spec. All commonly-used types are exported from the main `adcp` package for convenience:

```python
from adcp import (
    GetProductsRequest,
    BrandManifest,
    Package,
    CpmFixedRatePricingOption,
    MediaBuyStatus,
)

# All methods require typed request objects
request = GetProductsRequest(brief="Coffee brands", max_results=10)
result = await agent.get_products(request)
# result: TaskResult[GetProductsResponse]

if result.success:
    for product in result.data.products:
        print(product.name, product.pricing_options)  # Full IDE autocomplete!

# Type-safe pricing with discriminators
pricing = CpmFixedRatePricingOption(
    pricing_option_id="cpm_usd",
    pricing_model="cpm",
    is_fixed=True,  # Literal[True] - type checked!
    currency="USD",
    rate=5.0
)

# Type-safe status enums
if media_buy.status == MediaBuyStatus.active:
    print("Media buy is active")
```

**Exported from main package:**
- **Core domain types**: `BrandManifest`, `Creative`, `CreativeManifest`, `MediaBuy`, `Package`
- **Status enums**: `CreativeStatus`, `MediaBuyStatus`, `PackageStatus`, `PricingModel`
- **All 9 pricing options**: `CpcPricingOption`, `CpmFixedRatePricingOption`, `VcpmAuctionPricingOption`, etc.
- **Request/Response types**: All 16 operations with full request/response types

#### Semantic Type Aliases

For discriminated union types (success/error responses), use semantic aliases for clearer code:

```python
from adcp import (
    CreateMediaBuySuccessResponse,  # Clear: this is the success case
    CreateMediaBuyErrorResponse,     # Clear: this is the error case
)

def handle_response(
    response: CreateMediaBuySuccessResponse | CreateMediaBuyErrorResponse
) -> None:
    if isinstance(response, CreateMediaBuySuccessResponse):
        print(f"✅ Media buy created: {response.media_buy_id}")
    else:
        print(f"❌ Errors: {response.errors}")
```

**Available semantic aliases:**
- Response types: `*SuccessResponse` / `*ErrorResponse` (e.g., `CreateMediaBuySuccessResponse`)
- Request variants: `*FormatRequest` / `*ManifestRequest` (e.g., `PreviewCreativeFormatRequest`)
- Preview renders: `PreviewRenderImage` / `PreviewRenderHtml` / `PreviewRenderIframe`
- Activation keys: `PropertyIdActivationKey` / `PropertyTagActivationKey`

See `examples/type_aliases_demo.py` for more examples.

**Import guidelines:**
- ✅ **DO**: Import from main package: `from adcp import GetProductsRequest`
- ✅ **DO**: Use semantic aliases: `from adcp import CreateMediaBuySuccessResponse`
- ⚠️ **AVOID**: Import from internal modules: `from adcp.types._generated import CreateMediaBuyResponse1`

The main package exports provide a stable API while internal generated types may change.

### Multi-Agent Operations
Execute across multiple agents simultaneously:

```python
from adcp import GetProductsRequest

# Parallel execution across all agents
request = GetProductsRequest(brief="Coffee brands")
results = await client.get_products(request)

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

### Resource Management

**Why use async context managers?**
- Ensures HTTP connections are properly closed, preventing resource leaks
- Handles cleanup even when exceptions occur
- Required for production applications with connection pooling
- Prevents issues with async task group cleanup in MCP protocol

The recommended pattern uses async context managers:

```python
from adcp import ADCPClient, AgentConfig, GetProductsRequest

# Recommended: Automatic cleanup with context manager
config = AgentConfig(id="agent_x", agent_uri="https://...", protocol="a2a")
async with ADCPClient(config) as client:
    request = GetProductsRequest(brief="Coffee brands")
    result = await client.get_products(request)
    # Connection automatically closed on exit

# Multi-agent client also supports context managers
async with ADCPMultiAgentClient(agents) as client:
    # Execute across all agents in parallel
    results = await client.get_products(request)
    # All agent connections closed automatically (even if some failed)
```

Manual cleanup is available for special cases (e.g., managing client lifecycle manually):

```python
# Use manual cleanup when you need fine-grained control over lifecycle
client = ADCPClient(config)
try:
    result = await client.get_products(request)
finally:
    await client.close()  # Explicit cleanup
```

**When to use manual cleanup:**
- Managing client lifecycle across multiple functions
- Testing scenarios requiring explicit control
- Integration with frameworks that manage resources differently

In most cases, prefer the context manager pattern.

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

## Publisher Authorization Validation

Verify sales agents are authorized to sell publisher properties via adagents.json:

```python
from adcp import (
    fetch_adagents,
    verify_agent_authorization,
    verify_agent_for_property,
)

# Fetch and parse adagents.json from publisher
adagents_data = await fetch_adagents("publisher.com")

# Verify agent authorization for a property
is_authorized = verify_agent_authorization(
    adagents_data=adagents_data,
    agent_url="https://sales-agent.example.com",
    property_type="website",
    property_identifiers=[{"type": "domain", "value": "publisher.com"}]
)

# Or use convenience wrapper (fetch + verify in one call)
is_authorized = await verify_agent_for_property(
    publisher_domain="publisher.com",
    agent_url="https://sales-agent.example.com",
    property_identifiers=[{"type": "domain", "value": "publisher.com"}],
    property_type="website"
)
```

**Domain Matching Rules:**
- Exact match: `example.com` matches `example.com`
- Common subdomains: `www.example.com` matches `example.com`
- Wildcards: `api.example.com` matches `*.example.com`
- Protocol-agnostic: `http://agent.com` matches `https://agent.com`

**Use Cases:**
- Sales agents verify authorization before accepting media buys
- Publishers test their adagents.json files
- Developer tools build authorization validators

See `examples/adagents_validation.py` for complete examples.

### Authorization Discovery

Discover which publishers have authorized your agent using two approaches:

**1. "Push" Approach** - Ask the agent (recommended, fastest):
```python
from adcp import ADCPClient

async with ADCPClient(agent_config) as client:
    # Single API call to agent
    response = await client.simple.list_authorized_properties()
    print(f"Authorized for: {response.publisher_domains}")
```

**2. "Pull" Approach** - Check publisher adagents.json files (when you need property details):
```python
from adcp import fetch_agent_authorizations

# Check specific publishers (fetches in parallel)
contexts = await fetch_agent_authorizations(
    "https://our-sales-agent.com",
    ["nytimes.com", "wsj.com", "cnn.com"]
)

for domain, ctx in contexts.items():
    print(f"{domain}:")
    print(f"  Property IDs: {ctx.property_ids}")
    print(f"  Tags: {ctx.property_tags}")
```

**When to use which:**
- **Push**: Quick discovery, portfolio overview, high-level authorization check
- **Pull**: Property-level details, specific publisher list, works offline

See `examples/fetch_agent_authorizations.py` for complete examples.

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

### Using Test Agents from CLI

The CLI provides easy access to public test agents without configuration:

```bash
# Use test agent with authentication (MCP)
uvx adcp https://test-agent.adcontextprotocol.org/mcp/ \
  --auth 1v8tAhASaUYYp4odoQ1PnMpdqNaMiTrCRqYo9OJp6IQ \
  get_products '{"brief":"Coffee brands"}'

# Use test agent WITHOUT authentication (MCP)
uvx adcp https://test-agent.adcontextprotocol.org/mcp/ \
  get_products '{"brief":"Coffee brands"}'

# Use test agent with authentication (A2A)
uvx adcp --protocol a2a \
  --auth 1v8tAhASaUYYp4odoQ1PnMpdqNaMiTrCRqYo9OJp6IQ \
  https://test-agent.adcontextprotocol.org \
  get_products '{"brief":"Coffee brands"}'

# Save test agent for easier access
uvx adcp --save-auth test-agent https://test-agent.adcontextprotocol.org/mcp/ mcp
# Enter token when prompted: 1v8tAhASaUYYp4odoQ1PnMpdqNaMiTrCRqYo9OJp6IQ

# Now use saved config
uvx adcp test-agent get_products '{"brief":"Coffee brands"}'

# Use creative agent (no auth required)
uvx adcp https://creative.adcontextprotocol.org/mcp \
  preview_creative @creative_manifest.json
```

**Test Agent Details:**
- **URL (MCP)**: `https://test-agent.adcontextprotocol.org/mcp/`
- **URL (A2A)**: `https://test-agent.adcontextprotocol.org`
- **Auth Token**: `1v8tAhASaUYYp4odoQ1PnMpdqNaMiTrCRqYo9OJp6IQ` (optional, public token)
- **Rate Limited**: For testing only, not for production
- **No Auth Mode**: Omit `--auth` flag to test unauthenticated behavior
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
