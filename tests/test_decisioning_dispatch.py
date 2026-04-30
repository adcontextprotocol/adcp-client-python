"""Unit tests for adcp.decisioning.dispatch.

Covers the seam that ties RequestContext hydration, account
resolution, executor lifecycle, AdcpError projection, and
TaskHandoff lifecycle together. Per the dispatch design doc's file
plan + round-3/4 review additions.
"""

from __future__ import annotations

import asyncio
import warnings
from concurrent.futures import ThreadPoolExecutor
from contextvars import ContextVar
from typing import Any

import pytest
from pydantic import BaseModel

from adcp.decisioning import (
    AdcpError,
    AuthInfo,
    DecisioningCapabilities,
    DecisioningPlatform,
    InMemoryTaskRegistry,
    SingletonAccounts,
)
from adcp.decisioning.dispatch import (
    REQUIRED_METHODS_PER_SPECIALISM,
    _build_request_context,
    _invoke_platform_method,
    _project_handoff,
    compose_caller_identity,
    validate_platform,
)
from adcp.decisioning.types import Account, TaskHandoff
from adcp.server.base import ToolContext


@pytest.fixture
def executor():
    """ThreadPoolExecutor fixture — small pool, cleaned up per test."""
    pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="test-dispatch-")
    yield pool
    pool.shutdown(wait=True)


# ---- validate_platform ----


class _ValidPlatform(DecisioningPlatform):
    capabilities = DecisioningCapabilities(specialisms=["sales-non-guaranteed"])
    accounts = SingletonAccounts(account_id="hello")

    def get_products(self, req, ctx):
        return {"products": []}

    def create_media_buy(self, req, ctx):
        return {"media_buy_id": "mb_1"}

    def update_media_buy(self, media_buy_id, patch, ctx):
        return {"media_buy_id": media_buy_id, "status": "active"}

    def sync_creatives(self, req, ctx):
        return {"creatives": []}

    def get_media_buy_delivery(self, req, ctx):
        return {"deliveries": []}


def test_validate_platform_passes_for_valid_subclass() -> None:
    """Happy path — fully-implemented platform passes validation."""
    validate_platform(_ValidPlatform())


def test_validate_platform_raises_when_capabilities_is_default() -> None:
    """Subclass that forgets to set ``capabilities`` inherits the
    base class's ``DecisioningCapabilities()`` (empty) — that's
    actually fine (no specialisms claimed = no methods required).
    But subclass that REPLACES with a non-DecisioningCapabilities
    type fails fast."""

    class _BogusCapsPlatform(DecisioningPlatform):
        capabilities = "not a DecisioningCapabilities"  # type: ignore[assignment]
        accounts = SingletonAccounts(account_id="hello")

    with pytest.raises(AdcpError, match="must be a DecisioningCapabilities"):
        validate_platform(_BogusCapsPlatform())


def test_validate_platform_raises_when_accounts_none() -> None:
    """Subclass that forgets to attach an AccountStore fails fast."""

    class _MissingAccountsPlatform(DecisioningPlatform):
        capabilities = DecisioningCapabilities()

    with pytest.raises(AdcpError, match="accounts is None"):
        validate_platform(_MissingAccountsPlatform())


def test_validate_platform_raises_on_missing_specialism_method() -> None:
    """Platform claims sales-non-guaranteed but only implements 3 of
    the 5 required methods — raises with per-method diagnostics."""

    class _PartialSalesPlatform(DecisioningPlatform):
        capabilities = DecisioningCapabilities(specialisms=["sales-non-guaranteed"])
        accounts = SingletonAccounts(account_id="hello")

        def get_products(self, req, ctx):
            return {}

        def create_media_buy(self, req, ctx):
            return {}

        def update_media_buy(self, media_buy_id, patch, ctx):
            return {}

        # Missing: sync_creatives, get_media_buy_delivery

    with pytest.raises(AdcpError) as exc_info:
        validate_platform(_PartialSalesPlatform())
    assert exc_info.value.code == "INVALID_REQUEST"
    missing_methods = {m["method"] for m in exc_info.value.details["missing"]}
    assert "sync_creatives" in missing_methods
    assert "get_media_buy_delivery" in missing_methods


def test_validate_platform_warns_on_unknown_specialism() -> None:
    """Unknown specialism — typo or future spec — emits UserWarning,
    NOT an AdcpError raise. Forward-compat with v6.x+ specs (round-3
    D14)."""

    class _UnknownSpecialismPlatform(DecisioningPlatform):
        capabilities = DecisioningCapabilities(specialisms=["this-does-not-exist-yet"])
        accounts = SingletonAccounts(account_id="hello")

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always", UserWarning)
        validate_platform(_UnknownSpecialismPlatform())
    matched = [w for w in caught if "this-does-not-exist-yet" in str(w.message)]
    assert len(matched) == 1
    assert "typos" in str(matched[0].message)


def test_validate_platform_governance_aware_required_for_governance_specialism() -> None:
    """A platform claiming a governance-* specialism without setting
    capabilities.governance_aware=True fails fast — silent gate
    skipping is a security regression. (D15 round-4)"""

    class _GovernanceWithoutOptInPlatform(DecisioningPlatform):
        capabilities = DecisioningCapabilities(
            specialisms=["governance-spend-authority"],
            governance_aware=False,
        )
        accounts = SingletonAccounts(account_id="hello")

    with pytest.raises(AdcpError) as exc_info:
        validate_platform(_GovernanceWithoutOptInPlatform())
    assert exc_info.value.code == "INVALID_REQUEST"
    msg = str(exc_info.value)
    assert "governance" in msg.lower()
    assert "governance_aware" in msg


def test_validate_platform_governance_aware_optin_passes() -> None:
    """Platform with governance_aware=True passes validation. (The
    real Stage-3 wiring will additionally require a custom
    StateReader; that check is per-request, not boot-time, since the
    StateReader is supplied by serve()/dispatch.)"""

    class _GovernanceOptInPlatform(DecisioningPlatform):
        capabilities = DecisioningCapabilities(
            specialisms=["governance-spend-authority"],
            governance_aware=True,
        )
        accounts = SingletonAccounts(account_id="hello")

    # Note: governance-spend-authority isn't in
    # REQUIRED_METHODS_PER_SPECIALISM yet (v6.0 ships only sales-*),
    # so it'll emit an "unknown specialism" UserWarning. That's fine
    # — the governance_aware flag is what we're testing here.
    with warnings.catch_warnings(record=True):
        warnings.simplefilter("always", UserWarning)
        validate_platform(_GovernanceOptInPlatform())


def test_validate_platform_empty_specialisms_passes() -> None:
    """Platform with no specialism claims passes — useful for
    custom-base sellers that don't fit a spec specialism."""

    class _NoClaimsPlatform(DecisioningPlatform):
        capabilities = DecisioningCapabilities(specialisms=[])
        accounts = SingletonAccounts(account_id="hello")

    validate_platform(_NoClaimsPlatform())


def test_required_methods_per_specialism_pinned_for_sales() -> None:
    """Contract test — locks the sales core method set so future
    spec churn surfaces as a visible test failure."""
    expected_core = {
        "get_products",
        "create_media_buy",
        "update_media_buy",
        "sync_creatives",
        "get_media_buy_delivery",
    }
    for slug in (
        "sales-non-guaranteed",
        "sales-guaranteed",
        "sales-broadcast-tv",
        "sales-streaming-tv",
        "sales-social",
        "sales-exchange",
        "sales-proposal-mode",
    ):
        assert REQUIRED_METHODS_PER_SPECIALISM[slug] == expected_core, f"sales core drift on {slug}"


# ---- compose_caller_identity (D9 round-3) ----


def test_compose_caller_identity_uses_module_qualname_and_account_id() -> None:
    """Composite key is ``module.qualname:account_id``. Includes
    ``__module__`` because two ``MyStore`` classes in different
    packages share ``__qualname__`` — structural cross-MODULE
    isolation (round-4 review)."""
    store = SingletonAccounts(account_id="acme")
    account: Account[Any] = Account(id="acme:buyer-a")
    key = compose_caller_identity(account, store)
    assert key == "adcp.decisioning.accounts.SingletonAccounts:acme:buyer-a"


def test_compose_caller_identity_rejects_empty_account_id() -> None:
    """Empty/whitespace/<unset> account.id raises — Account(id="")
    or the dataclass default would silently collapse every empty-id
    tenant into one cache scope class (P0 security fix from round-4
    review)."""
    store = SingletonAccounts(account_id="x")
    for bogus in ("", "   ", "<unset>"):
        with pytest.raises(AdcpError) as exc_info:
            compose_caller_identity(Account(id=bogus), store)
        assert exc_info.value.code == "INVALID_REQUEST"
        assert "empty" in str(exc_info.value).lower() or "unset" in str(exc_info.value).lower()


def test_compose_caller_identity_isolates_across_stores() -> None:
    """Two different store classes with the same account.id produce
    different cache keys — structural cross-store isolation (round-3
    D9)."""

    class _CustomStore:
        resolution = "explicit"

        def resolve(self, ref, auth_info=None):
            return Account(id="x")

    a = SingletonAccounts(account_id="hello")
    b = _CustomStore()
    same_account: Account[Any] = Account(id="x")
    assert compose_caller_identity(same_account, a) != compose_caller_identity(same_account, b)


# ---- _build_request_context ----


def test_build_request_context_threads_account_and_auth() -> None:
    tool_ctx = ToolContext(
        request_id="req_1",
        caller_identity="caller_x",
        tenant_id="tenant_y",
        metadata={"foo": "bar"},
    )
    account: Account[Any] = Account(id="acct_a", name="Acme")
    auth = AuthInfo(kind="signed_request", principal="buyer-a", key_id="kid-1")

    ctx = _build_request_context(tool_ctx, account, auth)

    assert ctx.account is account
    assert ctx.auth_info is auth
    assert ctx.auth_principal == "buyer-a"
    assert ctx.request_id == "req_1"
    # Without ``store=`` (test fixture path), caller_identity falls
    # back to tool_ctx.caller_identity. The composite-key path is
    # exercised by test_build_request_context_uses_composite_key_when_store_supplied.
    assert ctx.caller_identity == "caller_x"
    assert ctx.tenant_id == "tenant_y"
    assert ctx.metadata == {"foo": "bar"}


def test_build_request_context_uses_composite_key_when_store_supplied() -> None:
    """P0 round-4 regression: ``_build_request_context`` MUST set
    ``ctx.caller_identity`` to the composite key when ``store=`` is
    supplied. Without this wiring, idempotency middleware caches by
    raw ``tool_ctx.caller_identity`` and D9 round-3 cross-store
    isolation does not exist at runtime."""
    store = SingletonAccounts(account_id="acme")
    account: Account[Any] = Account(id="acme:buyer-a")
    tool_ctx = ToolContext(caller_identity="raw-original")
    ctx = _build_request_context(tool_ctx, account, None, store=store)
    assert ctx.caller_identity == ("adcp.decisioning.accounts.SingletonAccounts:acme:buyer-a")


def test_build_request_context_with_no_auth() -> None:
    """Unauthenticated dev path (singleton fixtures): auth_principal
    is None, auth_info is None."""
    tool_ctx = ToolContext()
    account: Account[Any] = Account(id="dev")
    ctx = _build_request_context(tool_ctx, account, None)
    assert ctx.auth_info is None
    assert ctx.auth_principal is None


def test_build_request_context_supplies_stubs_when_no_state_resolver() -> None:
    """Default state/resolve are the v6.0 stubs — adopter call
    sites work without explicit wiring."""
    from adcp.decisioning.resolve import _NotYetWiredResolver
    from adcp.decisioning.state import _NotYetWiredStateReader

    ctx = _build_request_context(ToolContext(), Account(id="x"), None)
    assert isinstance(ctx.state, _NotYetWiredStateReader)
    assert isinstance(ctx.resolve, _NotYetWiredResolver)


def test_build_request_context_threads_custom_state_and_resolver() -> None:
    """Stage-3 serve() can wire a v6.1-style backing store; dispatch
    plumbs it through unchanged."""

    class _FakeStateReader:
        def find_by_object(self, t, i):
            return ("custom",)

        def find_proposal_by_id(self, p):
            return None

        def governance_context(self):
            return None

        def workflow_steps(self):
            return ()

    class _FakeResolver:
        async def property_list(self, list_id):
            return f"resolved:{list_id}"

        async def collection_list(self, list_id):
            return None

        async def creative_format(self, format_id, *, revalidate=False):
            return None

    fake_state = _FakeStateReader()
    fake_resolve = _FakeResolver()
    ctx = _build_request_context(
        ToolContext(),
        Account(id="x"),
        None,
        state_reader=fake_state,
        resource_resolver=fake_resolve,
    )
    assert ctx.state is fake_state
    assert ctx.resolve is fake_resolve


# ---- _invoke_platform_method ----


class _ProductsRequest(BaseModel):
    """Stand-in Pydantic request for tests."""

    foo: str = "bar"


class _ProductsResponse(BaseModel):
    products: list[dict[str, Any]] = []


@pytest.mark.asyncio
async def test_invoke_async_method_returns_typed_response(
    executor: ThreadPoolExecutor,
) -> None:
    class _AsyncPlatform(DecisioningPlatform):
        capabilities = DecisioningCapabilities()
        accounts = SingletonAccounts(account_id="x")

        async def get_products(self, req, ctx):
            return _ProductsResponse(products=[{"id": "p1"}])

    ctx = _build_request_context(ToolContext(), Account(id="x"), None)
    result = await _invoke_platform_method(
        _AsyncPlatform(),
        "get_products",
        _ProductsRequest(),
        ctx,
        executor=executor,
        registry=InMemoryTaskRegistry(),
    )
    assert isinstance(result, _ProductsResponse)
    assert result.products == [{"id": "p1"}]


@pytest.mark.asyncio
async def test_invoke_sync_method_runs_on_executor(
    executor: ThreadPoolExecutor,
) -> None:
    """Sync platform method runs in a worker thread — verified via
    thread-name introspection."""
    seen_thread_names: list[str] = []

    class _SyncPlatform(DecisioningPlatform):
        capabilities = DecisioningCapabilities()
        accounts = SingletonAccounts(account_id="x")

        def get_products(self, req, ctx):
            import threading

            seen_thread_names.append(threading.current_thread().name)
            return _ProductsResponse(products=[{"id": "sync"}])

    ctx = _build_request_context(ToolContext(), Account(id="x"), None)
    result = await _invoke_platform_method(
        _SyncPlatform(),
        "get_products",
        _ProductsRequest(),
        ctx,
        executor=executor,
        registry=InMemoryTaskRegistry(),
    )
    assert isinstance(result, _ProductsResponse)
    assert seen_thread_names[0].startswith(
        "test-dispatch-"
    ), f"sync method should run on the test executor; ran on {seen_thread_names}"


@pytest.mark.asyncio
async def test_invoke_sync_method_propagates_contextvars(
    executor: ThreadPoolExecutor,
) -> None:
    """Sync handler running on the executor sees ContextVars set in
    the request scope (D6 — explicit copy_context). Without the
    explicit snapshot, the executor thread sees the default value
    instead of the request-scoped one."""
    request_id_var: ContextVar[str] = ContextVar("test_request_id", default="default")
    seen: list[str] = []

    class _SyncPlatform(DecisioningPlatform):
        capabilities = DecisioningCapabilities()
        accounts = SingletonAccounts(account_id="x")

        def get_products(self, req, ctx):
            seen.append(request_id_var.get())
            return _ProductsResponse()

    ctx = _build_request_context(ToolContext(), Account(id="x"), None)

    request_id_var.set("req_xyz")
    await _invoke_platform_method(
        _SyncPlatform(),
        "get_products",
        _ProductsRequest(),
        ctx,
        executor=executor,
        registry=InMemoryTaskRegistry(),
    )
    assert seen == ["req_xyz"]


@pytest.mark.asyncio
async def test_invoke_re_raises_adcp_error(
    executor: ThreadPoolExecutor,
) -> None:
    class _RaisingPlatform(DecisioningPlatform):
        capabilities = DecisioningCapabilities()
        accounts = SingletonAccounts(account_id="x")

        async def get_products(self, req, ctx):
            raise AdcpError(
                "BUDGET_TOO_LOW",
                message="below floor",
                recovery="correctable",
            )

    ctx = _build_request_context(ToolContext(), Account(id="x"), None)
    with pytest.raises(AdcpError) as exc_info:
        await _invoke_platform_method(
            _RaisingPlatform(),
            "get_products",
            _ProductsRequest(),
            ctx,
            executor=executor,
            registry=InMemoryTaskRegistry(),
        )
    # Verbatim — NOT wrapped to INTERNAL_ERROR.
    assert exc_info.value.code == "BUDGET_TOO_LOW"
    assert exc_info.value.recovery == "correctable"


@pytest.mark.asyncio
async def test_invoke_wraps_unexpected_exceptions_to_internal_error(
    executor: ThreadPoolExecutor,
) -> None:
    class _CrashingPlatform(DecisioningPlatform):
        capabilities = DecisioningCapabilities()
        accounts = SingletonAccounts(account_id="x")

        async def get_products(self, req, ctx):
            raise ValueError("oops, internal-state bug")

    ctx = _build_request_context(ToolContext(), Account(id="x"), None)
    with pytest.raises(AdcpError) as exc_info:
        await _invoke_platform_method(
            _CrashingPlatform(),
            "get_products",
            _ProductsRequest(),
            ctx,
            executor=executor,
            registry=InMemoryTaskRegistry(),
        )
    assert exc_info.value.code == "INTERNAL_ERROR"
    assert exc_info.value.recovery == "terminal"
    # Original exception preserved as __cause__ for server-side
    # debugging — wire response stays opaque.
    assert isinstance(exc_info.value.__cause__, ValueError)
    assert "oops, internal-state bug" not in str(exc_info.value)


@pytest.mark.asyncio
async def test_invoke_with_arg_projector_uses_kwargs(
    executor: ThreadPoolExecutor,
) -> None:
    """Tools whose Python signature differs from wire shape (D1
    arg-projection — e.g. update_media_buy(media_buy_id, patch,
    ctx)) get the kwargs dict passed through."""

    class _PatchModel(BaseModel):
        media_buy_id: str
        new_status: str

    class _ProjectingPlatform(DecisioningPlatform):
        capabilities = DecisioningCapabilities()
        accounts = SingletonAccounts(account_id="x")

        async def update_media_buy(self, media_buy_id, patch, ctx):
            return {"media_buy_id": media_buy_id, "status": patch.new_status}

    ctx = _build_request_context(ToolContext(), Account(id="x"), None)
    patch = _PatchModel(media_buy_id="mb_1", new_status="active")
    result = await _invoke_platform_method(
        _ProjectingPlatform(),
        "update_media_buy",
        patch,
        ctx,
        executor=executor,
        registry=InMemoryTaskRegistry(),
        arg_projector={"media_buy_id": "mb_1", "patch": patch},
    )
    assert result == {"media_buy_id": "mb_1", "status": "active"}


# ---- _project_handoff (TaskHandoff lifecycle) ----


@pytest.mark.asyncio
async def test_handoff_returns_submitted_envelope(
    executor: ThreadPoolExecutor,
) -> None:
    """The synchronous return is the wire Submitted envelope —
    {task_id, status, task_type}. Buyer pattern-matches on shape."""
    registry = InMemoryTaskRegistry()
    ctx = _build_request_context(ToolContext(), Account(id="acct_a"), None)
    completed = asyncio.Event()

    async def _handoff_fn(task_ctx):
        completed.set()
        return {"media_buy_id": "mb_1"}

    handoff = TaskHandoff(_handoff_fn)
    envelope = await _project_handoff(
        handoff,
        ctx,
        method_name="create_media_buy",
        registry=registry,
        executor=executor,
    )
    assert envelope["status"] == "submitted"
    assert envelope["task_type"] == "create_media_buy"
    assert envelope["task_id"].startswith("task_")

    # Wait for the background task to complete so the assertion below
    # is deterministic. (CI may schedule background tasks slowly.)
    await asyncio.wait_for(completed.wait(), timeout=2.0)
    # Yield once more so the registry.complete() call lands.
    await asyncio.sleep(0.05)

    rec = await registry.get(envelope["task_id"], expected_account_id="acct_a")
    assert rec is not None
    assert rec["state"] == "completed"
    assert rec["result"] == {"media_buy_id": "mb_1"}


@pytest.mark.asyncio
async def test_handoff_async_fn_completes_via_registry(
    executor: ThreadPoolExecutor,
) -> None:
    """Async handoff fn returns a Pydantic model; framework calls
    model_dump() and persists the dict via registry.complete."""
    registry = InMemoryTaskRegistry()
    ctx = _build_request_context(ToolContext(), Account(id="acct_a"), None)

    async def _handoff_fn(task_ctx):
        return _ProductsResponse(products=[{"id": "x"}])

    envelope = await _project_handoff(
        TaskHandoff(_handoff_fn),
        ctx,
        method_name="create_media_buy",
        registry=registry,
        executor=executor,
    )
    # Wait for background task to finish.
    await asyncio.sleep(0.1)
    rec = await registry.get(envelope["task_id"], expected_account_id="acct_a")
    assert rec is not None
    assert rec["state"] == "completed"
    assert rec["result"] == {"products": [{"id": "x"}]}


@pytest.mark.asyncio
async def test_handoff_adcp_error_persists_via_registry_fail(
    executor: ThreadPoolExecutor,
) -> None:
    """When the handoff fn raises AdcpError, the framework calls
    registry.fail with the to_wire() shape so tasks/get returns
    the spec adcp_error envelope."""
    registry = InMemoryTaskRegistry()
    ctx = _build_request_context(ToolContext(), Account(id="acct_a"), None)

    async def _handoff_fn(task_ctx):
        raise AdcpError(
            "POLICY_VIOLATION",
            message="rejected",
            recovery="correctable",
            field="package",
        )

    envelope = await _project_handoff(
        TaskHandoff(_handoff_fn),
        ctx,
        method_name="create_media_buy",
        registry=registry,
        executor=executor,
    )
    await asyncio.sleep(0.1)
    rec = await registry.get(envelope["task_id"], expected_account_id="acct_a")
    assert rec is not None
    assert rec["state"] == "failed"
    assert rec["error"]["code"] == "POLICY_VIOLATION"
    assert rec["error"]["recovery"] == "correctable"
    assert rec["error"]["field"] == "package"


@pytest.mark.asyncio
async def test_handoff_unexpected_exception_wraps_to_internal_error(
    executor: ThreadPoolExecutor,
) -> None:
    """Non-AdcpError exception in the handoff fn wraps to
    INTERNAL_ERROR — wire response never leaks the original."""
    registry = InMemoryTaskRegistry()
    ctx = _build_request_context(ToolContext(), Account(id="acct_a"), None)

    async def _handoff_fn(task_ctx):
        raise RuntimeError("internal bug")

    envelope = await _project_handoff(
        TaskHandoff(_handoff_fn),
        ctx,
        method_name="create_media_buy",
        registry=registry,
        executor=executor,
    )
    await asyncio.sleep(0.1)
    rec = await registry.get(envelope["task_id"], expected_account_id="acct_a")
    assert rec is not None
    assert rec["state"] == "failed"
    assert rec["error"]["code"] == "INTERNAL_ERROR"
    # Original exception text NOT exposed.
    assert "internal bug" not in rec["error"].get("message", "")


@pytest.mark.asyncio
async def test_handoff_sync_fn_runs_on_executor(
    executor: ThreadPoolExecutor,
) -> None:
    """Sync handoff fn runs on the executor with explicit
    contextvars snapshot. (Async fn uses asyncio.create_task which
    inherits contextvars for free; sync needs the explicit copy.)"""
    registry = InMemoryTaskRegistry()
    ctx = _build_request_context(ToolContext(), Account(id="acct_a"), None)

    def _sync_handoff_fn(task_ctx):
        import threading

        return {"thread": threading.current_thread().name}

    envelope = await _project_handoff(
        TaskHandoff(_sync_handoff_fn),
        ctx,
        method_name="create_media_buy",
        registry=registry,
        executor=executor,
    )
    await asyncio.sleep(0.1)
    rec = await registry.get(envelope["task_id"], expected_account_id="acct_a")
    assert rec is not None
    assert rec["state"] == "completed"
    assert rec["result"]["thread"].startswith("test-dispatch-")


@pytest.mark.asyncio
async def test_handoff_background_task_is_strong_referenced(
    executor: ThreadPoolExecutor,
) -> None:
    """P0 round-4 regression: ``asyncio.create_task`` only weak-refs
    the resulting Task; under GC pressure the loop can collect the
    background task before it completes, leaving the registry stuck
    in 'submitted' forever. Fix: the framework tracks pending tasks
    in a module-level set with done-callback cleanup. Test asserts
    the set membership is correct during the task's lifetime."""
    from adcp.decisioning.dispatch import _BACKGROUND_HANDOFF_TASKS

    registry = InMemoryTaskRegistry()
    ctx = _build_request_context(ToolContext(), Account(id="acct_a"), None)
    started = asyncio.Event()
    finish = asyncio.Event()

    async def _handoff_fn(task_ctx):
        started.set()
        await finish.wait()
        return {"done": True}

    initial_size = len(_BACKGROUND_HANDOFF_TASKS)
    envelope = await _project_handoff(
        TaskHandoff(_handoff_fn),
        ctx,
        method_name="create_media_buy",
        registry=registry,
        executor=executor,
    )
    # Background task is alive — strong-ref'd via the module-level set.
    await asyncio.wait_for(started.wait(), timeout=2.0)
    assert len(_BACKGROUND_HANDOFF_TASKS) > initial_size
    bg_tasks_for_this = [
        t
        for t in _BACKGROUND_HANDOFF_TASKS
        if t.get_name() == f"adcp-handoff-{envelope['task_id']}"
    ]
    assert (
        len(bg_tasks_for_this) == 1
    ), f"Expected exactly one tracked background task; got {len(bg_tasks_for_this)}"
    # Let it complete; the done-callback removes from the set.
    finish.set()
    await asyncio.sleep(0.1)
    assert all(
        t.get_name() != f"adcp-handoff-{envelope['task_id']}" for t in _BACKGROUND_HANDOFF_TASKS
    ), "Completed background task must be removed via done-callback"


@pytest.mark.asyncio
async def test_handoff_invoked_via_invoke_platform_method(
    executor: ThreadPoolExecutor,
) -> None:
    """End-to-end: a platform method returning ctx.handoff_to_task(fn)
    flows through _invoke_platform_method and produces the Submitted
    envelope without the caller knowing it was a handoff."""
    registry = InMemoryTaskRegistry()
    ctx = _build_request_context(ToolContext(), Account(id="acct_a"), None)

    async def _async_review(task_ctx):
        return _ProductsResponse(products=[{"id": "reviewed"}])

    class _HybridPlatform(DecisioningPlatform):
        capabilities = DecisioningCapabilities()
        accounts = SingletonAccounts(account_id="x")

        async def create_media_buy(self, req, ctx):
            return ctx.handoff_to_task(_async_review)

    result = await _invoke_platform_method(
        _HybridPlatform(),
        "create_media_buy",
        _ProductsRequest(),
        ctx,
        executor=executor,
        registry=registry,
    )
    # Returned the wire envelope, NOT the handoff marker.
    assert isinstance(result, dict)
    assert result["status"] == "submitted"
    assert result["task_type"] == "create_media_buy"
