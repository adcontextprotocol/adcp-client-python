"""Account v3 wire-aligned fields + AccountStore ctx threading.

Covers two paired changes:

* :class:`Account[TMeta]` and :class:`SyncAccountsResultRow` carry
  optional wire-aligned fields (``billing_entity``, ``setup``,
  ``governance_agents``, ``account_scope``, ``payment_terms``,
  ``credit_limit``, ``rate_card``, ``reporting_bucket``).
* The wire-emit projections (``to_wire_account``,
  ``to_wire_sync_accounts_row``, ``to_wire_sync_governance_row``)
  apply two write-only strips per spec: ``billing_entity.bank`` and
  ``governance_agents[i].authentication.credentials``.
* :class:`AccountStore.upsert` / ``.list`` / ``.sync_governance``
  receive an optional :class:`ResolveContext` carrying ``auth_info``,
  ``tool_name``, and the resolved ``BuyerAgent``. The dispatch helper
  ``_call_with_optional_ctx`` probes adopter signatures so pre-ctx
  impls keep working.
"""

from __future__ import annotations

import asyncio
from dataclasses import fields
from typing import Any

import pytest

from adcp.decisioning import (
    Account,
    AdcpError,
    ApiKeyCredential,
    AuthInfo,
    BuyerAgent,
    ResolveContext,
    SyncAccountsResultRow,
    SyncGovernanceEntry,
    SyncGovernanceResultRow,
    to_wire_account,
    to_wire_sync_accounts_row,
    to_wire_sync_governance_row,
)
from adcp.decisioning.accounts import _call_with_optional_ctx
from adcp.types import (
    AccountReference,
    AccountScope,
    Authentication,
    BusinessEntity,
    CreditLimit,
    GovernanceAgent,
    PaymentTerms,
    ReportingBucket,
    Setup,
)

# Tests verify both the request-side GovernanceAgent (carries
# authentication credentials, exported via adcp.types as
# `GovernanceAgent`) and the response-side variant (no authentication
# field). Importing the response-side type explicitly so the
# schema-shape regression test can assert the input/output split.
from adcp.types.generated_poc.core.account import (
    GovernanceAgent as ResponseGovernanceAgent,
)

# Address and Bank are sub-models of BusinessEntity. The codegen places
# them on the BusinessEntity-owning module; pulling from generated_poc
# is acceptable in tests (the type-import layering rule applies to
# src/, not tests/).
from adcp.types.generated_poc.core.business_entity import Address, Bank

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _bank_fixture() -> Bank:
    return Bank(
        account_holder="Acme Inc.",
        iban="DE89370400440532013000",
        bic="DEUTDEFF",
    )


def _entity_fixture(*, with_bank: bool = True) -> BusinessEntity:
    return BusinessEntity(
        legal_name="Acme Inc.",
        vat_id="DE123456789",
        address=Address(
            street="123 Main",
            city="Berlin",
            postal_code="10115",
            country="DE",
        ),
        bank=_bank_fixture() if with_bank else None,
    )


def _populated_account() -> Account[Any]:
    # GovernanceAgent (the publicly-exported, request-side variant)
    # requires `authentication`. The framework's wire-emit projection
    # strips the field on response, but adopters constructing the
    # typed model still need to populate it. Real adopters carry the
    # credentials through from the buyer's `sync_governance` call;
    # the test fixture constructs a dummy bearer for shape coverage.
    return Account(
        id="acct_1",
        name="Acme",
        status="active",
        billing_entity=_entity_fixture(),
        setup=Setup(
            url="https://example.com/setup",
            message="Complete credit application",
        ),
        governance_agents=[
            GovernanceAgent(
                url="https://gov.example.com/",
                authentication=Authentication(
                    schemes=["Bearer"],
                    credentials="x" * 32,
                ),
            ),
        ],
        account_scope=AccountScope.brand,
        payment_terms=PaymentTerms.net_30,
        credit_limit=CreditLimit(amount=100_000.0, currency="USD"),
        rate_card="enterprise-2026",
        reporting_bucket=ReportingBucket(
            protocol="s3",
            bucket="acme-reporting-bucket",
            file_retention_days=30,
        ),
    )


# ---------------------------------------------------------------------------
# Account dataclass shape
# ---------------------------------------------------------------------------


def test_account_carries_wire_aligned_optional_fields() -> None:
    """The new optional fields are present on the dataclass and
    default to ``None`` so adopters who don't set them see no
    behavior change."""
    field_names = {f.name for f in fields(Account)}
    expected = {
        "billing_entity",
        "setup",
        "governance_agents",
        "account_scope",
        "payment_terms",
        "credit_limit",
        "rate_card",
        "reporting_bucket",
    }
    missing = expected - field_names
    assert not missing, f"Account missing wire fields: {missing}"

    # Defaults are None — backwards-compatible with adopters that
    # construct ``Account(id=...)`` and don't populate the new fields.
    bare = Account(id="x")
    for name in expected:
        assert getattr(bare, name) is None, f"{name} should default to None"


def test_sync_accounts_result_row_carries_wire_aligned_fields() -> None:
    field_names = {f.name for f in fields(SyncAccountsResultRow)}
    expected = {
        "billing_entity",
        "setup",
        "account_scope",
        "rate_card",
        "payment_terms",
        "credit_limit",
        "billing",
        "errors",
        "warnings",
        "sandbox",
    }
    missing = expected - field_names
    assert not missing, f"SyncAccountsResultRow missing wire fields: {missing}"


# ---------------------------------------------------------------------------
# to_wire_account — projection + write-only strips
# ---------------------------------------------------------------------------


def test_to_wire_account_projects_each_new_field() -> None:
    wire = to_wire_account(_populated_account())

    assert wire["account_id"] == "acct_1"
    assert wire["name"] == "Acme"
    assert wire["status"] == "active"
    assert "billing_entity" in wire
    assert wire["billing_entity"]["legal_name"] == "Acme Inc."
    assert "setup" in wire
    assert wire["setup"]["message"] == "Complete credit application"
    assert "governance_agents" in wire
    assert wire["governance_agents"][0]["url"].rstrip("/") == "https://gov.example.com"
    assert wire["account_scope"] == "brand"
    assert wire["payment_terms"] == "net_30"
    assert wire["credit_limit"] == {"amount": 100_000.0, "currency": "USD"}
    assert wire["rate_card"] == "enterprise-2026"
    assert wire["reporting_bucket"]["bucket"] == "acme-reporting-bucket"


def test_to_wire_account_strips_billing_entity_bank() -> None:
    """Spec: BusinessEntity.bank is write-only — MUST NOT be echoed
    in any response payload."""
    wire = to_wire_account(_populated_account())
    assert "bank" not in wire["billing_entity"]
    # Other fields preserved.
    assert wire["billing_entity"]["legal_name"] == "Acme Inc."
    assert wire["billing_entity"]["vat_id"] == "DE123456789"


def test_to_wire_account_strips_governance_agent_authentication() -> None:
    """Spec + defense-in-depth: governance_agents[].authentication
    carries write-only credentials. Even if an adopter smuggles them
    through a loosely-typed dict, the projection drops them at the
    emit boundary.

    Python type hints aren't enforced at runtime — same posture as
    the JS-side TypeScript-erasure rationale."""
    smuggled_agent = {
        "url": "https://gov.example.com/",
        "categories": ["budget_authority"],
        "authentication": {
            "schemes": ["Bearer"],
            "credentials": "super-secret-bearer-token",
        },
    }
    account = Account(
        id="acct_1",
        name="Acme",
        status="active",
        # Loose dict — not the typed GovernanceAgent. Adopter could
        # have passed any shape.
        governance_agents=[smuggled_agent],  # type: ignore[list-item]
    )
    wire = to_wire_account(account)
    assert "authentication" not in wire["governance_agents"][0]
    assert "credentials" not in str(wire["governance_agents"][0])
    # url + categories preserved.
    assert wire["governance_agents"][0]["url"] == "https://gov.example.com/"
    assert wire["governance_agents"][0]["categories"] == ["budget_authority"]


def test_to_wire_account_omits_billing_entity_when_only_bank() -> None:
    """When billing_entity carries only bank (no other fields), the
    projection omits the entire entity rather than emitting an empty
    object — BusinessEntity requires legal_name per the wire schema,
    so the empty case would fail downstream validation."""
    bank_only = {"bank": {"account_holder": "Acme", "iban": "DE89370400440532013000"}}
    account = Account(
        id="acct_1",
        name="Acme",
        status="active",
        billing_entity=bank_only,  # type: ignore[arg-type]
    )
    wire = to_wire_account(account)
    assert "billing_entity" not in wire


def test_to_wire_account_omits_unset_optional_fields() -> None:
    """Adopters who don't populate the new fields see a wire payload
    that only carries the required + populated fields — no spurious
    null keys."""
    bare = Account(id="acct_x", name="X", status="active")
    wire = to_wire_account(bare)
    assert wire == {"account_id": "acct_x", "name": "X", "status": "active"}


# ---------------------------------------------------------------------------
# to_wire_sync_accounts_row — same write-only strips
# ---------------------------------------------------------------------------


def test_to_wire_sync_accounts_row_projects_optional_fields() -> None:
    row = SyncAccountsResultRow(
        brand={"domain": "acme.com"},
        operator="acme.com",
        action="created",
        status="pending_approval",
        account_id="acct_new",
        name="Acme",
        billing="operator",
        billing_entity=_entity_fixture(),
        setup=Setup(message="Sign agreement"),
        account_scope=AccountScope.brand,
        rate_card="standard",
        payment_terms=PaymentTerms.net_30,
        credit_limit=CreditLimit(amount=50_000.0, currency="USD"),
    )
    wire = to_wire_sync_accounts_row(row)
    assert wire["action"] == "created"
    assert wire["status"] == "pending_approval"
    assert wire["account_id"] == "acct_new"
    assert wire["name"] == "Acme"
    assert wire["billing"] == "operator"
    assert wire["account_scope"] == "brand"
    assert wire["payment_terms"] == "net_30"
    assert wire["setup"]["message"] == "Sign agreement"
    assert wire["credit_limit"] == {"amount": 50_000.0, "currency": "USD"}


def test_to_wire_sync_accounts_row_strips_billing_entity_bank() -> None:
    """Regression: adopters returning a row that spreads a DB record
    with bank populated (e.g., {**db.find(brand), 'action': 'updated'})
    don't leak bank coordinates."""
    row = SyncAccountsResultRow(
        brand={"domain": "acme.com"},
        operator="acme.com",
        action="updated",
        status="active",
        billing_entity=_entity_fixture(),
    )
    wire = to_wire_sync_accounts_row(row)
    assert "bank" not in wire["billing_entity"]
    assert wire["billing_entity"]["legal_name"] == "Acme Inc."


def test_to_wire_sync_accounts_row_omits_billing_entity_when_only_bank() -> None:
    row = SyncAccountsResultRow(
        brand={"domain": "acme.com"},
        operator="acme.com",
        action="updated",
        status="active",
        billing_entity={"bank": {"account_holder": "Acme"}},  # type: ignore[arg-type]
    )
    wire = to_wire_sync_accounts_row(row)
    assert "billing_entity" not in wire


# ---------------------------------------------------------------------------
# to_wire_sync_governance_row — credentials strip
# ---------------------------------------------------------------------------


def test_to_wire_sync_governance_row_strips_authentication() -> None:
    """Each governance_agents[i].authentication is the write-only
    credential the seller persists for outbound check_governance
    calls. The framework strips it at the wire boundary so it never
    reaches the buyer OR the idempotency replay cache."""
    row = SyncGovernanceResultRow(
        account=AccountReference(root={"account_id": "acct_1"}),
        status="synced",
        governance_agents=[
            {
                "url": "https://gov.example.com/",
                "categories": ["budget_authority"],
                "authentication": {
                    "schemes": ["Bearer"],
                    "credentials": "super-secret-bearer-token-1234567890",
                },
            },
        ],
    )
    wire = to_wire_sync_governance_row(row)
    assert "authentication" not in wire["governance_agents"][0]
    assert "credentials" not in str(wire["governance_agents"][0])
    # url + categories preserved.
    assert wire["governance_agents"][0]["url"] == "https://gov.example.com/"
    assert wire["governance_agents"][0]["categories"] == ["budget_authority"]


def test_to_wire_sync_governance_row_handles_empty_clear() -> None:
    """An entry whose governance_agents is empty clears the binding
    for that account (replace semantics per spec)."""
    row = SyncGovernanceResultRow(
        account=AccountReference(root={"account_id": "acct_1"}),
        status="synced",
        governance_agents=[],
    )
    wire = to_wire_sync_governance_row(row)
    assert wire["governance_agents"] == []
    assert wire["status"] == "synced"


def test_to_wire_sync_governance_row_per_entry_failure() -> None:
    """Per-entry rejection (not operation-level throw) so a single
    bad entry doesn't fail the whole batch."""
    row = SyncGovernanceResultRow(
        account=AccountReference(root={"account_id": "acct_1"}),
        status="failed",
        errors=[
            {
                "code": "PERMISSION_DENIED",
                "message": "Account not in caller's tenant",
            }
        ],
    )
    wire = to_wire_sync_governance_row(row)
    assert wire["status"] == "failed"
    assert wire["errors"][0]["code"] == "PERMISSION_DENIED"
    assert "governance_agents" not in wire


# ---------------------------------------------------------------------------
# AccountStore ctx threading + backwards-compat
# ---------------------------------------------------------------------------


def test_resolve_context_carries_auth_tool_agent() -> None:
    """ResolveContext bundles the same shape already threaded to
    accounts.resolve so adopters can implement principal-keyed gates
    without re-deriving identity from the request."""
    auth = AuthInfo(
        kind="api_key",
        key_id="kid_1",
        principal="agent.example.com",
        credential=ApiKeyCredential(kind="api_key", key_id="kid_1"),
    )
    agent = BuyerAgent(
        agent_url="https://buyer.example.com",
        display_name="Buyer",
        status="active",
    )
    ctx = ResolveContext(
        auth_info=auth,
        tool_name="sync_accounts",
        agent=agent,
    )
    assert ctx.auth_info is auth
    assert ctx.tool_name == "sync_accounts"
    assert ctx.agent is agent


def test_call_with_optional_ctx_threads_ctx_when_signature_accepts() -> None:
    """Adopter impl with a ``ctx`` parameter receives the
    ResolveContext."""
    received: dict[str, Any] = {}

    def upsert(refs: list[Any], ctx: ResolveContext | None = None) -> str:
        received["refs"] = refs
        received["ctx"] = ctx
        return "with-ctx"

    ctx = ResolveContext(tool_name="sync_accounts")
    out = _call_with_optional_ctx(upsert, ["ref1"], ctx=ctx)
    assert out == "with-ctx"
    assert received["refs"] == ["ref1"]
    assert received["ctx"] is ctx


def test_call_with_optional_ctx_drops_ctx_for_pre_ctx_impl() -> None:
    """Backwards-compat: an adopter impl written before ctx threading
    landed (no ``ctx`` parameter) keeps working unchanged. The
    framework probes via inspect.signature and drops ctx silently."""
    received: dict[str, Any] = {}

    def legacy_upsert(refs: list[Any]) -> str:
        received["refs"] = refs
        return "no-ctx"

    ctx = ResolveContext(tool_name="sync_accounts")
    out = _call_with_optional_ctx(legacy_upsert, ["ref1"], ctx=ctx)
    assert out == "no-ctx"
    assert received["refs"] == ["ref1"]
    # ctx not threaded — adopter doesn't accept it.
    assert "ctx" not in received


def test_call_with_optional_ctx_works_with_async_handler() -> None:
    """Async adopter impl — ``inspect.signature`` works regardless
    of coroutine-ness; the dispatch shim leaves awaiting to the
    caller."""

    async def async_upsert(refs: list[Any], ctx: ResolveContext | None = None) -> str:
        return f"async-{len(refs)}-{ctx.tool_name if ctx else 'no-ctx'}"

    ctx = ResolveContext(tool_name="sync_accounts")
    coro = _call_with_optional_ctx(async_upsert, ["a", "b"], ctx=ctx)
    result = asyncio.run(coro)
    assert result == "async-2-sync_accounts"


def test_billing_not_permitted_for_agent_via_ctx() -> None:
    """Adopter pattern: read ctx.agent.billing_capabilities to gate
    the request; raise AdcpError(BILLING_NOT_PERMITTED_FOR_AGENT)
    when the requested billing isn't in the agent's permitted
    subset."""
    operator_only_agent = BuyerAgent(
        agent_url="https://buyer.example.com",
        display_name="Buyer",
        status="active",
        billing_capabilities=frozenset({"operator"}),
    )

    def upsert_with_billing_gate(
        refs: list[dict[str, Any]],
        ctx: ResolveContext | None = None,
    ) -> list[SyncAccountsResultRow]:
        # Adopter's billing-gate check.
        for r in refs:
            requested = r.get("billing")
            if (
                requested is not None
                and ctx is not None
                and ctx.agent is not None
                and requested not in ctx.agent.billing_capabilities
            ):
                raise AdcpError(
                    "BILLING_NOT_PERMITTED_FOR_AGENT",
                    message=(f"Agent {ctx.agent.agent_url!r} cannot bill " f"as {requested!r}"),
                    field="billing",
                    recovery="terminal",
                )
        return [
            SyncAccountsResultRow(
                brand=r["brand"],
                operator=r["operator"],
                action="created",
                status="active",
            )
            for r in refs
        ]

    ctx = ResolveContext(tool_name="sync_accounts", agent=operator_only_agent)

    # Permitted billing — succeeds.
    rows = _call_with_optional_ctx(
        upsert_with_billing_gate,
        [{"brand": {"domain": "acme.com"}, "operator": "acme.com", "billing": "operator"}],
        ctx=ctx,
    )
    assert rows[0].action == "created"

    # Disallowed billing — adopter raises BILLING_NOT_PERMITTED_FOR_AGENT.
    with pytest.raises(AdcpError) as exc_info:
        _call_with_optional_ctx(
            upsert_with_billing_gate,
            [{"brand": {"domain": "acme.com"}, "operator": "acme.com", "billing": "agent"}],
            ctx=ctx,
        )
    assert exc_info.value.code == "BILLING_NOT_PERMITTED_FOR_AGENT"
    assert exc_info.value.field == "billing"


# ---------------------------------------------------------------------------
# SyncGovernanceEntry — input shape carrying authentication
# ---------------------------------------------------------------------------


def test_sync_governance_entry_carries_authentication_on_input() -> None:
    """The input shape passed to AccountStore.sync_governance keeps
    authentication (with credentials) so adopters can persist it for
    outbound check_governance calls. The framework strips on emit
    only — input shape preserves credentials for the persistence
    step."""
    entry = SyncGovernanceEntry(
        account=AccountReference(root={"account_id": "acct_1"}),
        governance_agents=[
            {
                "url": "https://gov.example.com/",
                "authentication": {
                    "schemes": ["Bearer"],
                    "credentials": "bearer-token-with-32-or-more-chars",
                },
            }
        ],
    )
    # Adopter sees the full input, including credentials.
    agent = entry.governance_agents[0]
    assert agent["authentication"]["credentials"] == "bearer-token-with-32-or-more-chars"


def test_sync_governance_typed_request_agent_has_authentication() -> None:
    """The wire-input GovernanceAgent type carries authentication —
    confirms the schema split between input (with creds) and output
    (creds stripped) holds for the codegen'd types.

    The publicly-exported ``GovernanceAgent`` from ``adcp.types`` is
    the request-side variant (carries ``authentication``); the
    response-side variant lives at
    ``adcp.types.generated_poc.core.account.GovernanceAgent`` and has
    no ``authentication`` field. The framework's wire-emit projection
    strips ``authentication`` regardless of which shape the adopter
    constructs."""
    auth = Authentication(schemes=["Bearer"], credentials="x" * 32)
    request_agent = GovernanceAgent(
        url="https://gov.example.com",
        authentication=auth,
    )
    assert request_agent.authentication.credentials == "x" * 32

    # The response-side GovernanceAgent has no authentication field.
    assert "authentication" not in ResponseGovernanceAgent.model_fields
