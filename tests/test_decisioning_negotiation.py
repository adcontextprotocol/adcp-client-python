from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

from adcp.decisioning import (
    Account,
    AdcpError,
    AuthInfo,
    DecisioningCapabilities,
    DecisioningPlatform,
    FromAuthAccounts,
    InMemoryTaskRegistry,
    PlatformRouter,
    ProposalCapabilities,
    SingletonAccounts,
    execute_refinement_batch,
    preflight_refinement_batch_or_raise,
    prepare_refinement_result,
)
from adcp.decisioning.capabilities import MediaBuy
from adcp.decisioning.handler import PlatformHandler
from adcp.decisioning.proposal_store import ProposalState
from adcp.negotiation import compute_terms_digest
from adcp.server import ToolContext
from adcp.testing import make_request_context
from adcp.types import RefineProposalsRequest


def _terms(amount: float = 100) -> dict:
    return {
        "brand": {"domain": "buyer.example"},
        "purchases": [
            {
                "product_id": "p1",
                "pricing": {
                    "pricing_option_id": "po-1",
                    "pricing_model": "cpm",
                    "currency": "USD",
                    "fixed_price": 5,
                },
                "pricing_option_id": "po-1",
                "impressions": 20_000,
            }
        ],
        "total_budget": {"amount": amount, "currency": "USD"},
        "start_time": "2026-09-01T00:00:00Z",
        "end_time": "2026-09-30T00:00:00Z",
    }


def _proposal(*, status: str = "draft", parent: str | None = None) -> dict:
    terms = _terms()
    proposal = {
        "proposal_id": "successor-1",
        "proposal_kind": "new_media_buy",
        "proposal_status": status,
        "name": "September plan",
        "commercial_terms": terms,
    }
    if parent is not None:
        proposal["parent_proposal_id"] = parent
    if status == "committed":
        proposal["expires_at"] = "2099-08-25T00:00:00Z"
    return proposal


def _source_proposal(proposal_id: str = "source-1", *, status: str = "draft") -> dict:
    proposal = _proposal(status=status)
    proposal["proposal_id"] = proposal_id
    return proposal


def _request(action: str = "revise") -> dict:
    refinement = {"proposal_id": "source-1", "action": action}
    if action == "revise":
        refinement["ask"] = "Please revise the proposal."
    return {
        "idempotency_key": "refinement-request-0001",
        "refinements": [refinement],
    }


def test_seller_preflight_uses_typed_unsupported_dimension_details() -> None:
    request = _request()
    request["refinements"][0]["constraints"] = {
        "flight": {"end_no_earlier_than": "2026-09-30T00:00:00Z"}
    }

    with pytest.raises(AdcpError) as exc_info:
        preflight_refinement_batch_or_raise(
            request,
            {"supported_dimensions": ["total_budget", "cpm"]},
        )

    assert exc_info.value.code == "UNSUPPORTED_FEATURE"
    assert exc_info.value.details == {
        "unsupported_dimension": "flight",
        "supported_dimensions": ["total_budget", "cpm"],
    }


def test_seller_preflight_reports_batch_validation_before_policy() -> None:
    request = _request()
    request["refinements"].append(dict(request["refinements"][0]))

    with pytest.raises(AdcpError) as exc_info:
        preflight_refinement_batch_or_raise(request, None)

    assert exc_info.value.code == "VALIDATION_ERROR"
    assert exc_info.value.details["issues"][0]["code"] == "duplicate_proposal_id"


@pytest.mark.parametrize(
    "refinements",
    [
        [],
        [{"proposal_id": "source-1", "action": "revise"}],
        [
            {"proposal_id": f"source-{index}", "action": "revise", "ask": "revise"}
            for index in range(26)
        ],
    ],
)
def test_seller_preflight_enforces_request_schema_before_policy(refinements) -> None:
    with pytest.raises(AdcpError) as exc_info:
        preflight_refinement_batch_or_raise({"refinements": refinements}, None)

    assert exc_info.value.code == "VALIDATION_ERROR"
    assert exc_info.value.details["schema_errors"]


def test_prepare_result_adds_only_missing_lineage_and_digest() -> None:
    proposal = _proposal()
    result = prepare_refinement_result(
        _request()["refinements"][0],
        {"outcome": "revised", "proposals": [proposal]},
    )

    prepared = result["proposals"][0]
    assert result["source_proposal_id"] == "source-1"
    assert prepared["parent_proposal_id"] == "source-1"
    assert prepared["terms_digest"] == compute_terms_digest(prepared["commercial_terms"])


@pytest.mark.asyncio
async def test_finalize_requires_atomic_transaction_before_policy_runs() -> None:
    called = False

    async def process(_refinement, _context):
        nonlocal called
        called = True
        return {"outcome": "finalized", "proposal": _proposal(status="committed")}

    with pytest.raises(AdcpError, match="atomic refinement transaction") as exc_info:
        await execute_refinement_batch(_request("finalize"), None, process)

    assert exc_info.value.code == "UNSUPPORTED_FEATURE"
    assert not called


@pytest.mark.asyncio
async def test_finalize_transaction_commits_only_after_response_verification() -> None:
    events: list[str] = []

    @asynccontextmanager
    async def transaction(_request, _context):
        events.append("begin")
        try:
            yield
        except Exception:
            events.append("rollback")
            raise
        else:
            events.append("commit")

    async def process(_refinement, _context):
        events.append("hold")
        return {"outcome": "finalized", "proposal": _proposal(status="committed")}

    response = await execute_refinement_batch(
        _request("finalize"),
        None,
        process,
        finalize_transaction=transaction,
        source_proposals={"source-1": _source_proposal()},
    )

    assert response["results"][0]["proposal"]["parent_proposal_id"] == "source-1"
    assert events == ["begin", "hold", "commit"]


@pytest.mark.asyncio
async def test_invalid_finalize_response_rolls_back_entire_transaction() -> None:
    events: list[str] = []

    @asynccontextmanager
    async def transaction(_request, _context):
        events.append("begin")
        try:
            yield
        except Exception:
            events.append("rollback")
            raise
        else:
            events.append("commit")

    async def process(_refinement, _context):
        events.append("hold")
        return {
            "source_proposal_id": "source-1",
            "outcome": "finalized",
            "proposal": _proposal(status="committed", parent="wrong-source"),
        }

    with pytest.raises(AdcpError, match="protocol-invalid response") as exc_info:
        await execute_refinement_batch(
            _request("finalize"),
            None,
            process,
            finalize_transaction=transaction,
            source_proposals={"source-1": _source_proposal()},
        )

    assert exc_info.value.code == "INTERNAL_ERROR"
    assert events == ["begin", "hold", "rollback"]


@pytest.mark.asyncio
async def test_finalize_snapshots_source_before_adopter_mutation() -> None:
    events: list[str] = []
    sources = {"source-1": _source_proposal()}

    @asynccontextmanager
    async def transaction(_request, _context):
        events.append("begin")
        try:
            yield
        except Exception:
            events.append("rollback")
            raise
        else:
            events.append("commit")

    async def process(_refinement, _context):
        mutated = sources["source-1"]
        mutated["commercial_terms"]["total_budget"]["amount"] = 200
        mutated["terms_digest"] = compute_terms_digest(mutated["commercial_terms"])
        successor = _proposal(status="committed")
        successor["commercial_terms"]["total_budget"]["amount"] = 200
        return {"outcome": "finalized", "proposal": successor}

    with pytest.raises(AdcpError, match="protocol-invalid response"):
        await execute_refinement_batch(
            _request("finalize"),
            None,
            process,
            finalize_transaction=transaction,
            source_proposals=sources,
        )

    assert events == ["begin", "rollback"]


@pytest.mark.asyncio
async def test_finalize_rejects_non_draft_source_before_policy() -> None:
    called = False
    source = _source_proposal(status="committed")

    @asynccontextmanager
    async def transaction(_request, _context):
        yield

    async def process(_refinement, _context):
        nonlocal called
        called = True
        return {"outcome": "finalized", "proposal": _proposal(status="committed")}

    with pytest.raises(AdcpError) as exc_info:
        await execute_refinement_batch(
            _request("finalize"),
            None,
            process,
            finalize_transaction=transaction,
            source_proposals={"source-1": source},
        )

    assert exc_info.value.code == "INVALID_STATE"
    assert not called


@pytest.mark.asyncio
async def test_schema_invalid_finalize_response_rolls_back() -> None:
    events: list[str] = []

    @asynccontextmanager
    async def transaction(_request, _context):
        events.append("begin")
        try:
            yield
        except Exception:
            events.append("rollback")
            raise
        else:
            events.append("commit")

    async def process(_refinement, _context):
        invalid = _proposal(status="draft")
        return {"outcome": "finalized", "proposal": invalid}

    with pytest.raises(AdcpError, match="schema-invalid"):
        await execute_refinement_batch(
            _request("finalize"),
            None,
            process,
            finalize_transaction=transaction,
            source_proposals={"source-1": _source_proposal()},
        )

    assert events == ["begin", "rollback"]


@pytest.mark.asyncio
async def test_failed_finalize_rolls_back_and_classifies_siblings() -> None:
    events: list[str] = []

    @asynccontextmanager
    async def transaction(_request, _context):
        events.append("begin")
        try:
            yield
        except Exception:
            events.append("rollback")
            raise
        else:
            events.append("commit")

    async def process(refinement, _context):
        if refinement["proposal_id"] == "source-2":
            return {
                "outcome": "unable",
                "reason_code": "hold_unavailable",
                "reason": "Inventory sold through.",
            }
        return {"outcome": "finalized", "proposal": _proposal(status="committed")}

    request = _request("finalize")
    request["refinements"].append({"proposal_id": "source-2", "action": "finalize"})
    response = await execute_refinement_batch(
        request,
        None,
        process,
        finalize_transaction=transaction,
        source_proposals={
            "source-1": _source_proposal(),
            "source-2": _source_proposal("source-2"),
        },
    )

    assert [item["outcome"] for item in response["results"]] == ["unable", "unable"]
    assert [item["reason_code"] for item in response["results"]] == [
        "batch_aborted",
        "hold_unavailable",
    ]
    assert events == ["begin", "rollback"]


@pytest.mark.asyncio
async def test_revision_batch_needs_no_transaction_and_preserves_order() -> None:
    async def process(refinement, _context):
        proposal = _proposal()
        proposal["proposal_id"] = f"next-{refinement['proposal_id']}"
        return {"outcome": "revised", "proposals": [proposal]}

    request = _request()
    request["refinements"].append(
        {"proposal_id": "source-2", "action": "revise", "ask": "Please revise."}
    )
    response = await execute_refinement_batch(request, None, process)

    assert [item["source_proposal_id"] for item in response["results"]] == [
        "source-1",
        "source-2",
    ]


@pytest.mark.asyncio
async def test_platform_handler_preflights_before_adopter_callback() -> None:
    class Platform(DecisioningPlatform):
        capabilities = DecisioningCapabilities(
            media_buy=MediaBuy.model_validate(
                {"proposal_refinement": {"supported_dimensions": ["total_budget"]}}
            )
        )
        accounts = SingletonAccounts(account_id="seller")
        called = False

        def refine_proposals(self, req, ctx):
            self.called = True
            raise AssertionError("adopter callback must not run")

    request = _request()
    request["refinements"][0]["constraints"] = {
        "flight": {"end_no_earlier_than": "2026-09-30T00:00:00Z"}
    }
    platform = Platform()
    with ThreadPoolExecutor(max_workers=1) as executor:
        handler = PlatformHandler(platform, executor=executor, registry=InMemoryTaskRegistry())
        with pytest.raises(AdcpError) as exc_info:
            await handler.refine_proposals(
                RefineProposalsRequest.model_validate(request), ToolContext()
            )

    assert exc_info.value.code == "UNSUPPORTED_FEATURE"
    assert not platform.called


@pytest.mark.asyncio
async def test_platform_handler_rejects_invalid_adopter_response() -> None:
    class Platform(DecisioningPlatform):
        capabilities = DecisioningCapabilities(media_buy=MediaBuy())
        accounts = SingletonAccounts(account_id="seller")

        def refine_proposals(self, req, ctx):
            return {"status": "completed", "results": [], "products": []}

    with ThreadPoolExecutor(max_workers=1) as executor:
        handler = PlatformHandler(Platform(), executor=executor, registry=InMemoryTaskRegistry())
        with pytest.raises(AdcpError) as exc_info:
            await handler.refine_proposals(
                RefineProposalsRequest.model_validate(_request()), ToolContext()
            )

    assert exc_info.value.code == "INTERNAL_ERROR"
    assert exc_info.value.details["schema_errors"]


@pytest.mark.asyncio
async def test_platform_handler_snapshots_source_before_callback() -> None:
    class Platform(DecisioningPlatform):
        capabilities = DecisioningCapabilities(media_buy=MediaBuy())
        accounts = SingletonAccounts(account_id="seller")
        sources = {"source-1": _source_proposal()}

        def get_refinement_source_proposals(self, req, ctx):
            return self.sources

        def refine_proposals(self, req, ctx):
            source = self.sources["source-1"]
            source["commercial_terms"]["total_budget"]["amount"] = 200
            source["terms_digest"] = compute_terms_digest(source["commercial_terms"])
            successor = _proposal(status="committed")
            successor["parent_proposal_id"] = "source-1"
            successor["commercial_terms"]["total_budget"]["amount"] = 200
            successor["terms_digest"] = compute_terms_digest(successor["commercial_terms"])
            return {
                "status": "completed",
                "results": [
                    {
                        "source_proposal_id": "source-1",
                        "outcome": "finalized",
                        "proposal": successor,
                    }
                ],
                "products": [],
            }

    with ThreadPoolExecutor(max_workers=1) as executor:
        handler = PlatformHandler(Platform(), executor=executor, registry=InMemoryTaskRegistry())
        with pytest.raises(AdcpError, match="protocol-invalid response"):
            await handler.refine_proposals(
                RefineProposalsRequest.model_validate(_request("finalize")), ToolContext()
            )


@pytest.mark.asyncio
async def test_platform_handler_uses_authoritative_store_state_for_finalize() -> None:
    class Store:
        def get(self, proposal_id, *, expected_account_id):
            return SimpleNamespace(
                state=ProposalState.COMMITTED,
                proposal_payload=_proposal(status="draft"),
            )

    class Platform(DecisioningPlatform):
        capabilities = DecisioningCapabilities(media_buy=MediaBuy())
        accounts = SingletonAccounts(
            account_id="seller", metadata_factory=lambda: {"tenant_id": "tenant"}
        )
        called = False

        def proposal_store_for_tenant(self, tenant_id):
            return Store()

        def refine_proposals(self, req, ctx):
            self.called = True
            raise AssertionError("committed source must be rejected before callback")

    platform = Platform()
    with ThreadPoolExecutor(max_workers=1) as executor:
        handler = PlatformHandler(platform, executor=executor, registry=InMemoryTaskRegistry())
        with pytest.raises(AdcpError) as exc_info:
            await handler.refine_proposals(
                RefineProposalsRequest.model_validate(_request("finalize")), ToolContext()
            )

    assert exc_info.value.code == "INVALID_STATE"
    assert not platform.called


@pytest.mark.asyncio
async def test_platform_handler_validates_task_handoff_completion() -> None:
    class Platform(DecisioningPlatform):
        capabilities = DecisioningCapabilities(media_buy=MediaBuy())
        accounts = SingletonAccounts(account_id="seller")

        def refine_proposals(self, req, ctx):
            async def finish(_task_ctx):
                return {"status": "completed", "results": [], "products": []}

            return ctx.handoff_to_task(finish)

    registry = InMemoryTaskRegistry()
    with ThreadPoolExecutor(max_workers=1) as executor:
        handler = PlatformHandler(Platform(), executor=executor, registry=registry)
        submitted = await handler.refine_proposals(
            RefineProposalsRequest.model_validate(_request()), ToolContext()
        )
        for _ in range(20):
            record = await registry.get(submitted["task_id"])
            if record is not None and record["state"] == "failed":
                break
            await asyncio.sleep(0)

    assert record is not None
    assert record["state"] == "failed"
    assert record["error"]["code"] == "INTERNAL_ERROR"


@pytest.mark.asyncio
async def test_platform_handler_rejects_unverifiable_workflow_handoff() -> None:
    class Platform(DecisioningPlatform):
        capabilities = DecisioningCapabilities(media_buy=MediaBuy())
        accounts = SingletonAccounts(account_id="seller")

        def refine_proposals(self, req, ctx):
            return ctx.handoff_to_workflow(lambda _task_ctx: None)

    with ThreadPoolExecutor(max_workers=1) as executor:
        handler = PlatformHandler(Platform(), executor=executor, registry=InMemoryTaskRegistry())
        with pytest.raises(AdcpError, match="bypass framework response verification") as exc:
            await handler.refine_proposals(
                RefineProposalsRequest.model_validate(_request()), ToolContext()
            )

    assert exc.value.code == "UNSUPPORTED_FEATURE"


@pytest.mark.asyncio
async def test_platform_router_routes_compact_refinement_to_proposal_manager() -> None:
    class Child(DecisioningPlatform):
        capabilities = DecisioningCapabilities(media_buy=MediaBuy())
        accounts = SingletonAccounts(account_id="seller")

        def refine_proposals(self, req, ctx):
            raise AssertionError("child platform must not receive manager-owned refinement")

    class Manager:
        capabilities = ProposalCapabilities(
            sales_specialism="sales-guaranteed",
            proposal_refinement={"supported_dimensions": ["total_budget"]},
        )

        def refine_proposals(self, req, ctx):
            return {"routed": "manager"}

    router = PlatformRouter(
        accounts=SingletonAccounts(account_id="seller"),
        platforms={"tenant": Child()},
        proposal_managers={"tenant": Manager()},
        capabilities=DecisioningCapabilities(media_buy=MediaBuy()),
    )
    ctx = make_request_context(account=Account(id="seller", metadata={"tenant_id": "tenant"}))

    response = await router.refine_proposals(_request(), ctx)

    assert response == {"routed": "manager"}


def test_platform_router_rejects_declared_refinement_without_callback() -> None:
    class Child(DecisioningPlatform):
        capabilities = DecisioningCapabilities(media_buy=MediaBuy())
        accounts = SingletonAccounts(account_id="seller")

    class Manager:
        capabilities = ProposalCapabilities(
            sales_specialism="sales-guaranteed",
            proposal_refinement={"supported_dimensions": []},
        )

    with pytest.raises(ValueError, match="does not implement refine_proposals"):
        PlatformRouter(
            accounts=SingletonAccounts(account_id="seller"),
            platforms={"tenant": Child()},
            proposal_managers={"tenant": Manager()},
            capabilities=DecisioningCapabilities(media_buy=MediaBuy()),
        )


@pytest.mark.asyncio
async def test_capability_discovery_projects_manager_refinement() -> None:
    class Child(DecisioningPlatform):
        capabilities = DecisioningCapabilities(media_buy=MediaBuy())
        accounts = SingletonAccounts(account_id="child")

    class Manager:
        capabilities = ProposalCapabilities(
            sales_specialism="sales-guaranteed",
            proposal_refinement={"supported_dimensions": ["total_budget"]},
        )

        def refine_proposals(self, req, ctx):
            raise AssertionError

    router = PlatformRouter(
        accounts=SingletonAccounts(
            account_id="seller", metadata_factory=lambda: {"tenant_id": "tenant"}
        ),
        platforms={"tenant": Child()},
        proposal_managers={"tenant": Manager()},
        capabilities=DecisioningCapabilities(media_buy=MediaBuy()),
    )
    with ThreadPoolExecutor(max_workers=1) as executor:
        handler = PlatformHandler(router, executor=executor, registry=InMemoryTaskRegistry())
        response = await handler.get_adcp_capabilities({}, ToolContext())
        legacy = await handler.get_adcp_capabilities({}, ToolContext(resolved_adcp_version="3.1"))

    assert "refine_proposals" in response["media_buy"]["lifecycle_tools"]
    assert response["media_buy"]["proposal_refinement"] == {
        "supported_dimensions": ["total_budget"]
    }
    assert "refine_proposals" not in legacy["media_buy"].get("lifecycle_tools", [])
    assert "proposal_refinement" not in legacy["media_buy"]


@pytest.mark.asyncio
async def test_authenticated_capability_discovery_projects_tenant_manager_refinement() -> None:
    class Child(DecisioningPlatform):
        capabilities = DecisioningCapabilities(media_buy=MediaBuy())
        accounts = SingletonAccounts(account_id="child")

    class Manager:
        capabilities = ProposalCapabilities(
            sales_specialism="sales-guaranteed",
            proposal_refinement={"supported_dimensions": ["cpm"]},
        )

        def refine_proposals(self, req, ctx):
            raise AssertionError

    router = PlatformRouter(
        accounts=FromAuthAccounts(
            loader=lambda principal: Account(
                id=principal, metadata={"tenant_id": "tenant"}, status="active"
            )
        ),
        platforms={"tenant": Child()},
        proposal_managers={"tenant": Manager()},
        capabilities=DecisioningCapabilities(media_buy=MediaBuy()),
    )
    context = ToolContext(metadata={"adcp.auth_info": AuthInfo(kind="bearer", principal="buyer-1")})
    with ThreadPoolExecutor(max_workers=1) as executor:
        handler = PlatformHandler(router, executor=executor, registry=InMemoryTaskRegistry())
        response = await handler.get_adcp_capabilities({}, context)

    assert "refine_proposals" in response["media_buy"]["lifecycle_tools"]
    assert response["media_buy"]["proposal_refinement"] == {"supported_dimensions": ["cpm"]}


@pytest.mark.parametrize(
    "proposal_refinement",
    [
        {},
        {"supported_dimensions": ["unknown"]},
        {"supported_dimensions": ["alternatives"], "max_alternatives": 11},
        {"supported_dimensions": ["cpm"], "max_alternatives": 2},
        {"supported_dimensions": ["cpm"], "unknown": True},
    ],
)
def test_proposal_manager_rejects_invalid_refinement_capability(
    proposal_refinement: dict,
) -> None:
    with pytest.raises(ValueError):
        ProposalCapabilities(
            sales_specialism="sales-guaranteed",
            proposal_refinement=proposal_refinement,
        )
