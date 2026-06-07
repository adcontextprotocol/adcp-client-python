# Testing Your AdCP Server

You build an AdCP seller on `adcp.server` / `adcp.decisioning`. This guide
covers the two testing paths the SDK supports:

1. **In-process testing** with the SDK harness — fast unit and integration
   tests that exercise your handlers, error translation, and wire envelope
   without booting a network server.
2. **Compliance scenario grading** with the `@adcp/sdk` storyboard runner —
   the authoritative pass/fail judgement of whether your server conforms to an
   AdCP scenario, run against your live server.

Use both. The in-process harness gives you tight per-handler feedback in your
own `pytest` suite. The storyboard runner is the single source of truth for
compliance — the canonical scenarios and the grader live in the JS `@adcp/sdk`
package, and that runner is what CI grades against.

---

## Path 1: In-process testing with the SDK harness

`adcp.testing` provides in-process clients that invoke your platform through
the real handler-dispatch and error-translation stack — no HTTP, no port, no
SSE framing. There are two, one per transport:

- `SellerTestClient.invoke(tool, payload)` — MCP dispatch.
- `SellerA2AClient.invoke(skill, payload)` — A2A executor + event-queue
  dispatch.

Both take a `DecisioningPlatform`, both return a `ToolInvokeResult`, and both
share the same assertion surface — so a single test body can cover both
transports.

### A minimal test

```python
import pytest

from adcp.testing import SellerTestClient


@pytest.fixture
def seller():
    return SellerTestClient(MySeller())


async def test_get_products_succeeds(seller):
    result = await seller.invoke("get_products", {"buying_mode": "brief"})
    assert result.ok
    assert "products" in result.data
```

`MySeller` is your `DecisioningPlatform` subclass. `invoke` is async, so your
tests are `async def` (the SDK ships a `pytest-asyncio` config; adopters using
their own runner should enable async test collection).

The payload is the AdCP request for that tool. The harness runs your tool's
request schema before your handler sees it, so an incomplete payload surfaces
as an `INVALID_REQUEST` error rather than reaching your handler — pass a
complete, valid request when you want to exercise handler logic.

### Reading the result

`invoke` returns a `ToolInvokeResult`:

| Field | Type | Meaning |
|---|---|---|
| `.ok` | `bool` | `True` when the tool returned a success envelope (no `adcp_error`). |
| `.data` | `dict \| None` | The success payload. `None` on error. |
| `.adcp_error` | `AdcpErrorPayload \| None` | The structured error. `None` on success. |
| `.structured_content` | `dict` | The raw structured content, success or error. |

`AdcpErrorPayload` carries the AdCP transport-error fields: `code`, `message`,
and the optionals `recovery`, `field`, `suggestion`, `retry_after`, `details`.
When your handler raises an `AdcpError`, the harness extracts it onto
`.adcp_error` so you can pin the error contract:

```python
from adcp.decisioning.types import AdcpError


class FloorEnforcingSeller(MySeller):
    def create_media_buy(self, req, ctx):
        raise AdcpError(
            "BUDGET_TOO_LOW",
            message="below floor",
            recovery="correctable",
            field="total_budget",
        )


async def test_budget_below_floor():
    seller = SellerTestClient(FloorEnforcingSeller())
    result = await seller.invoke("create_media_buy", valid_create_media_buy_payload())
    assert not result.ok
    assert result.adcp_error.code == "BUDGET_TOO_LOW"
    assert result.adcp_error.recovery == "correctable"
    assert result.adcp_error.field == "total_budget"
```

### Testing both transports with one assertion body

`SellerA2AClient` has the same call shape and return type, so parametrize the
client fixture to run one test across MCP and A2A:

```python
from adcp.testing import SellerA2AClient, SellerTestClient


@pytest.fixture(params=["mcp", "a2a"])
def seller(request):
    platform = MySeller()
    if request.param == "mcp":
        return SellerTestClient(platform)
    return SellerA2AClient(platform)


async def test_get_products_succeeds_on_both_transports(seller):
    result = await seller.invoke("get_products", {"buying_mode": "brief"})
    assert result.ok
    assert "products" in result.data
```

For MCP, `invoke`'s first argument is the tool name; for A2A it is the skill
name. AdCP tool and skill names are identical, so the same string works for
both.

### Schema validation

Both clients accept a `validation` argument. The default (`None`) disables
schema validation so a test focuses on handler behavior. To exercise the same
request/response schema checks your production server runs, pass the server
default:

```python
from adcp.validation.client_hooks import SERVER_DEFAULT_VALIDATION

seller = SellerTestClient(MySeller(), validation=SERVER_DEFAULT_VALIDATION)
```

### HTTP-level tests

The clients above skip the HTTP layer. To test auth middleware, CORS, request
size limits, or anything that lives in the ASGI stack, use `build_test_client`
— an async context manager that mounts your platform's full ASGI app over
`httpx.ASGITransport` (no port bind) and yields an `httpx.AsyncClient`:

```python
from adcp.testing import build_test_client
from adcp.server.auth import BearerTokenAuth, Principal, validator_from_token_map


async def test_unauthenticated_request_rejected():
    auth = BearerTokenAuth(
        validate_token=validator_from_token_map(
            {"tok_test": Principal(caller_identity="agent.example.com", tenant_id="acme")}
        )
    )
    async with build_test_client(MySeller(), auth=auth) as client:
        resp = await client.post("/mcp/", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        assert resp.status_code == 401
```

`build_test_client` requires `asgi-lifespan` (included in `adcp[dev]`). Its
sibling `build_asgi_app` returns the bare ASGI app if you want to drive it with
`starlette.testclient.TestClient` or another harness.

### What the harness does not do

The harness does not grade compliance scenarios. There is no native
`run_scenario()` and one is not planned — the canonical scenarios and grader
live in `@adcp/sdk`, and duplicating that grader in Python would only drift.
For full scenario grading, use Path 2.

---

## Path 2: Compliance scenario grading with the storyboard runner

The AdCP compliance scenarios ("storyboards") and the grader that judges them
are maintained in the JS `@adcp/sdk` package. They are the single source of
truth for whether a server conforms. The runner drives your live server over
the wire, walks a scenario step by step, and emits a pass/fail report.

This is the same runner CI uses. The Python SDK does not reimplement it; you
invoke it through `npx`.

### Run a storyboard against your server

Boot your server, then point the runner at its MCP endpoint:

```bash
# Boot your server (binds port 3001 by default).
python agent.py &

# Grade it against the media_buy_seller storyboard.
npx @adcp/sdk storyboard run http://localhost:3001/mcp media_buy_seller --json
```

The third argument is the storyboard name. Pick the one that matches your
agent:

| Agent type | Storyboard |
|---|---|
| Seller (publisher, SSP, retail media) | `media_buy_seller` |
| Signals (audience data, CDP) | `signal_owned`, `signal_marketplace` |
| Creative (ad server, CMP) | `creative_lifecycle` |

`--json` emits a machine-readable result whose top-level `overall_status` is
`"passing"` when every step passes. Drop `--json` for human-readable output.

### Forcing state transitions

Compliance scenarios drive your server through state transitions (a media buy
moving from pending to active, a creative being approved). Wire a
`TestControllerStore` so the runner can force those transitions:

```python
from adcp.server import serve
from adcp.server.test_controller import TestControllerStore

serve(MySeller(), name="my-seller", test_controller=MyStore())
```

Without a test controller the runner cannot advance scenarios past the first
state gate.

### Wiring it into CI

The repository's `scripts/ci/run_storyboard_reference_seller.sh` is the
reference for a CI job: it installs `@adcp/sdk`, boots `examples/seller_agent.py`,
runs the storyboard, and asserts the result. The grading invocation it uses is:

```bash
adcp storyboard run \
  "http://127.0.0.1:${ADCP_PORT}/mcp" media_buy_seller \
  --json --allow-http \
  >"$STORYBOARD_RESULT_PATH"
```

`--allow-http` lets the runner talk to a plaintext loopback address, which a CI
server binds. The script then asserts `overall_status == "passing"` and
`controller_detected == true`; mirror those two checks in your own pipeline.

Copy that script as the starting point for grading your own server: swap
`examples/seller_agent.py` for your server's entry point and the storyboard
name for the one that matches your agent type.

---

## Which path when

| You want to... | Use |
|---|---|
| Assert a handler returns the right success payload | `SellerTestClient.invoke` |
| Assert a handler raises the right `adcp_error` code | `SellerTestClient.invoke` / `SellerA2AClient.invoke` |
| Cover both MCP and A2A from one test | Parametrize over both clients |
| Test auth, CORS, or request-size middleware | `build_test_client` |
| Know whether your server passes AdCP compliance | `storyboard run` |
| Gate merges on compliance in CI | `storyboard run`, per `run_storyboard_reference_seller.sh` |

---

## See Also

- [Handler authoring](handler-authoring.md) — building the `adcp.server` agent you are testing.
- [SDK testing guide](testing-guide.md) — testing buyer-side code that consumes the SDK.
- [`scripts/ci/run_storyboard_reference_seller.sh`](../scripts/ci/run_storyboard_reference_seller.sh) — the reference CI storyboard job.
- [AdCP Protocol Specification](https://adcontextprotocol.org/) — the spec the storyboards grade against.
