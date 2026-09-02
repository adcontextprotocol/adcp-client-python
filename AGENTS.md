# ADCP Python SDK - Agent Reference

## PR Title Hygiene

Use concrete conventional-commits PR titles (`fix(scope): summary`, `docs: summary`, `feat(scope): summary`). Do not prefix titles with the tool or model that authored the PR, such as `[codex]`, `[claude]`, `[cursor]`, or similar ownership tags. Put authoring-tool context in the PR body or labels when it matters, not in the review-facing title.

## Server Handler Methods

Override these in your `ADCPHandler` subclass. Unimplemented methods return `not_supported()`.

| Method | Domain | Request Type | Description |
|---|---|---|---|
| `get_adcp_capabilities` | protocol | GetAdcpCapabilitiesRequest | Declare supported domains/features |
| `get_principal` | protocol | GetPrincipalRequest | Read the authenticated principal configuration |
| `sync_principal` | protocol | SyncPrincipalRequest | Synchronize authenticated principal configuration |
| `get_products` | media_buy | GetProductsRequest | Return ad products matching a brief |
| `list_products` | media_buy | ListProductsRequest | List products with the compact lifecycle |
| `request_proposals` | media_buy | RequestProposalsRequest | Request seller proposals |
| `refine_proposals` | media_buy | RefineProposalsRequest | Refine seller proposals |
| `decline_proposals` | media_buy | DeclineProposalsRequest | Decline seller proposals |
| `buy_products` | media_buy | BuyProductsRequest | Commit a direct product purchase |
| `accept_proposal` | media_buy | AcceptProposalRequest | Accept a proposal and create its media buy |
| `control_media_buy` | media_buy | ControlMediaBuyRequest | Control an existing media buy |
| `list_creative_formats` | media_buy | ListCreativeFormatsRequest | List available creative formats |
| `create_media_buy` | media_buy | CreateMediaBuyRequest | Create a new media buy |
| `update_media_buy` | media_buy | UpdateMediaBuyRequest | Update an existing media buy |
| `get_media_buys` | media_buy | GetMediaBuysRequest | List media buys |
| `get_media_buy_delivery` | media_buy | GetMediaBuyDeliveryRequest | Get delivery metrics |
| `get_reporting_status` | media_buy | GetReportingStatusRequest | Read reporting delivery and reconciliation status |
| `sync_reporting_receipts` | media_buy | SyncReportingReceiptsRequest | Submit reporting reconciliation receipts |
| `provide_performance_feedback` | media_buy | ProvidePerformanceFeedbackRequest | Send conversion data |
| `sync_creatives` | media_buy | SyncCreativesRequest | Sync creative assets |
| `list_creatives` | media_buy | ListCreativesRequest | List synced creatives |
| `build_creative` | creative | BuildCreativeRequest | Generate a creative |
| `preview_creative` | creative | PreviewCreativeRequest | Preview a creative |
| `get_creative_delivery` | creative | GetCreativeDeliveryRequest | Get creative delivery tags |
| `get_creative_features` | governance | GetCreativeFeaturesRequest | Get creative feature definitions |
| `get_signals` | signals | GetSignalsRequest | Discover audience signals |
| `activate_signal` | signals | ActivateSignalRequest | Activate a signal |
| `list_account_changes` | media_buy | ListAccountChangesRequest | Read the durable account change feed |
| `list_accounts` | media_buy | ListAccountsRequest | List advertiser accounts |
| `sync_accounts` | media_buy | SyncAccountsRequest | Sync account data |
| `get_account_financials` | media_buy | GetAccountFinancialsRequest | Get financial details |
| `report_usage` | media_buy | ReportUsageRequest | Report usage metrics |
| `sync_event_sources` | media_buy | SyncEventSourcesRequest | Register conversion pixels |
| `log_event` | media_buy | LogEventRequest | Log conversion events |
| `sync_audiences` | media_buy | SyncAudiencesRequest | Sync audience segments |
| `sync_catalogs` | media_buy | SyncCatalogsRequest | Sync product catalogs |
| `sync_governance` | account | SyncGovernanceRequest | Sync governance config |
| `create_content_standards` | governance | CreateContentStandardsRequest | Create content standards |
| `get_content_standards` | governance | GetContentStandardsRequest | Get content standards |
| `list_content_standards` | governance | ListContentStandardsRequest | List content standards |
| `update_content_standards` | governance | UpdateContentStandardsRequest | Update content standards |
| `calibrate_content` | governance | CalibrateContentRequest | Evaluate content compliance |
| `validate_content_delivery` | governance | ValidateContentDeliveryRequest | Validate delivery compliance |
| `get_media_buy_artifacts` | governance | GetMediaBuyArtifactsRequest | Get compliance artifacts |
| `sync_plans` | governance | SyncPlansRequest | Sync campaign plans |
| `check_governance` | governance | CheckGovernanceRequest | Check governance approval |
| `report_plan_outcome` | governance | ReportPlanOutcomeRequest | Report plan outcomes |
| `get_plan_audit_logs` | governance | GetPlanAuditLogsRequest | Get audit logs |
| `si_get_offering` | sponsored_intelligence | SiGetOfferingRequest | Get SI offering details |
| `si_initiate_session` | sponsored_intelligence | SiInitiateSessionRequest | Start SI session |
| `si_send_message` | sponsored_intelligence | SiSendMessageRequest | Send SI message |
| `si_terminate_session` | sponsored_intelligence | SiTerminateSessionRequest | End SI session |

## Response Builders

Import from `adcp.server.responses`:

| Function | Returns | Use With |
|---|---|---|
| `capabilities_response(protocols)` | Capabilities dict | `get_adcp_capabilities` |
| `products_response(products)` | Products dict | `get_products` |
| `media_buy_response(id, packages)` | Media buy dict | `create_media_buy` |
| `update_media_buy_response(id, packages)` | Updated buy dict | `update_media_buy` |
| `media_buys_response(media_buys)` | Media buys list | `get_media_buys` |
| `delivery_response(delivery)` | Delivery metrics | `get_media_buy_delivery` |
| `creative_formats_response(formats)` | Formats list | `list_creative_formats` |
| `sync_creatives_response(creatives)` | Sync results | `sync_creatives` |
| `list_creatives_response(creatives)` | Creatives list | `list_creatives` |
| `build_creative_response(manifest)` | Creative manifest | `build_creative` |
| `preview_creative_response(previews)` | Preview URLs | `preview_creative` |
| `signals_response(signals)` | Signals list | `get_signals` |
| `activate_signal_response(signal)` | Activation result | `activate_signal` |
| `sync_accounts_response(accounts)` | Sync results | `sync_accounts` |
| `log_event_response(events)` | Log results | `log_event` |
| `sync_catalogs_response(catalogs)` | Catalog results | `sync_catalogs` |
| `error_response(code, message)` | Error dict | Any handler |
| `media_buy_error_response(errors)` | Error dict | `create/update_media_buy` |

## Type Guards

Import from `adcp.types.guards` or `adcp`:

```python
from adcp import is_adcp_success, is_adcp_error

if is_adcp_success(response):
    print(response.media_buy_id)  # type-narrowed
else:
    print(response.errors)
```

## Common Error Codes

| Code | Meaning | Recovery |
|---|---|---|
| `NOT_SUPPORTED` | Agent doesn't implement this task | Check capabilities first |
| `INVALID_BUDGET` | Budget below minimum | Increase to minimum |
| `INVALID_REQUEST` | Malformed request | Fix request params |
| `MISSING_CREATIVE` | No creatives attached | Call sync_creatives first |
| `MEDIA_BUY_NOT_FOUND` | Unknown media buy ID | Verify ID from create response |
| `UNAUTHORIZED` | Missing/invalid auth | Check auth token |
| `RATE_LIMITED` | Too many requests | Retry with backoff |

## Optional Protocol Extensions

These protocols are opt-in upgrades for `DecisioningPlatform` subclasses. Implement them to unlock advanced framework behavior.

| Protocol | Method | Domain | Description |
|---|---|---|---|
| `IncrementalGetProducts` | `get_products_incremental` | media_buy | **Protocol declaration only — dispatch path ships in a follow-up to #495.** Implement to stream partial results on `time_budget` timeout; the framework will collect yielded batches until the deadline and project `incomplete[]` for unfinished scopes. Until the dispatch path lands, implementing this Protocol has no effect — timeouts still return `products: []` + `incomplete: [{scope: 'products'}]`. |

Import from `adcp.decisioning`:

```python
from adcp.decisioning import IncrementalGetProducts, ProductsCheckpoint
from adcp.types import GetProductsRequest
from adcp.decisioning import RequestContext
from typing import AsyncIterator

class MySeller(DecisioningPlatform, IncrementalGetProducts):
    async def get_products_incremental(
        self,
        req: GetProductsRequest,
        ctx: RequestContext,
        checkpoint: ProductsCheckpoint,
    ) -> AsyncIterator[dict]:
        for batch in self._stream_products(req):
            checkpoint.add_batch(batch)
            yield batch
```

**Note:** `get_products_incremental` MUST be an async generator (`async def` with `yield`). Detection uses `asyncio.isasyncgenfunction`. When `req.time_budget.unit == 'campaign'`, no SDK-managed deadline is installed; the adopter decides timing.

## DX Helpers (adcp.server.helpers)

Eliminate boilerplate in handler code. Import from `adcp.server` or `adcp.server.helpers`.

### Error Responses

```python
from adcp.server import adcp_error

# Auto-recovery from 20+ standard codes (transient/correctable/terminal)
return adcp_error("BUDGET_TOO_LOW", "Budget $50 is below $500 minimum",
                  field="budget", suggestion="Increase to at least $500")

return adcp_error("RATE_LIMITED", retry_after=30)

return adcp_error("PRODUCT_NOT_FOUND",
    field="product_id",
    suggestion="Use get_products to discover available products")
```

### Media Buy State Machine

```python
from adcp.server import valid_actions_for_status, is_terminal_status
from adcp.server.responses import media_buy_response

# Response builders auto-populate valid_actions from status
resp = media_buy_response("mb_1", packages, status="active")
# resp["valid_actions"] = ["pause", "cancel", "update_budget", ...]
# resp["revision"] = 1  (auto-set)
# resp["confirmed_at"] = "2026-..."  (auto-set)
```

### Account Resolution

```python
from adcp.server import resolve_account

async def create_media_buy(self, params, context=None):
    account, error = await resolve_account(params, self.find_account)
    if error:
        return error  # Auto-formatted ACCOUNT_NOT_FOUND response
    # account is guaranteed non-None here
```

### Context Passthrough

```python
from adcp.server import inject_context

async def get_products(self, params, context=None):
    response = {"products": my_products}
    return inject_context(params, response)  # Echoes params.context back
```

### Cancellation

```python
from adcp.server import cancel_media_buy_response

# Auto-sets canceled_at=now, status="canceled", valid_actions=[]
return cancel_media_buy_response("mb_1", "buyer", reason="Campaign ended")
```

### Decorator Builder (auto-capabilities)

```python
from adcp.server import adcp_server, serve

server = adcp_server("my-seller")

@server.get_products
async def get_products(params, context=None):
    return products_response(MY_PRODUCTS)

# get_adcp_capabilities auto-generated from registered handlers
serve(server, name="my-seller")
```

## Import Quick Reference

```python
# Client setup
from adcp import ADCPClient, ADCPMultiAgentClient, AgentConfig

# Request/response types
from adcp.types import GetProductsRequest, CreateMediaBuyRequest, Product, Package

# Or import from a curated partial module for a narrower surface:
#   adcp.types.media_buy / creative / signals / protocol / buyer / seller
from adcp.types.media_buy import CreateMediaBuyRequest
# Never import from adcp.types.generated_poc.* or adcp.types._generated (internal)

# Response variant types (discriminated unions)
from adcp.types.aliases import CreateMediaBuySuccessResponse, CreateMediaBuyErrorResponse

# Type guards
from adcp.types.guards import is_create_media_buy_success, is_adcp_error

# Server framework
from adcp.server import ADCPHandler, serve
from adcp.server.responses import capabilities_response, products_response

# DX helpers
from adcp.server import adcp_error, valid_actions_for_status, resolve_account
from adcp.server import inject_context, cancel_media_buy_response

# Pre-configured test agents (simple smoke testing)
from adcp.testing import test_agent, creative_agent

# In-process transport testing (DecisioningPlatform)
from adcp.testing import build_asgi_app, build_test_client, make_request_context

# build_asgi_app: returns a Starlette ASGI app for httpx.ASGITransport / TestClient
# build_test_client: async context manager wrapping build_asgi_app + LifespanManager
# Example:
#   async with build_test_client(my_platform, auth=BearerTokenAuth(...)) as client:
#       resp = await client.post("/mcp/", json={"jsonrpc": "2.0", ...})
```
