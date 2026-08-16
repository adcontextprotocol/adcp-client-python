# AdCP Python SDK Examples

A categorized index of the runnable examples in this directory. Each
example is a single-file (or single-directory) demo with a module
docstring explaining what it shows and how to run it.

Two sides of the protocol are covered:

- **Buyer** — calling AdCP agents. Entry point: `from adcp import ADCPClient, AgentConfig`.
  `client.simple.*` is the recommended starter API.
- **Seller** — building an AdCP agent. Entry point:
  `from adcp.server import ADCPHandler, serve`, or the
  `DecisioningPlatform` framework in `adcp.decisioning`.

For task-level guidance beyond these scripts, see the `skills/`
directory at the repo root (buyer skills like `call-adcp-agent` and
`adcp-media-buy`; seller skills like `build-seller-agent`).

## Running an example

Install the package, then run any script by path:

```bash
pip install -e .
python examples/basic_usage.py
```

Some examples need a local mock-server or environment variables — see
the "Run" section in each file's module docstring for prerequisites.

## Buyer quickstart

Calling AdCP agents as a buyer.

- [`basic_usage.py`](basic_usage.py) — configure an `ADCPClient`, call `get_products`, handle sync vs async responses.
- [`simple_api_demo.py`](simple_api_demo.py) — the `.simple` accessor: pass kwargs directly, get unwrapped data, exceptions on error.
- [`multi_agent.py`](multi_agent.py) — configure multiple agents and run operations across all of them in parallel.
- [`test_helpers_demo.py`](test_helpers_demo.py) — use the built-in test agents from `adcp.testing` (`test_agent`, `creative_agent`) for quick demos.
- [`type_aliases_demo.py`](type_aliases_demo.py) — the ergonomic semantic type aliases for clearer code.
- [`fetch_agent_authorizations.py`](fetch_agent_authorizations.py) — discover which publishers have authorized your agent, via the agent's capabilities ("push") and via publisher `adagents.json` ("pull").
- [`adagents_validation.py`](adagents_validation.py) — verify a sales agent is authorized to sell for a publisher's properties using the `adagents.json` validation utilities.

Related skills: `call-adcp-agent` (wire-level invariants for any buyer
call), `adcp-media-buy`, `adcp-creative`, `adcp-signals`,
`adcp-governance`, `adcp-brand`, `adcp-si`.

## Building a seller agent

Minimal-to-reference seller implementations. The `hello_seller_*`
files are each the smallest possible adopter for one AdCP specialism.

- [`minimal_sales_agent.py`](minimal_sales_agent.py) — single-file MCP sales agent ("Riverdale Gazette") covering the `get_adcp_capabilities` → `get_products` → `create_media_buy` flow.
- [`hello_seller.py`](hello_seller.py) — the canonical `DecisioningPlatform` starting point: the full required `sales-non-guaranteed` method surface, plus minimum valid buyer payloads.
- [`hello_seller_async_handoff.py`](hello_seller_async_handoff.py) — the three `create_media_buy` return shapes: sync success, correctable `AdcpError`, and `TaskHandoff` for HITL/background work.
- [`hello_seller_audience.py`](hello_seller_audience.py) — minimal `audience-sync` seller (`sync_audiences` with delta upsert).
- [`hello_seller_brand_rights.py`](hello_seller_brand_rights.py) — minimal `brand-rights` seller (`get_brand_identity`, `get_rights`, `acquire_rights` with a 4-arm success union).
- [`hello_seller_catalog.py`](hello_seller_catalog.py) — minimal `sales-catalog-driven` seller (adds `sync_catalogs`).
- [`hello_seller_collection_lists.py`](hello_seller_collection_lists.py) — minimal `collection-lists` seller (5-method CRUD + fetch-token issuance).
- [`hello_seller_content_standards.py`](hello_seller_content_standards.py) — minimal `content-standards` seller (CRUD + calibration + validation).
- [`hello_seller_creative.py`](hello_seller_creative.py) — minimal `creative-generative` / `creative-template` seller; template for AI-creative integrators returning a `CreativeManifest`.
- [`hello_seller_governance.py`](hello_seller_governance.py) — minimal `governance-spend-authority` / `governance-delivery-monitor` seller (note: requires `governance_aware=True`).
- [`hello_seller_property_lists.py`](hello_seller_property_lists.py) — minimal `property-lists` seller (5-method CRUD + fetch-token issuance).
- [`hello_seller_signals.py`](hello_seller_signals.py) — minimal `signal-marketplace` seller (`get_signals`, `activate_signal`); canonical `TaskHandoff` example.
- [`seller_agent.py`](seller_agent.py) — reference `ADCPHandler` seller for the `media_buy_seller` storyboard (9 steps, all core tools).
- [`typed_handler_demo.py`](typed_handler_demo.py) — declare handler `params` as a Pydantic model so the dispatcher validates at the boundary.
- [`scheduler_lifespan.py`](scheduler_lifespan.py) — lifecycle-bound background work via `serve(on_startup=, on_shutdown=)`.
- [`hello_mock_seller.py`](hello_mock_seller.py) — mock-mode upstream URL routing: swap the upstream per request for spec-conformance storyboards without a real backend.
- [`hello_proposal_manager.py`](hello_proposal_manager.py) — per-tenant `ProposalManager` binding via `PlatformRouter` (two-platform composition).
- [`hello_proposal_manager_v15.py`](hello_proposal_manager_v15.py) — the full v1.5 proposal lifecycle (`get_products` / `refine_products` / `finalize_proposal`) behind a minimal-LOC adopter.
- [`multi_platform_seller/`](multi_platform_seller/README.md) — directory example: N tenants, N `DecisioningPlatform` subclasses, one `serve()` process, dispatched by `PlatformRouter`.
- [`sales_proposal_mode_seller/`](sales_proposal_mode_seller/README.md) — directory example: the v1.5 `ProposalManager` surface end-to-end, passing the `proposal_finalize.yaml` storyboard.
- [`v3_reference_seller/`](v3_reference_seller/README.md) — directory example: canonical multi-tenant translator-pattern seller (AdCP wire in, real upstream ad server out); includes `MIGRATION.md`.

Related skills: `build-seller-agent`, `build-generative-seller-agent`,
`build-retail-media-agent`, `build-creative-agent`,
`build-signals-agent`. The seller storyboard tests live at
[`../tests/test_seller_agent_storyboard.py`](../tests/test_seller_agent_storyboard.py).

## Authentication & multi-tenancy

- [`mcp_with_auth_middleware.py`](mcp_with_auth_middleware.py) — multi-tenant MCP server with bearer-token auth via `BearerTokenAuthMiddleware` + `auth_context_factory`.
- [`buyer_agent_registry_sqlalchemy.py`](buyer_agent_registry_sqlalchemy.py) — SQLAlchemy-backed `BuyerAgentRegistry` for v3 sellers: the commercial-identity allowlist gate (signing-only and bearer-only factory shapes).

## Webhooks

- [`hello_seller_with_webhooks.py`](hello_seller_with_webhooks.py) — legacy, non-conformant sync-completion compatibility example using `InMemoryWebhookDeliverySupervisor`; normal `TaskHandoff` notifications require no sync opt-in.

See `docs/handler-authoring.md#webhooks` for the full `WebhookSender`
constructor comparison (bearer vs RFC 9421 JWK signing).

## Request signing & identity

- [`buyer_agent_registry_sqlalchemy.py`](buyer_agent_registry_sqlalchemy.py) — the commercial side of v3 identity (the cryptographic side lives in `adcp.signing`). The framework verifies signed traffic before this registry runs the commercial allowlist lookup.

## Multi-agent / A2A task stores

Durable, scope-isolated A2A `TaskStore` + `PushNotificationConfigStore`
implementations. Both carry an important security model in their
docstrings (tenant-scoped lookups; webhook-URL SSRF and plaintext
secret risks the adopter must address before production).

- [`a2a_db_tasks.py`](a2a_db_tasks.py) — raw-SQLite reference durable stores; SQLite chosen because the SQL pattern ports directly to Postgres / MySQL.
- [`a2a_sqlalchemy_tasks.py`](a2a_sqlalchemy_tasks.py) — SQLAlchemy-ORM companion: wrap an existing schema behind the a2a-sdk Protocols; runs on any SQLAlchemy backend.

## Creative preview & build

Fetch creative-format preview URLs from a creative agent and render
them with the `<rendered-creative>` web component.

- [`fetch_preview_urls.py`](fetch_preview_urls.py) — connect to the reference creative agent, list formats, generate preview URLs, and save them to `preview_urls.json`.
- [`generate_mock_previews.py`](generate_mock_previews.py) — generate mock preview data (for when the reference creative agent returns text rather than structured format data).
- [`web_component_demo.html`](web_component_demo.html) — HTML page that renders the saved previews with the `<rendered-creative>` web component.
- [`preview_urls.json`](preview_urls.json) — generated preview-URL data consumed by the HTML demo.

### Preview workflow

```bash
# 1. Fetch preview URLs from the creative agent (writes preview_urls.json)
python examples/fetch_preview_urls.py

# 2. Serve the repo over HTTP (the web component needs http://, not file://)
python -m http.server 8000

# 3. Open the demo
#    http://localhost:8000/examples/web_component_demo.html
```

The Python side uses the `fetch_previews=True` parameter on
`list_creative_formats` (and `get_products`) to attach preview data to
the response metadata:

```python
from adcp import ADCPClient
from adcp.types import AgentConfig, ListCreativeFormatsRequest, Protocol

creative_agent = ADCPClient(
    AgentConfig(
        id="creative_agent",
        agent_uri="https://creative.adcontextprotocol.org",
        protocol=Protocol.MCP,
    )
)

result = await creative_agent.list_creative_formats(
    ListCreativeFormatsRequest(),
    fetch_previews=True,
)
formats_with_previews = result.metadata["formats_with_previews"]
```

If the previews file is missing, run `python examples/fetch_preview_urls.py`
first. If the web component doesn't load, confirm you are serving over
`http://` (not `file://`) and have internet access — the component
loads from `https://creative.adcontextprotocol.org`.

Related skill: `adcp-creative`.

## More

For broader documentation see the
[AdCP docs](https://docs.adcontextprotocol.org) and the
[integration tests](../tests/integration/).
