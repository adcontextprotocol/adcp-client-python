"""Credential-leak strip — every wire-emit boundary scrubs.

Audit findings #463 against PR #469: the typed projections
(:func:`adcp.decisioning.to_wire_account` etc.) were public-API
helpers but no framework code called them. This file is the regression
suite that proves the strip is now load-bearing on every echo path:

* H1 — synchronous return path through :func:`_invoke_platform_method`.
* H2 — sync-completion webhook (``maybe_emit_sync_completion``).
* H3 — :class:`TaskRegistry` persistence path (``registry.complete``
  + ``tasks/get`` echo).
* M1 — INTERNAL_ERROR ``caused_by`` omits exception ``str()`` so
  bearer-shaped repr can't leak via the error wire.
* M2 — ``adcp.server.responses._serialize`` strips loose-dict items
  that bypass typed projections.
* M3 — ``ctx_metadata`` fail-closed gate against credential-shaped
  keys (round-trip risk via the AdCP context-echo contract).

Each test exercises a distinct emit path so a regression in any one
seam fires its own diagnostic.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from typing import Any

import pytest
from pydantic import BaseModel

from adcp.decisioning import (
    Account,
    AdcpError,
    DecisioningCapabilities,
    DecisioningPlatform,
    InMemoryTaskRegistry,
    SingletonAccounts,
    TaskHandoffContext,
)
from adcp.decisioning.account_projection import (
    CREDENTIAL_BEARING_METHODS,
    strip_credentials_from_wire_result,
)
from adcp.decisioning.dispatch import (
    _build_request_context,
    _invoke_platform_method,
)
from adcp.decisioning.types import TaskHandoff
from adcp.decisioning.webhook_emit import maybe_emit_sync_completion
from adcp.server.base import ToolContext
from adcp.server.responses import sync_governance_response

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def executor() -> Any:
    pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="cred-leak-")
    yield pool
    pool.shutdown(wait=True)


_BEARER = "super-secret-bearer-token-abcdefghij1234567890"
_IBAN = "DE89370400440532013000"


def _governance_response_with_credentials() -> dict[str, Any]:
    """Wire-shape ``sync_governance`` response carrying credentials.

    Realistic adopter footgun: ``return {"accounts": [{**entry, ...}]}``
    spreads the input ``governance_agents`` (with ``authentication``)
    onto the response. Pydantic ``extra='allow'`` would let this
    through; the dispatcher must scrub regardless.
    """
    return {
        "accounts": [
            {
                "account": {"account_id": "acct_1"},
                "status": "synced",
                "governance_agents": [
                    {
                        "url": "https://gov.example.com/",
                        "categories": ["budget_authority"],
                        "authentication": {
                            "schemes": ["Bearer"],
                            "credentials": _BEARER,
                        },
                    },
                ],
            },
        ],
        "sandbox": True,
    }


def _accounts_response_with_bank() -> dict[str, Any]:
    """Wire-shape ``list_accounts`` response carrying bank coordinates."""
    return {
        "accounts": [
            {
                "account_id": "acct_1",
                "name": "Acme",
                "status": "active",
                "billing_entity": {
                    "legal_name": "Acme Inc.",
                    "bank": {"account_holder": "Acme Inc.", "iban": _IBAN},
                },
            }
        ],
        "sandbox": True,
    }


# ---------------------------------------------------------------------------
# H1 — synchronous return path through _invoke_platform_method
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dispatcher_strips_governance_credentials_from_sync_return(
    executor: ThreadPoolExecutor,
) -> None:
    """End-to-end: a platform method returns a loose dict carrying
    ``governance_agents[i].authentication.credentials``; the
    dispatcher's strip layer removes them before the wire response
    leaves the dispatcher.

    Without this strip, every typed-projection helper in
    :mod:`account_projection` is theatre — adopters returning
    Pydantic ``extra='allow'`` models or plain dicts bypass the typed
    path entirely.
    """

    class _LeakyPlatform(DecisioningPlatform):
        capabilities = DecisioningCapabilities()
        accounts = SingletonAccounts(account_id="acct_1")

        async def sync_governance(self, req: Any, ctx: Any) -> dict[str, Any]:
            return _governance_response_with_credentials()

    class _Req(BaseModel):
        pass

    ctx = _build_request_context(ToolContext(), Account(id="acct_1"), None)
    result = await _invoke_platform_method(
        _LeakyPlatform(),
        "sync_governance",
        _Req(),
        ctx,
        executor=executor,
        registry=InMemoryTaskRegistry(),
    )
    # The bearer MUST NOT appear anywhere in the wire response.
    assert _BEARER not in str(result)
    assert "authentication" not in str(result)
    # url + categories preserved.
    agent = result["accounts"][0]["governance_agents"][0]
    assert agent["url"] == "https://gov.example.com/"
    assert agent["categories"] == ["budget_authority"]


@pytest.mark.asyncio
async def test_dispatcher_strips_billing_entity_bank_from_sync_return(
    executor: ThreadPoolExecutor,
) -> None:
    """Same posture as the governance test, for the second write-only
    field the spec calls out: ``billing_entity.bank``."""

    class _BankLeakingPlatform(DecisioningPlatform):
        capabilities = DecisioningCapabilities()
        accounts = SingletonAccounts(account_id="acct_1")

        async def list_accounts(self, req: Any, ctx: Any) -> dict[str, Any]:
            return _accounts_response_with_bank()

    class _Req(BaseModel):
        pass

    ctx = _build_request_context(ToolContext(), Account(id="acct_1"), None)
    result = await _invoke_platform_method(
        _BankLeakingPlatform(),
        "list_accounts",
        _Req(),
        ctx,
        executor=executor,
        registry=InMemoryTaskRegistry(),
    )
    assert _IBAN not in str(result)
    assert "bank" not in result["accounts"][0]["billing_entity"]
    # legal_name preserved.
    assert result["accounts"][0]["billing_entity"]["legal_name"] == "Acme Inc."


@pytest.mark.asyncio
async def test_dispatcher_skip_strip_for_non_credential_methods(
    executor: ThreadPoolExecutor,
) -> None:
    """The strip is method-gated to avoid walking large product /
    signal catalogs that can't carry credentials. ``get_products``
    isn't in :data:`CREDENTIAL_BEARING_METHODS` — the result passes
    through unchanged.

    Defensive: confirms the gate doesn't accidentally include a
    high-traffic method that would pay an O(n) walk on every call."""

    assert "get_products" not in CREDENTIAL_BEARING_METHODS
    assert "get_signals" not in CREDENTIAL_BEARING_METHODS

    big_payload = {"products": [{"id": f"p{i}"} for i in range(1000)]}

    class _BigPlatform(DecisioningPlatform):
        capabilities = DecisioningCapabilities()
        accounts = SingletonAccounts(account_id="acct_1")

        async def get_products(self, req: Any, ctx: Any) -> dict[str, Any]:
            return big_payload

    class _Req(BaseModel):
        pass

    ctx = _build_request_context(ToolContext(), Account(id="acct_1"), None)
    result = await _invoke_platform_method(
        _BigPlatform(),
        "get_products",
        _Req(),
        ctx,
        executor=executor,
        registry=InMemoryTaskRegistry(),
    )
    # Pass-through (same object reference) when method is non-credential.
    assert result is big_payload


def test_strip_credentials_returns_input_unchanged_for_non_credential_method() -> None:
    """The unit-level boundary: non-credential method names short-
    circuit and return the original object reference (no defensive
    copy on the hot path)."""
    payload = {"products": [{"id": "p1"}]}
    assert strip_credentials_from_wire_result("get_products", payload) is payload


def test_strip_credentials_walks_recursively() -> None:
    """The scrubber walks recursively — a deeply nested
    ``governance_agents[i].authentication`` is stripped just as
    a top-level one."""
    nested = {
        "accounts": [
            {
                "wrapper": {
                    "deep": {
                        "governance_agents": [
                            {"url": "https://x", "authentication": {"credentials": _BEARER}},
                        ]
                    }
                }
            }
        ]
    }
    out = strip_credentials_from_wire_result("sync_governance", nested)
    assert _BEARER not in str(out)
    assert "authentication" not in str(out)


# ---------------------------------------------------------------------------
# H2 — sync-completion webhook
# ---------------------------------------------------------------------------


class _FakeWebhookSender:
    """Captures the ``send_mcp`` payload so tests can assert on it."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def send_mcp(self, **kwargs: Any) -> None:
        self.calls.append(kwargs)


@pytest.mark.asyncio
async def test_webhook_strips_credentials_before_buyer_callback() -> None:
    """The webhook re-emits the response payload to a buyer-controlled
    URL — :data:`SPEC_WEBHOOK_TASK_TYPES` includes ``sync_accounts``,
    so a leaky response would echo the bearer to the buyer's webhook
    server. Defense-in-depth: the strip runs in
    :func:`maybe_emit_sync_completion` regardless of upstream
    sanitization."""
    import asyncio

    sender = _FakeWebhookSender()

    class _Params(BaseModel):
        # Pydantic with arbitrary types so push_notification_config is
        # a plain dict on the test fixture.
        push_notification_config: dict[str, Any]

    leaky_result = _governance_response_with_credentials()
    params = _Params(
        push_notification_config={
            "url": "https://buyer.example.com/wh",
            "token": "tok",
        }
    )

    maybe_emit_sync_completion(
        sender=sender,  # type: ignore[arg-type]
        enabled=True,
        method_name="sync_accounts",  # spec-eligible webhook task type
        params=params,
        result=leaky_result,
    )
    # Drain the fire-and-forget task.
    await asyncio.sleep(0.01)
    for _ in range(20):
        if sender.calls:
            break
        await asyncio.sleep(0.01)

    assert sender.calls, "webhook never fired"
    payload = sender.calls[0]
    assert _BEARER not in str(payload), "bearer leaked to buyer webhook"
    assert "authentication" not in str(payload["result"])


# ---------------------------------------------------------------------------
# H3 — TaskRegistry persistence (durable + tasks/get echo)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_task_registry_persists_stripped_artifact(
    executor: ThreadPoolExecutor,
) -> None:
    """Durable registries (Postgres, Redis) write the artifact to disk
    in plaintext; even in-memory, ``tasks/get`` returns it verbatim.
    Strip BEFORE :meth:`TaskRegistry.complete` so the stored shape
    never carries credentials."""

    class _HandoffPlatform(DecisioningPlatform):
        capabilities = DecisioningCapabilities()
        accounts = SingletonAccounts(account_id="acct_1")

        async def sync_governance(self, req: Any, ctx: Any) -> Any:
            async def _handoff(handoff_ctx: TaskHandoffContext) -> dict[str, Any]:
                return _governance_response_with_credentials()

            return TaskHandoff(_handoff)

    class _Req(BaseModel):
        pass

    registry = InMemoryTaskRegistry()
    ctx = _build_request_context(ToolContext(), Account(id="acct_1"), None)
    submitted = await _invoke_platform_method(
        _HandoffPlatform(),
        "sync_governance",
        _Req(),
        ctx,
        executor=executor,
        registry=registry,
    )
    assert submitted["status"] == "submitted"
    task_id = submitted["task_id"]

    # Wait for the background handoff to complete.
    import asyncio

    persisted: dict[str, Any] | None = None
    for _ in range(50):
        persisted = await registry.get(task_id, expected_account_id="acct_1")
        if persisted is not None and persisted["state"] == "completed":
            break
        await asyncio.sleep(0.01)

    assert persisted is not None
    assert persisted["state"] == "completed"
    # The persisted result MUST NOT carry credentials. ``tasks/get``
    # returns the registry's ``to_dict()`` verbatim — what's persisted
    # is what the buyer sees.
    assert _BEARER not in str(persisted)
    assert "authentication" not in str(persisted)


# ---------------------------------------------------------------------------
# H3b — WorkflowHandoff direct-complete path (adopter calls
# ``registry.complete`` from external workflow, NOT through dispatcher)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_workflow_handoff_registry_complete_strips_credentials() -> None:
    """``WorkflowHandoff`` enqueues to an external system; the adopter's
    workflow later calls ``registry.complete(task_id, result)`` directly
    — the framework is NOT on the call stack at that point, so the
    dispatcher-level strip can't fire.

    The strip must happen inside :meth:`TaskRegistry.complete` so a
    credential-bearing dict written from the adopter's external workflow
    can't survive into ``tasks/get``.
    """
    registry = InMemoryTaskRegistry()
    task_id = await registry.issue(
        account_id="acct_1",
        task_type="sync_governance",
    )

    # Simulate the adopter's external workflow calling complete()
    # directly with a credential-bearing result. No framework code is
    # on the call stack here.
    leaky_result = _governance_response_with_credentials()
    await registry.complete(task_id, leaky_result)

    persisted = await registry.get(task_id, expected_account_id="acct_1")
    assert persisted is not None
    assert persisted["state"] == "completed"
    # Bearer MUST NOT survive into the persisted artifact — ``tasks/get``
    # would otherwise echo it.
    assert _BEARER not in str(persisted)
    assert "authentication" not in str(persisted)
    # Non-credential fields preserved.
    agent = persisted["result"]["accounts"][0]["governance_agents"][0]
    assert agent["url"] == "https://gov.example.com/"


@pytest.mark.asyncio
async def test_registry_complete_skips_strip_for_non_credential_method() -> None:
    """The strip is method-gated by ``record.task_type``. A non-credential
    method (``get_products``) skips the recursive walk — adopter-stashed
    extra keys pass through unchanged."""
    registry = InMemoryTaskRegistry()
    task_id = await registry.issue(
        account_id="acct_1",
        task_type="get_products",
    )
    # A ``get_products`` task can't carry account credentials in the
    # spec-shape result, so the gate skips the walk entirely. Confirm
    # no over-eager scrubbing of unrelated keys.
    big_payload: dict[str, Any] = {
        "products": [{"id": "p1", "extra_metadata": {"authentication": "literal-string"}}],
    }
    await registry.complete(task_id, big_payload)
    persisted = await registry.get(task_id, expected_account_id="acct_1")
    assert persisted is not None
    # ``authentication`` here is a product-level metadata field, not a
    # governance-agent credential — must pass through unchanged.
    product_auth = persisted["result"]["products"][0]["extra_metadata"]["authentication"]
    assert product_auth == "literal-string"


# ---------------------------------------------------------------------------
# M1 — INTERNAL_ERROR caused_by omits exception str()
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_internal_error_caused_by_drops_message_field(
    executor: ThreadPoolExecutor,
) -> None:
    """An adopter exception whose ``str()`` carries a bearer-shaped
    token must not leak through ``details.caused_by.message``. The
    field is now omitted entirely; only ``caused_by.type`` (the
    exception class name) survives — enough for triage, too narrow
    to fit a credential."""

    class _CredentialLeakingPlatform(DecisioningPlatform):
        capabilities = DecisioningCapabilities()
        accounts = SingletonAccounts(account_id="acct_1")

        async def get_products(self, req: Any, ctx: Any) -> Any:
            # Realistic shape: an adopter's HTTP client wraps the
            # request URL + Authorization header into the exception
            # message on connection failure.
            raise ConnectionError(f"POST upstream failed: Authorization=Bearer {_BEARER}")

    class _Req(BaseModel):
        pass

    ctx = _build_request_context(ToolContext(), Account(id="acct_1"), None)
    with pytest.raises(AdcpError) as exc_info:
        await _invoke_platform_method(
            _CredentialLeakingPlatform(),
            "get_products",
            _Req(),
            ctx,
            executor=executor,
            registry=InMemoryTaskRegistry(),
        )
    err = exc_info.value
    assert err.code == "INTERNAL_ERROR"
    # caused_by carries ONLY ``type``.
    assert err.details["caused_by"] == {"type": "ConnectionError"}
    assert "message" not in err.details["caused_by"]
    # Bearer must not appear ANYWHERE on the wire envelope. The wire
    # ``message`` is propagated from the adopter exception class name
    # only (`_internal_error_message` builds it), not the exception
    # ``str()``.
    assert _BEARER not in str(err.details)
    assert _BEARER not in str(err.args[0] if err.args else "")


# ---------------------------------------------------------------------------
# M2 — adcp.server.responses._serialize strips loose dicts
# ---------------------------------------------------------------------------


def test_response_builder_strips_governance_credentials_from_loose_dicts() -> None:
    """Adopters hand-building responses via
    :mod:`adcp.server.responses` builders pass through ``_serialize``,
    which previously short-circuited dict items unchanged. A loose
    dict carrying ``governance_agents[i].authentication`` would
    smuggle credentials through. The builder layer now scrubs these
    keys recursively, mirroring the dispatcher-level strip."""
    from adcp.server.responses import _serialize

    items = [
        {
            "account": {"account_id": "acct_1"},
            "status": "synced",
            "governance_agents": [
                {
                    "url": "https://gov.example.com/",
                    "authentication": {
                        "schemes": ["Bearer"],
                        "credentials": _BEARER,
                    },
                }
            ],
        }
    ]
    serialized = _serialize(items)
    assert _BEARER not in str(serialized)
    assert "authentication" not in str(serialized[0]["governance_agents"][0])
    assert serialized[0]["governance_agents"][0]["url"] == "https://gov.example.com/"


def test_response_builder_strips_billing_entity_bank() -> None:
    """Bank coordinates the spec marks write-only must not echo via
    builder output."""
    from adcp.server.responses import _serialize

    items = [
        {
            "account_id": "acct_1",
            "name": "Acme",
            "billing_entity": {
                "legal_name": "Acme Inc.",
                "bank": {"iban": _IBAN},
            },
        }
    ]
    serialized = _serialize(items)
    assert _IBAN not in str(serialized)
    assert "bank" not in serialized[0]["billing_entity"]
    assert serialized[0]["billing_entity"]["legal_name"] == "Acme Inc."


def test_sync_governance_response_builder_round_trip_strip() -> None:
    """Cross-builder regression: the public ``sync_governance_response``
    helper is what storyboards / hello-world adopters call. The
    builder routes items through ``_serialize`` so a credential-bearing
    item dict is scrubbed before the response leaves the seller.
    """
    response = sync_governance_response(
        accounts=[
            {
                "account": {"account_id": "acct_1"},
                "status": "synced",
                "governance_agents": [
                    {
                        "url": "https://gov.example.com/",
                        "authentication": {
                            "schemes": ["Bearer"],
                            "credentials": _BEARER,
                        },
                    }
                ],
            }
        ],
    )
    assert _BEARER not in str(response)
    agent = response["accounts"][0]["governance_agents"][0]
    assert "authentication" not in agent
    assert agent["url"] == "https://gov.example.com/"


def test_sync_accounts_response_builder_round_trip_strip() -> None:
    """Cross-builder regression: ``sync_accounts_response`` is the
    other governance-adjacent builder that surfaces ``Account``
    envelopes. Loose-dict adopters spreading ``billing_entity`` (with
    ``bank``) onto the response must not leak bank coordinates."""
    from adcp.server.responses import sync_accounts_response

    response = sync_accounts_response(
        accounts=[
            {
                "account_id": "acct_1",
                "brand": "Acme",
                "action": "created",
                "status": "active",
                "billing_entity": {
                    "legal_name": "Acme Inc.",
                    "bank": {"iban": _IBAN},
                },
                "governance_agents": [
                    {
                        "url": "https://gov.example.com/",
                        "authentication": {
                            "schemes": ["Bearer"],
                            "credentials": _BEARER,
                        },
                    }
                ],
            }
        ],
    )
    serialized = response["accounts"][0]
    assert _IBAN not in str(response)
    assert _BEARER not in str(response)
    assert "bank" not in serialized["billing_entity"]
    assert "authentication" not in serialized["governance_agents"][0]
    assert serialized["billing_entity"]["legal_name"] == "Acme Inc."


# ---------------------------------------------------------------------------
# M3 — ctx_metadata fail-closed gate
# ---------------------------------------------------------------------------


def test_ctx_metadata_rejects_credential_shaped_keys() -> None:
    """An adopter who shoves a credential into ``ctx.metadata`` would
    discover the value round-trips into responses (the AdCP spec
    echoes context). The gate fails fast at
    :func:`_build_request_context` with a clear pointer to
    :class:`AuthInfo.credential`."""
    from adcp.decisioning.dispatch import _build_request_context

    tool_ctx = ToolContext(metadata={"upstream.api_token": "secret"})
    with pytest.raises(ValueError) as exc_info:
        _build_request_context(tool_ctx, Account(id="x"), None)
    msg = str(exc_info.value)
    assert "credential-shaped" in msg
    assert "upstream.api_token" in msg
    assert "AuthInfo.credential" in msg


def test_ctx_metadata_rejects_nested_credential_keys() -> None:
    """Sub-keys at any nesting depth count — a buyer-supplied
    ``{"upstream": {"api_token": "..."}}`` is rejected the same as
    a flat key. The diagnostic walks the adopter to the offending
    path."""
    from adcp.decisioning.dispatch import _build_request_context

    tool_ctx = ToolContext(metadata={"upstream": {"api_token": "secret"}})
    with pytest.raises(ValueError) as exc_info:
        _build_request_context(tool_ctx, Account(id="x"), None)
    msg = str(exc_info.value)
    assert "upstream" in msg
    assert "api_token" in msg


def test_ctx_metadata_allows_non_credential_keys() -> None:
    """Non-credential keys (correlation ids, feature flags, trace
    ids) pass through unchanged — the gate is targeted, not blanket
    metadata blocking."""
    from adcp.decisioning.dispatch import _build_request_context

    tool_ctx = ToolContext(
        metadata={
            "correlation_id": "req_abc123",
            "feature_flag.beta_pricing": True,
            "trace_id": "t_xyz",
        }
    )
    ctx = _build_request_context(tool_ctx, Account(id="x"), None)
    assert ctx.metadata["correlation_id"] == "req_abc123"
    assert ctx.metadata["feature_flag.beta_pricing"] is True


@pytest.mark.parametrize(
    "key",
    [
        "credential",
        "my.credentials",
        "auth_token",
        "client_secret",
        "x.api_key",
        "ApiKey",
        "BEARER",
        "user.password",
    ],
)
def test_ctx_metadata_credential_suffix_match_is_case_insensitive(key: str) -> None:
    """The suffix match handles common adopter naming variants —
    ``ApiKey`` / ``API_KEY`` / ``api_key`` all trip the gate."""
    from adcp.decisioning.dispatch import _build_request_context

    tool_ctx = ToolContext(metadata={key: "value"})
    with pytest.raises(ValueError):
        _build_request_context(tool_ctx, Account(id="x"), None)


def test_ctx_metadata_rejects_credentials_in_list_of_dicts() -> None:
    """``ctx.metadata`` with a list of config dicts is a realistic
    shape — adopters batch upstream client configs that way. The
    gate must walk into list items so a credential buried in any
    element trips the same rejection as a top-level key."""
    from adcp.decisioning.dispatch import _build_request_context

    tool_ctx = ToolContext(
        metadata={"upstream_configs": [{"name": "primary"}, {"api_token": "secret"}]}
    )
    with pytest.raises(ValueError) as exc_info:
        _build_request_context(tool_ctx, Account(id="x"), None)
    msg = str(exc_info.value)
    assert "upstream_configs" in msg
    assert "api_token" in msg


def test_ctx_metadata_rejects_credentials_in_nested_lists() -> None:
    """Nested lists (``[[{...}]]``) walk recursively — the gate is
    not depth-limited, so an adversarial adopter shape can't smuggle a
    credential past it via list-wrapping."""
    from adcp.decisioning.dispatch import _build_request_context

    tool_ctx = ToolContext(metadata={"groups": [[{"client_secret": "leak"}]]})
    with pytest.raises(ValueError) as exc_info:
        _build_request_context(tool_ctx, Account(id="x"), None)
    msg = str(exc_info.value)
    assert "groups" in msg
    assert "client_secret" in msg


def test_ctx_metadata_allows_list_of_strings() -> None:
    """A plain list of strings (tags, correlation chain) carries no
    dict to inspect — the gate must not raise on a benign list shape."""
    from adcp.decisioning.dispatch import _build_request_context

    tool_ctx = ToolContext(
        metadata={"trace_chain": ["span_a", "span_b", "span_c"], "tags": ["beta"]}
    )
    ctx = _build_request_context(tool_ctx, Account(id="x"), None)
    assert ctx.metadata["trace_chain"] == ["span_a", "span_b", "span_c"]


def test_ctx_metadata_allows_list_of_benign_dicts() -> None:
    """A list of dicts whose keys are NOT credential-shaped passes
    through unchanged — adopters batch config dicts through metadata
    without leaking credentials."""
    from adcp.decisioning.dispatch import _build_request_context

    tool_ctx = ToolContext(
        metadata={
            "upstream_configs": [
                {"name": "primary", "region": "us-east-1"},
                {"name": "secondary", "region": "eu-west-1"},
            ]
        }
    )
    ctx = _build_request_context(tool_ctx, Account(id="x"), None)
    assert ctx.metadata["upstream_configs"][0]["name"] == "primary"
