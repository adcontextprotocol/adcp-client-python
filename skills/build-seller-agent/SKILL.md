---
name: build-seller-agent
description: Use when building an AdCP seller agent — a publisher, SSP, or retail media network that sells advertising inventory to buyer agents.
---

# Build a Seller Agent (Python)

## Overview

A seller agent receives briefs from buyers, returns products with pricing, accepts media buys, manages creatives, and reports delivery. The business model — what you sell, how you price it, and whether humans approve deals — shapes every implementation decision. Determine that first.

## When to Use

- User wants to build an agent that sells ad inventory
- User mentions publisher, SSP, retail media, or media network in the context of AdCP
- User references `get_products`, `create_media_buy`, or the media buy protocol

**Not this skill:**
- Buying ad inventory → buyer/DSP agent
- Serving audience segments → `skills/build-signals-agent/`
- Rendering creatives from briefs → creative agent

## Before Writing Code

Determine these five things. Ask the user — don't guess.

### 1. What Kind of Seller?

- **Premium publisher** — guaranteed inventory, fixed pricing, IO approval
- **SSP / Exchange** — non-guaranteed, auction-based, instant activation
- **Retail media network** — both guaranteed and non-guaranteed, catalog-driven

### 2. Guaranteed or Non-Guaranteed?

- **Guaranteed** — `delivery_type: "guaranteed"`, may require async approval
- **Non-guaranteed** — `delivery_type: "non_guaranteed"`, buyer sets `bid_price`, instant activation

### 3. Products and Pricing

Each product needs: name, description, publisher_properties, format_ids, delivery_type, pricing_options.

Pricing models:
- `cpm` — `CpmPricingOption(pricing_option_id="...", pricing_model="cpm", floor_price=12.00, currency="USD")`
- `flat_rate` — `FlatRatePricingOption(pricing_option_id="...", pricing_model="flat_rate", fixed_price=250.00, currency="USD")`

### 4. Approval Workflow

- **Instant** — `create_media_buy` returns confirmed status
- **Async** — returns submitted, buyer polls `get_media_buys`

### 5. Creative Management

- **Standard** — `list_creative_formats` + `sync_creatives`
- **None** — omit creative tools

## Architecture

One file. Subclass `ADCPHandler`, override the tools you support, call `serve()`.

```python
from adcp.server import ADCPHandler, serve, adcp_error, resolve_account
from adcp.server.responses import capabilities_response, products_response, media_buy_response
from adcp.server.test_controller import TestControllerStore

class MySeller(ADCPHandler):
    async def get_adcp_capabilities(self, params, context=None):
        return capabilities_response(["media_buy"])

    async def get_products(self, params, context=None):
        return products_response(MY_PRODUCTS)

    # ... more tools ...

serve(MySeller(), name="my-seller", test_controller=MyStore())
```

## Product Construction Example

Every product needs `description`, `reporting_capabilities`, and `delivery_measurement` — these are required by the schema and the storyboard validator.

```python
import os

ADCP_PORT = int(os.environ.get("ADCP_PORT", 3001))
AGENT_URL = f"http://localhost:{ADCP_PORT}/mcp"

PRODUCTS = [
    {
        "product_id": "premium-homepage",
        "name": "Homepage Takeover",
        "description": "Full-page homepage placement with 100% SOV",
        "delivery_type": "guaranteed",
        "publisher_properties": [
            {"publisher_domain": "example.com", "selection_type": "all"}
        ],
        "format_ids": [
            {"agent_url": AGENT_URL, "id": "display_970x250"}
        ],
        "pricing_options": [
            {
                "pricing_option_id": "po-cpm-homepage",
                "pricing_model": "cpm",
                "floor_price": 15.00,
                "currency": "USD",
            }
        ],
        "reporting_capabilities": {
            "available_metrics": ["impressions", "spend", "clicks", "ctr"],
            "available_reporting_frequencies": ["hourly", "daily"],  # hourly|daily|monthly only
            "date_range_support": "date_range",  # or "lifetime_only"
            "supports_webhooks": False,
            "expected_delay_minutes": 60,
            "timezone": "UTC",
        },
        "delivery_measurement": {
            "provider": "internal",
        },
    },
]
```

## DX Helpers

The SDK provides helpers that eliminate common boilerplate. Import from `adcp.server`:

**Automatic behaviors** (no handler code needed):
- **Context passthrough** — if the request has a `context` field, it's echoed back in the response automatically.

| Helper | What it does |
|--------|-------------|
| `adcp_error(code, message, field=, suggestion=)` | Structured error with auto-recovery classification (20+ standard codes) |
| `media_buy_response(..., status="active")` | Auto-populates `valid_actions` from status, auto-sets `revision` and `confirmed_at` |
| `cancel_media_buy_response(id, "buyer")` | Auto-sets `canceled_at`, `status`, `valid_actions=[]` |
| `resolve_account(params, resolver)` | Auto-resolves AccountReference, returns ACCOUNT_NOT_FOUND if missing |
| `valid_actions_for_status(status)` | Maps status to valid buyer actions |
| `is_terminal_status(status)` | True for completed/rejected/canceled |
| `AccountError(code, message, suggestion=)` | Raise from resolver for suspended/payment/ambiguous accounts |

## Account Resolution Pattern

Sellers that require accounts should resolve them before processing:

```python
from adcp.server import resolve_account, AccountError

class MySeller(ADCPHandler):
    async def _resolve(self, ref):
        account = db.find(ref)
        if not account:
            return None  # auto-returns ACCOUNT_NOT_FOUND
        if account.status == "suspended":
            raise AccountError("ACCOUNT_SUSPENDED")
        if account.status == "payment_required":
            raise AccountError("ACCOUNT_PAYMENT_REQUIRED",
                suggestion="Update payment at https://billing.example.com")
        return account

    async def create_media_buy(self, params, context=None):
        account, error = await resolve_account(params, self._resolve)
        if error:
            return error  # structured error response
        # account is guaranteed non-None here
        ...
```

## Emitting Webhooks

Sellers emit webhooks to notify buyers asynchronously. AdCP 3.0 uses RFC 9421 HTTP Message Signatures — use `adcp.webhooks.WebhookSender` (one-call helper) or `sign_webhook` for lower-level control.

**When to emit:**
- Async-approval media buy transitions (`pending_creatives` → `pending_start` → `active`, or `→ rejected`)
- Artifact-ready notifications after long-running operations
- Delivery reports the buyer subscribed to via `reporting_webhook` on `create_media_buy`

**Idempotency keys.** Build with `generate_webhook_idempotency_key()` (or let `send_mcp` mint one via `create_mcp_webhook_payload`). Persist per event and reuse on retry — receivers dedupe on it. Prefer `sender.resend(result)` for retries: it replays the exact signed bytes under a fresh signature.

```python
from adcp.types import GeneratedTaskStatus
from adcp.webhooks import WebhookSender, generate_webhook_idempotency_key

# JWK must include the private `d` field and `adcp_use: "webhook-signing"`.
sender = WebhookSender.from_jwk(WEBHOOK_SIGNING_JWK)

async def notify_media_buy_active(webhook_url: str, mb_id: str, mb: dict) -> None:
    idem_key = generate_webhook_idempotency_key()  # persist per-event; reuse on retry
    async with sender:
        result = await sender.send_mcp(
            url=webhook_url,
            task_id=mb_id,
            task_type="create_media_buy",
            status=GeneratedTaskStatus.completed,
            result={"media_buy_id": mb_id, "status": "active", "packages": mb["packages"]},
            message="Media buy approved and activated",
            idempotency_key=idem_key,
        )
        if not result.ok:
            await sender.resend(result)  # replays exact bytes, fresh signature
```

**Legacy HMAC.** `get_adcp_signed_headers_for_webhook` is 3.x-only and deprecated in 4.0 — use only when a receiver can't yet verify 9421 signatures. See `adcp.webhooks` module docstring for the migration path.

**Receiving webhooks.** Sellers that receive webhooks from other agents (audit, governance) should use `adcp.webhooks.WebhookReceiver` — it handles 9421 verification, replay dedup, and legacy HMAC fallback.

## Proposal Workflow (Guaranteed Deals)

For premium/guaranteed inventory, buyers negotiate before committing. The flow:

1. Buyer calls `get_products` with `buying_mode: "refine"` and a `proposal`
2. Seller returns products with a `proposal` containing `proposal_id` and `status: "draft"`
3. Buyer refines (adjusts budget, dates, packages) by calling `get_products` again with the `proposal_id`
4. When satisfied, buyer calls `create_media_buy` with the committed `proposal_id`
5. Seller validates the proposal and creates the media buy

```python
async def get_products(self, params, context=None):
    buying_mode = params.get("buying_mode", "brief")

    if buying_mode == "refine":
        proposal = params.get("proposal", {})
        proposal_id = proposal.get("proposal_id") or f"prop-{uuid.uuid4().hex[:8]}"
        incoming_packages = proposal.get("packages", [])
        # Store/update the proposal draft
        proposals[proposal_id] = {
            "status": "draft",
            "packages": incoming_packages,
        }
        # proposal.json requires: proposal_id, name, allocations (minItems: 1).
        # Each allocation requires product_id and allocation_percentage (must sum to 100).
        n = max(len(incoming_packages), 1)
        even_split = round(100 / n, 2)
        return {**products_response(PRODUCTS), "proposals": [{
            "proposal_id": proposal_id,
            "name": proposal.get("name", "Draft proposal"),
            "proposal_status": "draft",
            "allocations": [
                {
                    "product_id": p["product_id"],
                    "allocation_percentage": even_split,
                }
                for p in incoming_packages
            ] or [
                {
                    "product_id": PRODUCTS[0]["product_id"],
                    "allocation_percentage": 100.0,
                }
            ],
        }]}

    # Default brief mode - return all matching products
    return products_response(PRODUCTS)

async def create_media_buy(self, params, context=None):
    proposal_id = params.get("proposal_id")
    if proposal_id:
        proposal = proposals.get(proposal_id)
        if not proposal:
            return adcp_error("INVALID_REQUEST", "Unknown proposal",
                              field="proposal_id")
        if proposal.get("status") != "draft":
            return adcp_error("PROPOSAL_NOT_COMMITTED",
                              "Proposal must be in draft status")
    # ... create the media buy from the proposal
```

For IO-required deals, return `IO_REQUIRED` from `create_media_buy` until the IO is signed:
```python
if product["delivery_type"] == "guaranteed" and not has_signed_io(account):
    return adcp_error("IO_REQUIRED",
                      suggestion="Sign the IO at https://seller.example.com/io")
```

## Tools and Required Response Shapes

Every tool below uses a response builder from `adcp.server.responses`. Use the builders — never return raw dicts.

**`get_adcp_capabilities`**
```python
from adcp.server.responses import capabilities_response

async def get_adcp_capabilities(self, params, context=None):
    return capabilities_response(["media_buy"])
```

**`sync_accounts`**
```python
from adcp.server.responses import sync_accounts_response

async def sync_accounts(self, params, context=None):
    results = []
    for acct in params.get("accounts", []):
        account_id = f"acct-{uuid.uuid4().hex[:8]}"
        # Store in memory so test controller can find it
        accounts[account_id] = {"status": "active", "brand": acct.get("brand"), "operator": acct.get("operator")}
        results.append({
            "account_id": account_id,
            "brand": acct.get("brand"),        # echo back
            "operator": acct.get("operator"),   # echo back
            "action": "created",
            "status": "active",
            "account_scope": "operator_brand",
        })
    return sync_accounts_response(results)
```

**`sync_governance`**
```python
from adcp.server.responses import sync_governance_response

async def sync_governance(self, params, context=None):
    results = []
    for entry in params.get("accounts", []):
        acct_ref = entry.get("account", {})
        agents = entry.get("governance_agents", [])
        results.append({
            "account": acct_ref,
            "status": "synced",
            "governance_agents": [
                {"url": a.get("url"), "categories": a.get("categories", [])}
                for a in agents
            ],
        })
    return sync_governance_response(results)
```

**`get_products`**
```python
from adcp.server.responses import products_response
from adcp.types import Product, CpmPricingOption, FormatId, PublisherPropertiesAll, DeliveryType, DeliveryMeasurement

async def get_products(self, params, context=None):
    return products_response(PRODUCTS)
```

**`create_media_buy`**
```python
from adcp.server import adcp_error
from adcp.server.responses import media_buy_response

async def create_media_buy(self, params, context=None):
    if not params.get("packages"):
        return adcp_error("INVALID_REQUEST", "At least one package required",
                          field="packages")

    packages = []
    for pkg in params.get("packages", []):
        product_id = pkg.get("product_id")
        if product_id not in {p["product_id"] for p in PRODUCTS}:
            return adcp_error("PRODUCT_NOT_FOUND",
                              f"Product '{product_id}' not found",
                              field="product_id",
                              suggestion="Use get_products to discover available products")
        packages.append({
            "package_id": f"pkg-{uuid.uuid4().hex[:8]}",
            "product_id": product_id,
            "pricing_option_id": pkg.get("pricing_option_id"),
            "budget": pkg.get("budget"),
        })
    mb_id = f"mb-{uuid.uuid4().hex[:8]}"
    # Store so get_media_buys and test controller can find it
    media_buys[mb_id] = {"status": "active", "currency": "USD", "packages": packages}
    # status="active" auto-populates valid_actions, revision, confirmed_at
    return media_buy_response(mb_id, packages, status="active")
```

**`get_media_buys`**
```python
from adcp.server.responses import media_buys_response

async def get_media_buys(self, params, context=None):
    requested_ids = params.get("media_buy_ids")
    results = []
    for mb_id, mb in media_buys.items():
        if requested_ids and mb_id not in requested_ids:
            continue
        results.append({
            "media_buy_id": mb_id,
            "status": mb["status"],
            "currency": mb.get("currency", "USD"),
            "packages": mb.get("packages", []),
        })
    return media_buys_response(results)
```

**`update_media_buy`** — handles pause, resume, cancel, budget changes, package updates.
```python
from adcp.server import adcp_error, cancel_media_buy_response
from adcp.server.responses import update_media_buy_response

async def update_media_buy(self, params, context=None):
    mb_id = params.get("media_buy_id")
    mb = media_buys.get(mb_id)
    if not mb:
        return adcp_error("MEDIA_BUY_NOT_FOUND", f"Media buy {mb_id} not found")

    # Check revision for optimistic concurrency
    if params.get("revision") and params["revision"] != mb.get("revision", 1):
        return adcp_error("CONFLICT", "Revision mismatch - refetch and retry")

    status = mb["status"]

    # Handle pause/resume/cancel
    if params.get("paused") is True and status == "active":
        mb["status"] = "paused"
    elif params.get("paused") is False and status == "paused":
        mb["status"] = "active"
    elif params.get("canceled") is True:
        if status in ("completed", "rejected", "canceled"):
            return adcp_error("NOT_CANCELLABLE",
                              f"Cannot cancel a {status} media buy")
        mb["status"] = "canceled"
        return cancel_media_buy_response(mb_id, "buyer")

    # Handle package updates
    if params.get("packages"):
        for pkg_update in params["packages"]:
            pkg_id = pkg_update.get("package_id")
            # Apply budget, dates, etc.

    mb["revision"] = mb.get("revision", 1) + 1
    return update_media_buy_response(
        mb_id, status=mb["status"], revision=mb["revision"]
    )
```

**`list_creative_formats`**
```python
from adcp.server.responses import creative_formats_response

async def list_creative_formats(self, params, context=None):
    return creative_formats_response([
        {
            "format_id": {"agent_url": AGENT_URL, "id": "display_300x250"},
            "name": "Display 300x250",
            "renders": [{"width": 300, "height": 250}],
            "assets": [{
                "item_type": "individual",
                "asset_id": "image",
                "asset_type": "image",
                "required": True,
                "accepted_media_types": ["image/png", "image/jpeg"],
            }],
        },
    ])
```

**`sync_creatives`**
```python
from adcp.server.responses import sync_creatives_response

async def sync_creatives(self, params, context=None):
    results = []
    for c in params.get("creatives", []):
        creative_id = c.get("creative_id", f"c-{uuid.uuid4().hex[:8]}")
        # Store so test controller can find it
        creatives[creative_id] = {**c, "status": "approved"}
        results.append({
            "creative_id": creative_id,
            "action": "created",
            "status": "approved",
        })
    return sync_creatives_response(results)
```

**`get_media_buy_delivery`**
```python
from adcp.server.responses import delivery_response

async def get_media_buy_delivery(self, params, context=None):
    requested_ids = params.get("media_buy_ids", [])
    deliveries = []
    for mb_id in requested_ids:
        if mb_id in media_buys:
            deliveries.append({
                "media_buy_id": mb_id,
                "status": "active",
                "totals": {"impressions": 45000, "clicks": 680, "spend": 540.00},
                "by_package": [],
            })
    return delivery_response(
        deliveries,
        reporting_period={"start": "2026-04-01T00:00:00Z", "end": "2026-04-09T23:59:59Z"},
    )
```

## Compliance Testing

Add a `TestControllerStore` so storyboard tests can force state transitions. Override the methods for scenarios your agent supports.

```python
from adcp.server.test_controller import TestControllerStore, TestControllerError

class MyStore(TestControllerStore):
    async def force_account_status(self, account_id, status):
        acct = accounts.get(account_id)
        if not acct:
            raise TestControllerError("NOT_FOUND", f"Account {account_id} not found")
        prev = acct["status"]
        acct["status"] = status
        return {"previous_state": prev, "current_state": status}

    async def force_media_buy_status(self, media_buy_id, status, rejection_reason=None):
        mb = media_buys.get(media_buy_id)
        if not mb:
            raise TestControllerError("NOT_FOUND", f"Media buy {media_buy_id} not found")
        prev = mb["status"]
        if prev in ("completed", "rejected", "canceled"):
            raise TestControllerError("INVALID_TRANSITION", f"Cannot transition from {prev}", current_state=prev)
        mb["status"] = status
        return {"previous_state": prev, "current_state": status}

    async def force_creative_status(self, creative_id, status, rejection_reason=None):
        c = creatives.get(creative_id)
        if not c:
            raise TestControllerError("NOT_FOUND", f"Creative {creative_id} not found")
        prev = c.get("status", "unknown")
        if prev == "archived":
            raise TestControllerError("INVALID_TRANSITION", "Cannot transition from archived", current_state=prev)
        c["status"] = status
        return {"previous_state": prev, "current_state": status}

    async def simulate_delivery(self, media_buy_id, impressions=None, clicks=None, conversions=None, reported_spend=None):
        if media_buy_id not in media_buys:
            raise TestControllerError("NOT_FOUND", f"Media buy {media_buy_id} not found")
        simulated = {"media_buy_id": media_buy_id}
        if impressions is not None: simulated["impressions"] = impressions
        if clicks is not None: simulated["clicks"] = clicks
        return {"simulated": simulated, "cumulative": simulated}

    async def simulate_budget_spend(self, spend_percentage, account_id=None, media_buy_id=None):
        return {"simulated": {"spend_percentage": spend_percentage}}
```

Pass the store to `serve()`:
```python
serve(MySeller(), name="my-seller", test_controller=MyStore())
```

`compliance_testing` is a separate top-level capability block — not a `supported_protocols` value. `serve(test_controller=store)` registers the `comply_test_controller` tool but does NOT inject the capability block. Declare it yourself via the `compliance_testing=` kwarg on `capabilities_response()`.

`adcp.idempotency.supported` is REQUIRED in AdCP 3.0 GA — always pass `idempotency=store.capability()` too. Use one shared `IdempotencyStore` per agent and reuse it for `@store.wrap` on mutating tools:

```python
from adcp.server.idempotency import IdempotencyStore, MemoryBackend

idempotency = IdempotencyStore(backend=MemoryBackend(), ttl_seconds=86400)

class MySeller(ADCPHandler):
    async def get_adcp_capabilities(self, params, context=None):
        return capabilities_response(
            ["media_buy"],
            idempotency=idempotency.capability(),
            compliance_testing={"scenarios": [
                "force_account_status",
                "force_media_buy_status",
                "force_creative_status",
                "simulate_delivery",
                "simulate_budget_spend",
            ]},
        )
```

## Emitting Webhooks

When a long-running operation finishes (or progresses), POST a webhook to the buyer's `push_notification_config` / `reporting_webhook`. The SDK gives you two helpers; pick based on the buyer's authentication profile.

**AdCP 4.0 default — RFC 9421 signing** (use `WebhookSender`):
```python
from adcp.webhooks import WebhookSender, create_mcp_webhook_payload

sender = WebhookSender.from_jwk(webhook_signing_jwk_with_private_d)
async with sender:
    result = await sender.send_mcp(
        url=str(config.url),
        task_id=task_id,
        task_type="create_media_buy",
        status="completed",
        result=response_dict,
    )
    if not result.ok:
        retry = await sender.resend(result)  # byte-identical replay
```

**AdCP 3.x legacy — Bearer or HMAC-SHA256** (use `deliver`):
```python
from adcp.webhooks import deliver, create_mcp_webhook_payload

response = await deliver(
    config,  # PushNotificationConfig or ReportingWebhook from the request
    create_mcp_webhook_payload(
        task_id=task_id, task_type="create_media_buy",
        status="completed", result=response_dict,
    ),
)
response.raise_for_status()
```

Notes:
- `deliver` hashes/signs the exact bytes it POSTs for HMAC-SHA256; for Bearer it attaches the credential as `Authorization`. Either way, the signer and the wire cannot disagree.
- `deliver` emits a `DeprecationWarning` on first use; migrate to `WebhookSender` for 4.0.
- If your buyer relies on `config.token` echo, pass `token_field="push_token"` (pick a name you and the receiver agree on — there is no spec-defined field name).
- In production pass a shared `httpx.AsyncClient` to `deliver` (or `client=` to `WebhookSender`) with a transport that blocks private/link-local IPs — the helper validates URL scheme but not egress destination.
- Retries: call `deliver` again with the same payload (deterministic serialization). For byte-identical HTTP envelopes including headers, use `WebhookSender.resend()`.

## SDK Quick Reference

**Response builders** (from `adcp.server.responses`):

| Function | Usage |
|----------|-------|
| `capabilities_response(protocols)` | `get_adcp_capabilities` response |
| `products_response(products)` | `get_products` response |
| `media_buy_response(id, packages, status=)` | `create_media_buy` success (auto-populates valid_actions) |
| `media_buys_response(media_buys)` | `get_media_buys` response |
| `update_media_buy_response(id, status=)` | `update_media_buy` success (auto-populates valid_actions) |
| `delivery_response(deliveries, reporting_period=)` | `get_media_buy_delivery` response |
| `sync_accounts_response(accounts)` | `sync_accounts` response |
| `sync_governance_response(accounts)` | `sync_governance` response |
| `creative_formats_response(formats)` | `list_creative_formats` response |
| `sync_creatives_response(creatives)` | `sync_creatives` response |

**DX helpers** (from `adcp.server`):

| Function | Usage |
|----------|-------|
| `adcp_error(code, message, field=, suggestion=)` | Structured error with auto-recovery |
| `cancel_media_buy_response(id, "buyer"/"seller")` | Cancellation with auto-defaults |
| `resolve_account(params, resolver)` | Account resolution with auto-error |
| `valid_actions_for_status(status)` | Status-to-actions mapping |
| `serve(handler, *, name=..., port=3001, transport="streamable-http"\|"a2a", test_controller=None)` | Start MCP or A2A server (default transport is `streamable-http`). Context passthrough is automatic. |

Import helpers from `adcp.server`. Import response builders from `adcp.server.responses`. Import types from `adcp.types`.

## Validation

**After writing the agent, validate it. Fix failures. Repeat.**

```bash
# agent.py ends with: serve(handler, port=3001)
python agent.py &
npx -y -p @adcp/client adcp storyboard run http://localhost:3001/mcp media_buy_seller --json
```

### What this skill covers (and what it doesn't)

This skill teaches the **9 core lifecycle scenarios** on the
`media_buy_seller` storyboard — the happy-path buyer journey from
discovery to delivery. Following the skill end-to-end produces an
agent that passes those 9 steps cleanly.

The full `media_buy_seller` storyboard runs ~40 scenarios across
multiple tracks. Expect `WARN: partial — 0 of 2 tracks passing`
against your skill-built agent: ~13 advanced scenarios require
stubs this skill doesn't teach. That's not a skill bug; it's a
scope choice. Add these independently when you need them:

| Track | Scenarios | What to add |
|---|---|---|
| Governance | `governance_denied`, `governance_override` | Wire a `GovernanceHandler` subclass (see `docs/handler-authoring.md`) |
| Creative lifecycle | `pending_creatives_to_start`, `pending_creatives_to_reject` | Extend your `sync_creatives` to return `status="pending"`, then flip via a `force_creative_status` test-controller scenario |
| Measurement | `measurement_terms_mismatch` | Validate `delivery_measurement` on `create_media_buy`; reject mismatched terms with `INVALID_REQUEST` |
| Targeting persistence | `inventory_list_targeting` | Persist targeting lists across `sync_creatives` / `get_media_buy_delivery` calls |
| State machine | `invalid_transition_*` | Return `INVALID_TRANSITION` from `update_media_buy` for disallowed state moves (e.g. `completed → paused`) |
| Delivery | `delivery_reporting/simulate_and_verify` | Wire `simulate_delivery` + `simulate_budget_spend` on your `TestControllerStore` |

**Keep iterating until the 9 core steps pass.** Advanced tracks are
additive — tackle them when your deployment actually needs the
behavior they validate.

## Storyboards

| Storyboard | Use case |
|-----------|----------|
| `media_buy_seller` | Full lifecycle — this skill teaches the 9 core steps; advanced tracks are optional additions above |
| `deterministic_testing` | Test controller state machine validation |
| `media_buy_non_guaranteed` | Auction flow with bid adjustment |
| `media_buy_guaranteed_approval` | IO approval workflow |

## Production Deployment

- Publish `.well-known/adagents.json` using the `adcp.adagents` module so buyer agents can discover capabilities.
- Wrap mutating tools (`create_media_buy`, `update_media_buy`, `sync_creatives`) with `adcp.server.idempotency.IdempotencyStore` to deduplicate retries.
- Sign outgoing webhooks with `adcp.webhooks` + `adcp.signing` so receivers can verify authenticity.
- Persist accounts, media buys, and creatives — in-memory dicts are fine for storyboards but lose state on restart.
- Front `serve()` with a process manager (systemd, Kubernetes) and terminate TLS upstream.

## Common Mistakes

| Mistake | Fix |
|---------|-----|
| Skip `get_adcp_capabilities` | Must be implemented — buyers call it first |
| Return raw dicts without builders | Use response builders from `adcp.server.responses` |
| Missing `brand`/`operator` in sync_accounts | Echo them back from the request |
| Not storing entities in memory | Test controller needs to find accounts, media buys, creatives |
| Wrong `delivery_response` signature | Takes `delivery_response(deliveries_list, reporting_period=...)`, not individual metrics |
| Missing `reporting_capabilities` on products | Required. Sub-fields: `available_metrics`, `available_reporting_frequencies`, `date_range_support`, `supports_webhooks` |
| `weekly` in `available_reporting_frequencies` | Only `hourly`, `daily`, `monthly` are valid |
| Missing `delivery_measurement.provider` | Required field — use `"internal"` or third-party provider name |

## Reference

This skill contains everything needed to build a 9/9 passing seller agent. The code blocks above are taken from a validated implementation.
