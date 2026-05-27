# adcp - Python Client for Ad Context Protocol

[![PyPI version](https://badge.fury.io/py/adcp.svg)](https://badge.fury.io/py/adcp)
[![License](https://img.shields.io/badge/License-Apache%202.0-blue.svg)](https://opensource.org/licenses/Apache-2.0)
[![Python](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)

Official Python SDK for the **Ad Context Protocol (AdCP)**. Build and connect to advertising agents that work synchronously OR asynchronously with the same code.

## Building an AdCP Agent

The fastest path to a working agent: subclass `ADCPHandler`, use response builders, call `serve()`.

```python
from adcp.server import ADCPHandler, serve
from adcp.server.responses import capabilities_response, products_response

class MySeller(ADCPHandler):
    async def get_adcp_capabilities(self, params, context=None):
        return capabilities_response(["media_buy"])

    async def get_products(self, params, context=None):
        return products_response(MY_PRODUCTS)

    # implement create_media_buy, get_media_buys, sync_creatives, etc.

serve(MySeller(), name="my-seller")
```

Validate with storyboards:
```bash
python agent.py &
npx @adcp/client storyboard run http://localhost:3001/mcp media_buy_seller --json
```

| Agent type | Skill | Storyboard | Steps |
|-----------|-------|-----------|-------|
| Seller (publisher, SSP, retail media) | [`skills/build-seller-agent/`](skills/build-seller-agent/SKILL.md) | `media_buy_seller` | 9 |
| Signals (audience data, CDP) | [`skills/build-signals-agent/`](skills/build-signals-agent/SKILL.md) | `signal_owned` | 4 |
| Creative (ad server, CMP) | [`skills/build-creative-agent/`](skills/build-creative-agent/SKILL.md) | `creative_lifecycle` | 6 |

For compliance testing, add a `TestControllerStore` so storyboards can force state transitions:
```python
from adcp.server.test_controller import TestControllerStore
serve(MySeller(), name="my-seller", test_controller=MyStore())
```

Each skill file in [`skills/`](skills/) contains the complete pattern, response shapes, and validation loop for coding agents (Claude, Codex) to generate passing servers.

### Multi-agent discovery manifest

Every HTTP transport (`streamable-http`, `a2a`, `both`) automatically
serves the AdCP multi-agent topology manifest at
`/.well-known/adcp-agents.json`. Buyers, conformance runners, and
tooling fetch this once per origin to discover which agents the host
serves and over which transports — no out-of-band configuration.

```bash
curl http://localhost:3001/.well-known/adcp-agents.json
```

Set `base_url`, `specialisms`, and `description` to populate the
manifest with your public origin and AdCP specialisms:

```python
serve(
    MySeller(),
    name="my-seller",
    transport="both",
    base_url="https://sales.example.com",
    specialisms=["sales-non-guaranteed", "sales-guaranteed"],
    description="Premium publisher inventory.",
)
```

## Connecting to AdCP Agents

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
    brief='Coffee subscription service for busy professionals',
    buying_mode='brief',
)

print(f"Found {len(products.products)} products")
```

### Simple vs. Standard API

**Every ADCPClient** includes both API styles via the `.simple` accessor:

**Simple API** (`client.simple.*`) - Recommended for examples/prototyping:
```python
from adcp.testing import test_agent

# Kwargs and direct return - raises on error
products = await test_agent.simple.get_products(brief='Coffee brands', buying_mode='brief')
print(products.products[0].name)
```

**Standard API** (`client.*`) - Recommended for production:
```python
from adcp.testing import test_agent
from adcp import GetProductsRequest

# Explicit request objects and TaskResult wrapper
request = GetProductsRequest(brief='Coffee brands', buying_mode='brief')
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
    request = GetProductsRequest(brief="Coffee brands", buying_mode="brief")
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

## AdCP version support

The 5.x line targets AdCP 3.0 stable. v3.1 support lands in SDK 6.0 against
the 3.1 stable spec — there is no opt-in preview surface in 5.x. If you talk
to a v3.1+ agent from 5.x, the SDK parses the response through v3.0 types
(unknown fields are preserved on the model but not surfaced as typed
attributes) and schema validation is skipped for that version. Track
[#741](https://github.com/adcontextprotocol/adcp-client-python/issues/741)
for 6.0 progress.

## Documentation

- **[API Reference](https://adcontextprotocol.github.io/adcp-client-python/)** - Complete API documentation with type signatures and examples
- **[Protocol Spec](https://github.com/adcontextprotocol/adcp)** - Ad Context Protocol specification
- **[Handler authoring](docs/handler-authoring.md)** - Building an AdCP-compliant agent on `adcp.server`
- **[Multi-tenant contract](docs/multi-tenant-contract.md)** - Scope invariants every multi-tenant agent must satisfy
- **[Examples](examples/)** - Code examples and usage patterns

The API reference documentation is automatically generated from the code and includes:
- Full type signatures for all methods
- Field descriptions from JSON Schema
- Method documentation with examples
- Searchable interface

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
    GetProductsRequest(brief="Coffee brands", buying_mode="brief")
)

# 2. Single agent with authentication (A2A)
result = await test_agent_a2a.get_products(
    GetProductsRequest(brief="Coffee brands", buying_mode="brief")
)

# 3. Single agent WITHOUT authentication (MCP)
# Useful for testing unauthenticated behavior
result = await test_agent_no_auth.get_products(
    GetProductsRequest(brief="Coffee brands", buying_mode="brief")
)

# 4. Single agent WITHOUT authentication (A2A)
result = await test_agent_a2a_no_auth.get_products(
    GetProductsRequest(brief="Coffee brands", buying_mode="brief")
)

# 5. Creative agent (preview functionality, no auth required)
result = await creative_agent.preview_creative(
    PreviewCreativeRequest(
        manifest={"format_id": "banner_300x250", "assets": {...}}
    )
)

# 6. Multi-agent (parallel execution with both protocols)
results = await test_agent_client.get_products(
    GetProductsRequest(brief="Coffee brands", buying_mode="brief")
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
    BrandReference,
    Package,
    CpmFixedRatePricingOption,
    MediaBuyStatus,
)

# All methods require typed request objects
request = GetProductsRequest(brief="Coffee brands", buying_mode="brief", max_results=10)
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
- **Core domain types**: `BrandReference`, `Creative`, `CreativeManifest`, `MediaBuy`, `Package`, `PackageRequest`, `TargetingOverlay`
- **AdCP status enums**: `CreativeStatus`, `DeliveryStatus`, `MediaBuyStatus`, `PricingModel`
- **All 9 pricing options**: `CpcPricingOption`, `CpmFixedRatePricingOption`, `VcpmAuctionPricingOption`, etc.
- **Request/Response types**: All 16 operations with full request/response types

For types not on the top-level surface, import from `adcp.types` (e.g., `from adcp.types import AssetStatus`). If a type you need isn't in `adcp.types`, open an issue — we'll add an alias. The `adcp.types.generated_poc.*` modules are internal; class names and module paths shift on every schema regeneration and are not a supported API.

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
- ⚠️ **AVOID**: Import from `adcp.types.generated_poc.*` — paths and class names (including numbered `Assets*` variants) change on every schema regeneration.

The main package exports provide a stable API while internal generated types may change.

### Multi-Agent Operations
Execute across multiple agents simultaneously:

```python
from adcp import GetProductsRequest

# Parallel execution across all agents
request = GetProductsRequest(brief="Coffee brands", buying_mode="brief")
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

### Signed webhooks (AdCP 3.0): receiver quickstart

AdCP 3.0 webhooks are signed under the RFC 9421 profile
(`adcp/webhook-signing/v1`) and carry a required `idempotency_key` for
at-least-once dedup. The `WebhookReceiver` packages verify + dedupe + parse
into one call so you don't have to re-derive the normative checklist:

```python
from flask import Flask, request, Response
from adcp.server.idempotency import MemoryBackend, WebhookDedupStore
from adcp.signing import StaticJwksResolver
from adcp.webhooks import (
    WebhookReceiver,
    WebhookReceiverConfig,
    WebhookVerifyOptions,
)

# One resolver per publisher. In production, wire an async JWKS fetcher
# pointed at the publisher's `adagents.json`.
jwks = StaticJwksResolver(publisher_jwks_dict)

receiver = WebhookReceiver(
    config=WebhookReceiverConfig(
        verify_options=WebhookVerifyOptions(jwks_resolver=jwks),
        dedup=WebhookDedupStore(MemoryBackend(), ttl_seconds=86400),
    ),
)

app = Flask(__name__)

@app.post("/webhooks/adcp")
async def hook():
    outcome = await receiver.receive(
        method=request.method, url=request.url,
        headers=dict(request.headers), body=request.get_data(),
    )
    if outcome.rejected:
        return Response(status=401, headers=outcome.response_headers)
    # Spec: MUST return 2xx on duplicates so the at-least-once sender stops
    # retrying. A duplicate is a no-op, not an error.
    if outcome.duplicate:
        return Response(status=200)
    process(outcome.payload)  # typed McpWebhookPayload
    return Response(status=200)
```

**Legacy HMAC-SHA256 fallback** (3.x only, removed in 4.0). The shortcut
constructor covers the "one publisher, one shared secret" case:

```python
from adcp.webhooks import LegacyHmacFallback

config = WebhookReceiverConfig(
    verify_options=WebhookVerifyOptions(jwks_resolver=jwks),
    dedup=WebhookDedupStore(MemoryBackend(), ttl_seconds=86400),
    legacy_hmac=LegacyHmacFallback.from_shared_secret(
        secret=os.environ["WEBHOOK_SHARED_SECRET"].encode(),
        sender_identity="publisher-buyerco",
    ),
)
```

By default the fallback only fires when no 9421 headers are present — this
prevents a MITM from stripping a valid 9421 signature and substituting a
forged HMAC one.

### Signed webhooks: sender quickstart

```python
from adcp.webhooks import WebhookSender

# One sender per private key; reuses a pooled httpx client under the hood.
sender = WebhookSender.from_jwk(webhook_signing_jwk_with_private_d)

async with sender:
    result = await sender.send_mcp(
        url="https://buyer.example.com/webhooks/adcp/create_media_buy/op_abc",
        task_id="task_456",
        task_type="create_media_buy",
        status="completed",
        result={"media_buy_id": "mb_1"},
    )

    if not result.ok:
        # resend() replays the exact same bytes under a fresh signature —
        # preserves idempotency_key AND every other payload field, so the
        # receiver dedupes against the original event.
        retry = await sender.resend(result)
```

`WebhookSender` handles payload construction, byte-exact JSON serialization,
9421 signing, and the httpx POST in one call. `send_raw(...)` is an escape
hatch for custom payload shapes; dedicated methods exist for every webhook
kind (`send_revocation_notification`, `send_artifact_webhook`,
`send_collection_list_changed`, `send_property_list_changed`,
`send_wholesale_feed`).

Validate buyer-provided webhook URLs before storing durable subscriptions:

```python
from adcp import (
    WebhookChallengeError,
    WebhookDestinationPolicy,
    WebhookSender,
    challenge_webhook_destination,
)

sender = WebhookSender.from_jwk(webhook_signing_jwk_with_private_d)

try:
    # Call only for new or changed active durable subscriptions. Persist
    # active=False configs without challenging; challenge before activation.
    auth_kwargs = (
        {"authentication": config.authentication}
        if config.authentication
        else {"sender": sender}
    )
    await challenge_webhook_destination(
        url=config.url,
        account_id=account_id,
        subscriber_id=config.subscriber_id,
        **auth_kwargs,
        field="accounts[0].notification_configs[0].url",
        policy=WebhookDestinationPolicy.production(),
    )
except WebhookChallengeError as exc:
    return {"errors": [exc.to_error()]}
```

Use `WebhookDestinationPolicy.local_development()` only for local tests that
need `http://localhost` or private-network destinations. Production validation
requires HTTPS and rejects loopback, private, link-local, reserved, and cloud
metadata destinations. The challenge helper uses the SDK-managed pinned
transport and fails unless the receiver echoes the challenge in a JSON
`challenge` or `token` field. For omitted `authentication`, pass an RFC 9421
`WebhookSender.from_jwk(...)` without `client=`. Bearer/HMAC durable configs
must pass `authentication=config.authentication`; custom egress clients,
proxies, and mTLS transports are for normal delivery paths, not registration
challenges.

Wholesale feed notifications use stable types from `adcp` / `adcp.types`:

```python
from adcp import NotificationConfig, WholesaleFeedEvent, WholesaleFeedWebhook
from adcp.webhooks import WebhookSender

if "product.updated" in subscription.event_types:
    await sender.send_wholesale_feed_to_subscription(
        subscription=subscription,
        account_id=account_id,
        notification_type="product.updated",
        wholesale_feed_version=feed_version,
        cache_scope="public",
        event=event,
    )
```

The webhook-signing JWK MUST be published in your `adagents.json` with
`adcp_use: "webhook-signing"` — distinct from your `request-signing` key so
neither signature can be replayed as the other. `WebhookSender.from_jwk`
refuses to construct from a JWK with the wrong `adcp_use` to fail fast at
setup rather than at receiver verification.

### Debug Mode

Enable debug mode to see full request/response details:

```python
agent_config = AgentConfig(
    id="agent_x",
    agent_uri="https://agent-x.com",
    protocol="mcp",
    debug=True  # Enable debug mode
)

result = await client.agent("agent_x").get_products(brief="Coffee brands", buying_mode="brief")

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
    request = GetProductsRequest(brief="Coffee brands", buying_mode="brief")
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
    result = await client.agent("agent_x").get_products(brief="Coffee", buying_mode="brief")
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

### Idempotency and retries

AdCP 3.0 requires an `idempotency_key` on every mutating request (`create_media_buy`, `sync_creatives`, and 26 others). The client handles this for you — you pass a key (or let the SDK generate one) and get back the key the seller cached under, plus a `replayed` flag indicating whether the seller served a cached response:

```python
import uuid
from adcp import CreateMediaBuyRequest

# Pass a fresh UUID v4 on each new logical operation — the request schema
# requires idempotency_key at construction time.
request = CreateMediaBuyRequest(
    idempotency_key=uuid.uuid4().hex,
    account=..., brand=..., start_time=..., end_time=..., packages=[...]
)
result = await client.create_media_buy(request)
print(result.idempotency_key)    # The key the SDK sent
print(result.replayed)           # True if the seller returned a cached response
```

**Retrying the same logical operation** — wrap the retry loop in `use_idempotency_key` so every attempt sends the same key. Otherwise each retry gets a new UUID and defeats the whole point.

```python
stored = uuid.uuid4().hex
for attempt in range(3):
    try:
        with client.use_idempotency_key(stored):
            result = await client.create_media_buy(request)
        break
    except TimeoutError:
        continue
```

**Bring your own key** when you persist keys across process restarts (e.g., storing alongside a campaign row in your DB):

```python
with client.use_idempotency_key(campaign.stored_key):
    result = await client.create_media_buy(request)
```

The pinned key is scoped to *this* client instance — a sibling `ADCPClient` running inside the same `with` block generates a fresh key (per AdCP §2315: keys must be unique per `(seller, request)` pair to prevent cross-seller correlation). The pinned key is also single-use within the scope: if you `asyncio.gather` two sibling calls inside the block, only the first gets the pinned key, the rest get fresh UUIDs — preventing accidental payload drift.

**Typed errors** for the two idempotency-specific failure modes:

```python
from adcp import IdempotencyConflictError, IdempotencyExpiredError

try:
    await client.create_media_buy(request)
except IdempotencyConflictError:
    # Same key, different payload. Either mint a fresh uuid.uuid4() or resend original.
    ...
except IdempotencyExpiredError:
    # Replay window closed; reconcile via a read before resubmitting with a new key.
    ...
```

**Strict mode** refuses mutating calls against sellers that don't declare `adcp.idempotency.replay_ttl_seconds` in capabilities:

```python
client = ADCPClient(agent, strict_idempotency=True)  # default: False
# First mutating call fetches capabilities and raises IdempotencyUnsupportedError
# if the seller is silent. Set False to opt-out; you then own reconciliation.
```

**Security note on logs.** The SDK redacts `idempotency_key` in its own debug captures, but the underlying `httpx`/`httpcore` loggers log full request bodies at `DEBUG`. If you enable `logging.basicConfig(level=logging.DEBUG)` in production, raise those two loggers back to `INFO` — otherwise full keys end up in logs during the seller's replay TTL window and become a retry-pattern oracle for anyone who can read them.

### Building a seller: idempotency middleware

If you're building an AdCP seller, the companion middleware handles the `(principal, key, canonical-hash)` bookkeeping so you don't hand-roll it per tool handler. Drop `@idempotency.wrap` above each mutating handler and declare your replay window in capabilities:

```python
from adcp.server import ADCPHandler, IdempotencyStore, MemoryBackend, serve
from adcp.server.responses import capabilities_response

idempotency = IdempotencyStore(
    backend=MemoryBackend(),  # PgBackend with transactional commit is a follow-up
    ttl_seconds=86400,        # 24h, spec-recommended floor
)

class MySeller(ADCPHandler):
    async def get_adcp_capabilities(self, params, context=None):
        return capabilities_response(
            ["media_buy"],
            idempotency=idempotency.capability(),
        )

    @idempotency.wrap
    async def create_media_buy(self, params, context=None):
        # Same key + canonical-equivalent payload → this body is skipped,
        # the cached response is returned. Same key + different payload →
        # IdempotencyConflictError raised before this runs, which the
        # framework translates to IDEMPOTENCY_CONFLICT on the wire.
        return my_business_logic(params)

serve(MySeller(), name="my-seller")
```

**What the middleware does for you:**

- Extracts `idempotency_key` from `params`, scopes lookups by `context.caller_identity` (per-principal — a security requirement from AdCP §2315)
- Hashes the payload with RFC 8785 JCS + SHA-256, stripping the spec's closed exclusion list (`idempotency_key`, `context`, `governance_context`, `push_notification_config.authentication.credentials`)
- On cache hit with matching hash: returns the cached response verbatim, skips your handler (deep-copied so caller mutation can't poison future replays)
- On cache hit with different hash: raises `IdempotencyConflictError`, which the framework surfaces as `IDEMPOTENCY_CONFLICT` on both MCP (`is_error=true` + text) and A2A (failed task with `adcp_error` DataPart)
- On cache miss: runs your handler, then commits the response

**Backends:** `MemoryBackend` ships now (tests, single-process agents). `PgBackend` is scaffolded — it raises `NotImplementedError` with a pointer to the follow-up issue. For production use across multiple workers, implement your own `IdempotencyBackend` subclass against Redis, Postgres, etc.

**Atomicity caveat:** `MemoryBackend` commits the cache entry AFTER your handler returns, so a crash between `handler success` and `cache commit` causes the retry to re-execute. `PgBackend` (follow-up) will commit the cache row in the same transaction as your business writes. Read the module docstring at `adcp.server.idempotency` before shipping this against a production database.

**How caller identity gets populated.** The middleware scopes its cache by `(caller_identity, idempotency_key)` — same key from two buyers must hit different cache slots, and a buyer's retry must replay only against its own prior call. `caller_identity` comes from `ToolContext`, which the transport layer builds per request:

- **A2A** — the framework derives `caller_identity` from `ServerCallContext.user.user_name` when the user is authenticated. Wire your [a2a-sdk auth middleware](https://a2aproject.github.io/) (bearer tokens, mTLS, OAuth) and `@idempotency.wrap` works automatically. Unauthenticated requests → no identity → dedup is skipped (fail-closed, with a one-time `UserWarning` so you notice).

- **MCP** — FastMCP exposes a session `client_id` but not an authenticated principal. The SDK does NOT auto-populate `caller_identity` for MCP tools today. If you're serving via MCP, wire your own FastMCP auth middleware and populate `ToolContext.caller_identity` before the idempotency middleware runs — either by overriding `adcp.server.mcp_tools.create_tool_caller` or by wrapping your handlers directly. Without this, `@idempotency.wrap` is a no-op on MCP (you'll get the one-time warning above).

**Principal contract.** `caller_identity` MUST be a stable, globally-unique identifier per tenant — an opaque buyer ID, not an email or display name. Email reuse after account deletion would cause cross-principal cache collisions. The value is logged at DEBUG (prefix-truncated) and keyed on in the cache; treat it as you would any user-scoping identifier.

### AdCP 3.0.0-rc.4 migration

**`plan.budget.authority_level` removed.** The single enum (`agent_full` / `agent_limited` / `human_required`) is replaced by two orthogonal fields on `plan.budget`, plus a new top-level flag on `plan`:

| Old (removed) | New |
|---|---|
| `budget.authority_level: agent_full` | `budget.reallocation_unlimited: true` |
| `budget.authority_level: agent_limited` | `budget.reallocation_threshold: <amount>` (in `budget.currency`) |
| `budget.authority_level: human_required` | Set `plan.human_review_required: true` **and** `budget.reallocation_threshold: 0` |

`reallocation_threshold` and `reallocation_unlimited` are mutually exclusive — pick one. `plan.human_review_required` is a separate field governing decisions that affect data subjects (targeting, creative, delivery) under GDPR Art 22 / EU AI Act Annex III; set it independently from the budget reallocation autonomy.

```python
# Before (rc.3 and earlier)
plan = SyncPlansRequest(plans=[{
    "plan_id": "...",
    "budget": {"total": 100000, "currency": "USD", "authority_level": "agent_limited"},
}])

# After (rc.4+)
plan = SyncPlansRequest(plans=[{
    "plan_id": "...",
    "budget": {
        "total": 100000,
        "currency": "USD",
        "reallocation_threshold": 5000,  # agent may reallocate up to $5K per change
    },
    "human_review_required": False,  # defaults to False; set True for GDPR Art 22 gating
}])
```

Any hand-coded `plan.budget` payload using `authority_level` will fail Pydantic validation against the rc.4 schema with `extra fields not permitted`. The SDK itself has no code references to the old enum; downstream consumers need to update their payloads.

**`update_rights` task added.** Buyers can now modify an existing rights acquisition without re-acquiring. See `client.update_rights(request)` or the MCP/A2A `update_rights` tool.

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

**Creative Management:**
- `preview_creative()` - Preview creative before building
- `build_creative()` - Generate production-ready creative assets

**Discovery & Accounts:**
- `get_adcp_capabilities()` - Discover agent capabilities and authorized publishers
- `list_accounts()` - List billing accounts

**Audience & Targeting:**
- `get_signals()` - Get audience signals
- `activate_signal()` - Activate audience signals
- `provide_performance_feedback()` - Send performance feedback

## Workflow Examples

### Complete Media Buy Workflow

A typical media buy workflow involves discovering products, creating the buy, and managing creatives:

```python
from adcp import ADCPClient, AgentConfig, GetProductsRequest, CreateMediaBuyRequest
from adcp import BrandReference, PublisherPropertiesAll

# 1. Connect to agent
config = AgentConfig(id="sales_agent", agent_uri="https://...", protocol="mcp")
async with ADCPClient(config) as client:

    # 2. Discover available products
    products_result = await client.get_products(
        GetProductsRequest(brief="Premium video inventory for coffee brand", buying_mode="brief")
    )

    if products_result.success:
        product = products_result.data.products[0]
        print(f"Found product: {product.name}")

    # 3. Create media buy reservation
    media_buy_result = await client.create_media_buy(
        CreateMediaBuyRequest(
            brand=BrandReference(domain="coffeeco.com"),
            packages=[{
                "package_id": product.packages[0].package_id,
                "quantity": 1000000,  # impressions
            }],
            publisher_properties=PublisherPropertiesAll(
                selection_type="all",  # Target all authorized properties
            ),
        )
    )

    if media_buy_result.success:
        media_buy_id = media_buy_result.data.media_buy_id
        print(f"✅ Media buy created: {media_buy_id}")

    # 4. Update media buy if needed
    from adcp import UpdateMediaBuyPackagesRequest

    update_result = await client.update_media_buy(
        UpdateMediaBuyPackagesRequest(
            media_buy_id=media_buy_id,
            packages=[{
                "package_id": product.packages[0].package_id,
                "quantity": 1500000  # Increase budget
            }]
        )
    )

    if update_result.success:
        print("✅ Media buy updated")
```

### Complete Creative Workflow

Build and deliver production-ready creatives:

```python
from adcp import ADCPClient, AgentConfig
from adcp import PreviewCreativeFormatRequest, BuildCreativeRequest
from adcp import CreativeManifest, PlatformDeployment

# 1. Connect to creative agent
config = AgentConfig(id="creative_agent", agent_uri="https://...", protocol="mcp")
async with ADCPClient(config) as client:

    # 2. List available formats
    formats_result = await client.list_creative_formats()

    if formats_result.success:
        format_id = formats_result.data.formats[0].format_id
        print(f"Using format: {format_id.id}")

    # 3. Preview creative (test before building)
    preview_result = await client.preview_creative(
        PreviewCreativeFormatRequest(
            target_format_id=format_id.id,
            inputs={
                "headline": "Fresh Coffee Daily",
                "cta": "Order Now"
            },
            output_format="url"  # Get preview URL
        )
    )

    if preview_result.success:
        preview_url = preview_result.data.renders[0].url
        print(f"Preview at: {preview_url}")

    # 4. Build production creative
    build_result = await client.build_creative(
        BuildCreativeRequest(
            manifest=CreativeManifest(
                format_id=format_id,
                brand_url="https://coffeeco.com",
                # ... creative content
            ),
            target_format_id=format_id.id,
            deployment=PlatformDeployment(
                type="platform",
                platform_id="google_admanager"
            )
        )
    )

    if build_result.success:
        vast_url = build_result.data.assets[0].url
        print(f"✅ Creative ready: {vast_url}")
```

### Integrated Workflow: Media Buy + Creatives

Combine both workflows for a complete campaign setup:

```python
from adcp import ADCPMultiAgentClient, AgentConfig, BrandReference, PublisherPropertiesAll
from adcp import BuildCreativeRequest, CreateMediaBuyRequest

# Connect to both sales and creative agents
async with ADCPMultiAgentClient(
    agents=[
        AgentConfig(id="sales", agent_uri="https://sales-agent.com", protocol="mcp"),
        AgentConfig(id="creative", agent_uri="https://creative-agent.com", protocol="mcp"),
    ]
) as client:

    # 1. Get products from sales agent
    sales_agent = client.agent("sales")
    products = await sales_agent.simple.get_products(
        brief="Premium video inventory",
        buying_mode="brief",
    )

    # 2. Get creative formats from creative agent
    creative_agent = client.agent("creative")
    formats = await creative_agent.simple.list_creative_formats()

    # 3. Build creative asset
    creative_result = await creative_agent.build_creative(
        BuildCreativeRequest(
            manifest=creative_manifest,
            target_format_id=formats.formats[0].format_id.id,
        )
    )

    # 4. Create media buy with creative
    media_buy_result = await sales_agent.create_media_buy(
        CreateMediaBuyRequest(
            brand=BrandReference(domain="coffeeco.com"),
            packages=[{"package_id": products.products[0].packages[0].package_id}],
            publisher_properties=PublisherPropertiesAll(selection_type="all"),
            creative_urls=[creative_result.data.assets[0].url],
        )
    )

    print(f"✅ Campaign live: {media_buy_result.data.media_buy_id}")
```

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
from adcp import ADCPClient, GetAdcpCapabilitiesRequest

async with ADCPClient(agent_config) as client:
    # Single API call to agent
    result = await client.get_adcp_capabilities(GetAdcpCapabilitiesRequest())
    if result.success and result.data.media_buy:
        portfolio = result.data.media_buy.portfolio
        print(f"Authorized for: {portfolio.publisher_domains}")
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

## Request Signing (AdCP 3.0 optional, 4.0 required)

AdCP defines an optional transport-layer request-signing profile based on
[RFC 9421 HTTP Message Signatures](https://datatracker.ietf.org/doc/rfc9421/).
A valid signature proves the request came from the agent whose key signed it.
See the [spec profile](https://adcontextprotocol.org/docs/building/implementation/security#signed-requests-transport-layer)
and the [conformance vectors](https://adcontextprotocol.org/test-vectors/request-signing/).

### Generate a keypair

```bash
python -m adcp.signing.keygen --alg ed25519 --out signing-key.pem
# prints the JWK to stdout — publish it at your agent's jwks_uri
```

ES256 is also supported: `--alg es256`. Ed25519 is the recommended default.

### Sign an outgoing request

```python
from cryptography.hazmat.primitives import serialization
from adcp.signing import sign_request

private_key = serialization.load_pem_private_key(
    open("signing-key.pem", "rb").read(), password=None
)

signed = sign_request(
    method="POST",
    url="https://seller.example.com/adcp/create_media_buy",
    headers={"Content-Type": "application/json"},
    body=body,
    private_key=private_key,
    key_id="adcp-ed25519-20260418",
    alg="ed25519",
    cover_content_digest=True,  # required by sellers that set covers_content_digest="required"
)
httpx.post(url, content=body, headers={**headers, **signed.as_dict()})
```

### Auto-sign on `ADCPClient`

The high-level client wires the signing event hook for you when you pass a `SigningConfig`:

```python
from adcp.client import ADCPClient
from adcp.signing import SigningConfig, load_private_key_pem

signing = SigningConfig(
    private_key=load_private_key_pem(open("signing-key.pem", "rb").read()),
    key_id="my-agent-2026",
)

client = ADCPClient(agent_config, signing=signing)
# Outbound calls are signed automatically per the seller's request_signing capability.
```

### Auto-sign on raw httpx (no ADCPClient)

For adapters that integrate against a seller via raw `httpx`, install the same hook on your own client:

```python
import httpx
from adcp.signing import SigningConfig, install_signing_event_hook, signing_operation

client = httpx.AsyncClient()
install_signing_event_hook(
    client,
    signing=signing,
    seller_capability=seller_caps.request_signing,
)

async with client:
    with signing_operation("create_media_buy"):
        resp = await client.post("https://seller.example.com/mcp", json=payload)
```

### Verify incoming requests (FastAPI)

```python
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from adcp.signing import (
    CachingJwksResolver, SignatureVerificationError,
    VerifierCapability, VerifyOptions,
    unauthorized_response_headers, verify_starlette_request,
)

jwks = CachingJwksResolver("https://buyer.example.com/.well-known/jwks.json")
capability = VerifierCapability(
    covers_content_digest="either",
    required_for=frozenset({"create_media_buy"}),
)

@app.post("/adcp/create_media_buy")
async def create_media_buy(request: Request):
    options = VerifyOptions(
        now=time.time(),
        capability=capability,
        operation="create_media_buy",
        jwks_resolver=jwks,
    )
    # `replay_store` defaults to a fresh InMemoryReplayStore when omitted.
    # Wire an explicit shared store (PgReplayStore via [pg] extra, or your
    # own ReplayStore Protocol implementation) for multi-replica deployments.
    try:
        signer = await verify_starlette_request(request, options=options)
    except SignatureVerificationError as exc:
        return JSONResponse(
            {"error": exc.code},
            status_code=401,
            headers=unauthorized_response_headers(exc),
        )
    # signer.key_id is the verified caller's key identity
    ...
```

Flask has an equivalent synchronous helper `verify_flask_request`.

### Migration & rollout

Rolling signing out against an existing integration is a staged exercise — bootstrap, then advance each operation through `supported_for` → `warn_for` → `required_for`. See [`docs/request-signing-migration.md`](docs/request-signing-migration.md) for the full walkthrough including key rotation, common pitfalls, and a pre-enforcement checklist.

### Conformance

The verifier passes all 28 AdCP request-signing conformance vectors (8 positive,
20 negative). Run them against your signer or verifier:

```bash
pytest tests/conformance/signing/
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
# Install dev dependencies and git hooks (requires uv)
make bootstrap

# Run tests
make test

# Type checking: source package plus adopter-facing type fixtures
make typecheck-all

# Format code
make format
make lint
```

## Contributing

Contributions welcome! See [CONTRIBUTING.md](CONTRIBUTING.md) for guidelines. All contributors must agree to the [AgenticAdvertising.Org IPR Policy](https://github.com/adcontextprotocol/adcp/blob/main/IPR_POLICY.md) — the bot prompts new contributors on their first PR and a single signature covers all AAO repositories.

## License

Apache 2.0 License - see [LICENSE](LICENSE) file for details.

## Support

- **API Reference**: [adcontextprotocol.github.io/adcp-client-python](https://adcontextprotocol.github.io/adcp-client-python/)
- **Protocol Documentation**: [docs.adcontextprotocol.org](https://docs.adcontextprotocol.org)
- **Issues**: [GitHub Issues](https://github.com/adcontextprotocol/adcp-client-python/issues)
- **Protocol Spec**: [AdCP Specification](https://github.com/adcontextprotocol/adcp)
