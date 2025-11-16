# Authorization Discovery

This document explains how to discover which publishers have authorized your agent.

## Two Approaches

### Approach 1: "Push" - Ask the Agent (RECOMMENDED)

The agent has a `list_authorized_properties` endpoint that tells you which publisher domains it represents. This is the fastest and most efficient approach.

```python
from adcp import ADCPClient, AgentConfig, Protocol

# Configure the agent
agent_config = AgentConfig(
    id="sales_agent",
    agent_uri="https://our-sales-agent.com",
    protocol=Protocol.A2A,
)

async with ADCPClient(agent_config) as client:
    # Ask the agent what it's authorized for
    response = await client.simple.list_authorized_properties()

    print(f"Agent represents {len(response.publisher_domains)} publishers:")
    for domain in response.publisher_domains:
        print(f"  • {domain}")
```

**Pros:**
- ✅ Fast - single API call
- ✅ Efficient - no need to fetch multiple files
- ✅ Complete - agent knows all publishers it represents

**Cons:**
- ❌ Doesn't provide property-level details (property IDs, tags)
- ❌ Requires agent to be running and accessible

**When to use:** When you need to quickly discover which publishers an agent represents.

### Approach 2: "Pull" - Check Publisher adagents.json Files

Fetch `adagents.json` files from publishers' `.well-known` directories and check if they authorize your agent.

```python
from adcp import fetch_agent_authorizations

# Check specific publishers
contexts = await fetch_agent_authorizations(
    "https://our-sales-agent.com",
    ["nytimes.com", "wsj.com", "cnn.com"]
)

for domain, ctx in contexts.items():
    print(f"{domain}:")
    print(f"  Property IDs: {ctx.property_ids}")
    print(f"  Tags: {ctx.property_tags}")
```

**Pros:**
- ✅ Provides property-level details (IDs, tags, full properties)
- ✅ Works even if agent is offline
- ✅ Fetches all publishers in parallel for performance

**Cons:**
- ❌ Slower - requires fetching multiple files
- ❌ Only checks publishers you specify
- ❌ Requires publishers to have `.well-known/adagents.json`

**When to use:** When you:
- Have a specific list of publishers to check
- Need property-level details (IDs, tags)
- Want to verify authorization without contacting the agent

## Complete Example

See `examples/fetch_agent_authorizations.py` for a working example demonstrating both approaches.

## API Reference

### `fetch_agent_authorizations(agent_url, publisher_domains, timeout=10.0, client=None)`

Fetch authorization contexts by checking publisher `adagents.json` files.

**Parameters:**
- `agent_url` (str): URL of your sales agent
- `publisher_domains` (list[str]): Publisher domains to check
- `timeout` (float, optional): Request timeout in seconds. Default: 10.0
- `client` (httpx.AsyncClient, optional): HTTP client for connection pooling

**Returns:**
- `dict[str, AuthorizationContext]`: Mapping of domain to authorization context

**Example:**
```python
import httpx
from adcp import fetch_agent_authorizations

# With connection pooling for better performance
async with httpx.AsyncClient() as client:
    contexts = await fetch_agent_authorizations(
        "https://our-agent.com",
        ["nytimes.com", "wsj.com"],
        client=client
    )
```

### `AuthorizationContext`

Contains authorization information for a publisher.

**Attributes:**
- `property_ids` (list[str]): Property IDs the agent can access
- `property_tags` (list[str]): Unique tags from all properties
- `raw_properties` (list[dict]): Complete property data

**Example:**
```python
ctx = contexts["nytimes.com"]
print(f"Property IDs: {ctx.property_ids}")
print(f"Tags: {ctx.property_tags}")
print(f"Full data: {ctx.raw_properties}")
```

### `ADCPClient.list_authorized_properties(request)`

Call the agent's `list_authorized_properties` endpoint.

**Parameters:**
- `request` (ListAuthorizedPropertiesRequest): Request with optional publisher domain filters

**Returns:**
- `TaskResult[ListAuthorizedPropertiesResponse]`: Response with publisher domains

**Simple API:**
```python
# Using the simple API (no request objects)
response = await client.simple.list_authorized_properties()
print(response.publisher_domains)
```

**Standard API:**
```python
# Using the standard API (full control)
from adcp import ListAuthorizedPropertiesRequest

request = ListAuthorizedPropertiesRequest(
    publisher_domains=["nytimes.com", "wsj.com"]
)
result = await client.list_authorized_properties(request)
if result.success:
    print(result.data.publisher_domains)
```

## Best Practices

1. **Start with Approach 1** - Use `list_authorized_properties` to quickly discover which publishers an agent represents.

2. **Use Approach 2 for details** - Fetch `adagents.json` files when you need property-level information.

3. **Use connection pooling** - When fetching multiple `adagents.json` files, pass a shared `httpx.AsyncClient` for better performance:
   ```python
   async with httpx.AsyncClient() as client:
       contexts = await fetch_agent_authorizations(
           agent_url, publisher_domains, client=client
       )
   ```

4. **Handle missing authorizations gracefully** - `fetch_agent_authorizations` silently skips publishers without `adagents.json` or that don't authorize your agent.

## Related

- Issue #53: https://github.com/adcontextprotocol/adcp-client-python/issues/53
- AdCP Specification: https://adcontextprotocol.org/
- adagents.json format: https://adcontextprotocol.org/adagents
