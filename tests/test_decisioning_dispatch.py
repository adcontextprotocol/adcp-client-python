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
    SPEC_SPECIALISM_ENUM,
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


def test_validate_platform_warns_on_novel_specialism() -> None:
    """Truly novel specialism (no close spelling match to any known
    slug) emits UserWarning, NOT a raise. Forward-compat with v6.x+
    specs (round-3 D14)."""

    class _NovelSpecialismPlatform(DecisioningPlatform):
        capabilities = DecisioningCapabilities(specialisms=["this-does-not-exist-yet"])
        accounts = SingletonAccounts(account_id="hello")

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always", UserWarning)
        validate_platform(_NovelSpecialismPlatform())
    matched = [w for w in caught if "this-does-not-exist-yet" in str(w.message)]
    assert len(matched) == 1
    assert "novel specialism" in str(matched[0].message)


def test_validate_platform_raises_on_typo_specialism() -> None:
    """Round-4 DX review: a typo close-match to a known slug
    (e.g. "sales-non-guarateed" missing the second 'n') raises
    AdcpError with a "Did you mean..." hint, NOT a silent UserWarning.
    Adopters running ``python hello_seller.py`` would otherwise see
    a server boot with 0 tools advertised and silently 404 every
    buyer call."""

    class _TypoPlatform(DecisioningPlatform):
        # Missing 'n' in "non-guaranteed".
        capabilities = DecisioningCapabilities(
            specialisms=["sales-non-guarateed"],
        )
        accounts = SingletonAccounts(account_id="hello")

    with pytest.raises(AdcpError) as exc_info:
        validate_platform(_TypoPlatform())
    assert exc_info.value.code == "INVALID_REQUEST"
    msg = str(exc_info.value)
    assert "did you mean 'sales-non-guaranteed'" in msg.lower()
    # Details carry the structured suggestion for tooling.
    suggestions = exc_info.value.details["typo_suggestions"]
    assert {"claimed": "sales-non-guarateed", "did_you_mean": "sales-non-guaranteed"} in suggestions


def test_validate_platform_governance_aware_required_for_governance_specialism() -> None:
    """A platform claiming a governance-* specialism without setting
    capabilities.governance_aware=True fails fast — silent gate
    skipping is a security regression. (D15 round-4)

    Use ``governance-aware-seller`` because it's in
    GOVERNANCE_SPECIALISMS but NOT in REQUIRED_METHODS_PER_SPECIALISM
    — isolates the governance-aware security gate from the
    required-method gate (the latter is exercised in
    ``test_decisioning_specialisms.py``)."""

    class _GovernanceWithoutOptInPlatform(DecisioningPlatform):
        capabilities = DecisioningCapabilities(
            specialisms=["governance-aware-seller"],
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
    StateReader is supplied by serve()/dispatch.)

    Use ``governance-aware-seller`` to keep this test isolated from
    required-method coverage (which is what
    ``test_decisioning_specialisms.py`` covers for the two
    governance-AGENT slugs that DO have method-coverage rules)."""

    class _GovernanceOptInPlatform(DecisioningPlatform):
        capabilities = DecisioningCapabilities(
            specialisms=["governance-aware-seller"],
            governance_aware=True,
        )
        accounts = SingletonAccounts(account_id="hello")

    # ``governance-aware-seller`` is unenforced in
    # REQUIRED_METHODS_PER_SPECIALISM (it's a SELLER claim, not a
    # governance-AGENT slug — see governance.py module docstring),
    # so it'll emit a "spec-recognized but unenforced" UserWarning.
    # That's fine — the governance_aware flag is what we're testing.
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
    spec churn surfaces as a visible test failure. Slugs covered are
    only those in the spec enum that the v6.0 framework enforces
    method coverage for; non-sales spec slugs (signal-*, audience-sync,
    creative-*, governance-*) emit "unenforced specialism" UserWarning
    until their per-Protocol coverage lands in v6.1+."""
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
        "sales-social",
        "sales-proposal-mode",
    ):
        assert REQUIRED_METHODS_PER_SPECIALISM[slug] == expected_core, f"sales core drift on {slug}"


def test_required_methods_only_contains_spec_slugs() -> None:
    """Every key in REQUIRED_METHODS_PER_SPECIALISM MUST be a real
    spec specialism slug. Round-5 Emma review: shipping invented slugs
    (e.g. ``sales-streaming-tv``) made adopters claiming non-spec
    specialisms pass validation — silent buyer compatibility break."""
    invented = set(REQUIRED_METHODS_PER_SPECIALISM.keys()) - SPEC_SPECIALISM_ENUM
    assert invented == set(), (
        f"REQUIRED_METHODS_PER_SPECIALISM contains slugs not in the spec "
        f"enum: {sorted(invented)}. Either drop them or add the slug to "
        f"schemas/cache/enums/specialism.json upstream."
    )


def test_spec_specialism_enum_matches_schema_cache() -> None:
    """SPEC_SPECIALISM_ENUM mirrors ``schemas/cache/enums/specialism.json``
    verbatim. CI catches out-of-band drift when the schema cache
    refreshes from upstream."""
    import json
    from pathlib import Path

    schema_path = Path(__file__).parent.parent / "schemas" / "cache" / "enums" / "specialism.json"
    with schema_path.open() as f:
        on_disk = frozenset(json.load(f)["enum"])
    assert SPEC_SPECIALISM_ENUM == on_disk, (
        f"SPEC_SPECIALISM_ENUM drifted from on-disk spec enum. "
        f"Missing from constant: {sorted(on_disk - SPEC_SPECIALISM_ENUM)}; "
        f"extra in constant: {sorted(SPEC_SPECIALISM_ENUM - on_disk)}."
    )


def test_validate_platform_warns_on_unenforced_spec_specialism() -> None:
    """Spec-recognized specialism that the v6.0 framework doesn't
    enforce method coverage for emits an "unenforced specialism"
    UserWarning — distinct from the "novel" warning, since it's a
    real claim, just not method-checked.

    After breadth-sprint Batch 4, ``governance-aware-seller`` is the
    ONLY spec specialism slug staying unenforced — by design, since
    it's a SELLER composition claim (a sales-* archetype that
    integrates with a governance agent via sync_governance +
    check_governance), NOT a wire-implementor claim. Adopters claim
    it to signal "this seller composes with governance" without
    implementing CampaignGovernancePlatform themselves.

    Note: ``governance-aware-seller`` is also in
    GOVERNANCE_SPECIALISMS, so a platform claiming it without
    ``governance_aware=True`` ALSO trips the security gate. This
    test sets ``governance_aware=True`` so we hit the unenforced
    warning path cleanly, isolated from the security gate."""

    class _UnenforcedSpecPlatform(DecisioningPlatform):
        capabilities = DecisioningCapabilities(
            specialisms=["governance-aware-seller"],
            governance_aware=True,
        )
        accounts = SingletonAccounts(account_id="hello")

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always", UserWarning)
        validate_platform(_UnenforcedSpecPlatform())
    matched = [w for w in caught if "governance-aware-seller" in str(w.message)]
    assert len(matched) == 1
    assert "spec-recognized" in str(matched[0].message)


def test_validate_platform_typo_check_uses_spec_enum() -> None:
    """Typo detector matches against the full spec enum, not just
    REQUIRED_METHODS keys. A typo of ``signal-marketplace`` (a spec
    slug we don't yet enforce coverage for) still trips the hard fail
    with a "did you mean…" hint."""

    class _TypoOfSpecSlugPlatform(DecisioningPlatform):
        # Missing 'l' in "marketplace".
        capabilities = DecisioningCapabilities(specialisms=["signal-marketpace"])
        accounts = SingletonAccounts(account_id="hello")

    with pytest.raises(AdcpError) as exc_info:
        validate_platform(_TypoOfSpecSlugPlatform())
    assert exc_info.value.code == "INVALID_REQUEST"
    msg = str(exc_info.value).lower()
    assert "did you mean 'signal-marketplace'" in msg


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
    # debugging.
    assert isinstance(exc_info.value.__cause__, ValueError)
    # Wire ``message`` cites the exception class so adopters get a
    # breadcrumb without having to grep server logs (Emma AudioStack
    # P2: "An internal error occurred" was a dead end).
    assert "ValueError" in str(exc_info.value)
    assert "get_products" in str(exc_info.value)
    # Wire ``details.caused_by`` carries ONLY the exception class —
    # full str/traceback stays in server logs. The class name is the
    # triage breadcrumb; the exception's str() is omitted on the wire
    # because any truncation length useful for diagnostics also fits a
    # full bearer token / OAuth secret.
    assert exc_info.value.details["caused_by"] == {"type": "ValueError"}
    assert "message" not in exc_info.value.details["caused_by"]


@pytest.mark.asyncio
async def test_invoke_internal_error_omits_exception_str(
    executor: ThreadPoolExecutor,
) -> None:
    """Defense-in-depth: ``caused_by.message`` is omitted entirely.
    Any truncation length useful for diagnostics also fits a full
    OAuth client secret or bearer token, so the wire surfaces only
    the exception class name. Full repr / traceback stays in server
    logs via :func:`logger.exception`."""

    class _BlowupPlatform(DecisioningPlatform):
        capabilities = DecisioningCapabilities()
        accounts = SingletonAccounts(account_id="x")

        async def get_products(self, req, ctx):
            # Realistic credential-leak shape: an adopter raises with
            # the bearer in the exception message.
            raise RuntimeError("upstream call failed: Bearer eyJhbGciOiJIUzI1NiJ9.payload.sig")

    ctx = _build_request_context(ToolContext(), Account(id="x"), None)
    with pytest.raises(AdcpError) as exc_info:
        await _invoke_platform_method(
            _BlowupPlatform(),
            "get_products",
            _ProductsRequest(),
            ctx,
            executor=executor,
            registry=InMemoryTaskRegistry(),
        )
    caused_by = exc_info.value.details["caused_by"]
    assert caused_by == {"type": "RuntimeError"}
    # The bearer-shaped string MUST NOT appear anywhere on the wire.
    assert "Bearer" not in str(exc_info.value.details)
    assert "eyJhbGciOiJIUzI1NiJ9" not in str(exc_info.value.details)


@pytest.mark.asyncio
async def test_invoke_validation_error_surfaces_narrowed_field_paths(
    executor: ThreadPoolExecutor,
) -> None:
    """When the platform method raises ``pydantic.ValidationError``
    directly — typically because the seller constructed an invalid
    response model — the wire ``details`` MUST carry the narrowed
    field-path list so the buyer agent sees what failed (Stability AI
    Emma P1: pre-fix wire said "see details for cause" with empty
    details). Field paths are pulled from
    ``ValidationError.errors()`` and run through
    ``narrow_union_errors`` to filter discriminated-union noise."""
    from pydantic import BaseModel

    class _ResponseModel(BaseModel):
        creative_id: str
        width: int
        height: int

    class _CrashingPlatform(DecisioningPlatform):
        capabilities = DecisioningCapabilities()
        accounts = SingletonAccounts(account_id="x")

        async def get_products(self, req, ctx):
            # Seller-side bug: building a response with missing fields.
            # Realistic shape: an adopter calling
            # CreativeManifest(...) with a missing required ``url`` on
            # ImageContent.
            _ResponseModel.model_validate({"creative_id": "cr-1"})

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
    # ``caused_by`` still surfaces the exception class for triage.
    assert exc_info.value.details["caused_by"]["type"] == "ValidationError"
    # NEW: ``validation_errors`` is populated with structured field
    # paths so the buyer agent (and the seller's wire log) see the
    # actual missing fields.
    validation_errors = exc_info.value.details["validation_errors"]
    missing_fields = {err["loc"][-1] for err in validation_errors if err["type"] == "missing"}
    assert "width" in missing_fields and "height" in missing_fields


@pytest.mark.asyncio
async def test_invoke_arg_projector_signature_drift_projects_invalid_request(
    executor: ThreadPoolExecutor,
) -> None:
    """When an adopter renames a Pydantic field projected via
    arg_projector (e.g., ``patch`` → ``update``), the framework's
    kwargs-unpack hits TypeError. Round-4 review P1: project to
    INVALID_REQUEST with a hint, NOT bare INTERNAL_ERROR — adopters
    fix the signature without a server-log dive."""
    from pydantic import BaseModel

    class _PatchModel(BaseModel):
        media_buy_id: str

    class _DriftedPlatform(DecisioningPlatform):
        capabilities = DecisioningCapabilities()
        accounts = SingletonAccounts(account_id="x")

        # Adopter renamed `patch` → `update_data`. Wire shape still
        # has both fields, but our arg_projector kwargs key mismatches.
        async def update_media_buy(self, media_buy_id, update_data, ctx):
            return {}

    ctx = _build_request_context(ToolContext(), Account(id="x"), None)
    patch = _PatchModel(media_buy_id="mb_1")
    with pytest.raises(AdcpError) as exc_info:
        await _invoke_platform_method(
            _DriftedPlatform(),
            "update_media_buy",
            patch,
            ctx,
            executor=executor,
            registry=InMemoryTaskRegistry(),
            arg_projector={"media_buy_id": "mb_1", "patch": patch},
        )
    assert exc_info.value.code == "INVALID_REQUEST"
    msg = str(exc_info.value)
    assert "signature mismatch" in msg
    assert "update_media_buy" in msg


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
    """The synchronous return is the wire Submitted envelope per
    ``schemas/cache/core/protocol-envelope.json`` — only ``task_id`` +
    ``status``. ``task_type`` lives on TaskRecord (for tasks/get
    reads) but never on the wire envelope; leaking the Python method
    name would couple the wire to handler-internal naming."""
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
    assert envelope["task_id"].startswith("task_")
    # Spec: Submitted wire envelope is {task_id, status} only.
    assert "task_type" not in envelope
    assert set(envelope.keys()) == {"task_id", "status"}

    # Wait for the background task to complete so the assertion below
    # is deterministic. (CI may schedule background tasks slowly.)
    await asyncio.wait_for(completed.wait(), timeout=2.0)
    # Yield once more so the registry.complete() call lands.
    await asyncio.sleep(0.05)

    # task_type IS on TaskRecord (registry surface) — buyer-side
    # tasks/get round-trips it; handler-internal use only.
    rec = await registry.get(envelope["task_id"], expected_account_id="acct_a")
    assert rec is not None
    assert rec["state"] == "completed"
    assert rec["task_type"] == "create_media_buy"
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
async def test_handoff_request_context_echoes_into_completed_task(
    executor: ThreadPoolExecutor,
) -> None:
    """Issue #563: when ``request_params`` is supplied with a
    ``context`` field, the registry-stored success envelope echoes
    that context. Buyer polling ``tasks/get`` on the completed task
    sees the same ``context`` they sent on the kick-off request —
    symmetric with the sync path's :func:`inject_context` and PR
    #560's AdcpError raise path."""
    from pydantic import BaseModel as _Req

    class _ReqWithContext(_Req):
        idempotency_key: str
        context: dict[str, Any]

    registry = InMemoryTaskRegistry()
    ctx = _build_request_context(ToolContext(), Account(id="acct_a"), None)
    req = _ReqWithContext(idempotency_key="key-1", context={"correlation_id": "buyer-563"})

    async def _handoff_fn(task_ctx):
        return {"media_buy_id": "mb_1"}

    envelope = await _project_handoff(
        TaskHandoff(_handoff_fn),
        ctx,
        method_name="create_media_buy",
        registry=registry,
        executor=executor,
        request_params=req,
    )
    await asyncio.sleep(0.1)
    rec = await registry.get(envelope["task_id"], expected_account_id="acct_a")
    assert rec is not None
    assert rec["state"] == "completed"
    assert rec["result"]["context"] == {"correlation_id": "buyer-563"}


@pytest.mark.asyncio
async def test_handoff_request_context_echoes_into_failed_task(
    executor: ThreadPoolExecutor,
) -> None:
    """Same echo on the AdcpError-raised path: registry.fail's wire
    envelope carries the request's ``context`` alongside the
    ``adcp_error`` shape."""
    from pydantic import BaseModel as _Req

    class _ReqWithContext(_Req):
        idempotency_key: str
        context: dict[str, Any]

    registry = InMemoryTaskRegistry()
    ctx = _build_request_context(ToolContext(), Account(id="acct_a"), None)
    req = _ReqWithContext(idempotency_key="key-2", context={"correlation_id": "buyer-fail-563"})

    async def _handoff_fn(task_ctx):
        raise AdcpError("POLICY_VIOLATION", message="rejected", recovery="correctable")

    envelope = await _project_handoff(
        TaskHandoff(_handoff_fn),
        ctx,
        method_name="create_media_buy",
        registry=registry,
        executor=executor,
        request_params=req,
    )
    await asyncio.sleep(0.1)
    rec = await registry.get(envelope["task_id"], expected_account_id="acct_a")
    assert rec is not None
    assert rec["state"] == "failed"
    assert rec["error"]["code"] == "POLICY_VIOLATION"
    # Context echoed on the failed-task envelope, sibling of the
    # ``adcp_error`` payload (mirrors PR #560's structuredContent shape).
    assert rec["error"].get("context") == {"correlation_id": "buyer-fail-563"}


@pytest.mark.asyncio
async def test_handoff_unexpected_exception_echoes_context_too(
    executor: ThreadPoolExecutor,
) -> None:
    """Non-AdcpError exception → wrapped INTERNAL_ERROR → still
    echoes context. The wrap path was the salesagent gap (#562
    follow-up territory) and the same fix applies on the bg path."""
    from pydantic import BaseModel as _Req

    class _ReqWithContext(_Req):
        idempotency_key: str
        context: dict[str, Any]

    registry = InMemoryTaskRegistry()
    ctx = _build_request_context(ToolContext(), Account(id="acct_a"), None)
    req = _ReqWithContext(idempotency_key="key-3", context={"correlation_id": "buyer-internal-563"})

    async def _handoff_fn(task_ctx):
        raise RuntimeError("bug")

    envelope = await _project_handoff(
        TaskHandoff(_handoff_fn),
        ctx,
        method_name="create_media_buy",
        registry=registry,
        executor=executor,
        request_params=req,
    )
    await asyncio.sleep(0.1)
    rec = await registry.get(envelope["task_id"], expected_account_id="acct_a")
    assert rec is not None
    assert rec["error"]["code"] == "INTERNAL_ERROR"
    assert rec["error"].get("context") == {"correlation_id": "buyer-internal-563"}


@pytest.mark.asyncio
async def test_handoff_no_request_params_no_context_synthesised(
    executor: ThreadPoolExecutor,
) -> None:
    """When ``request_params`` is None (test fixtures, custom dispatch),
    no context echo happens — the registry stores the wire envelope
    as-is."""
    registry = InMemoryTaskRegistry()
    ctx = _build_request_context(ToolContext(), Account(id="acct_a"), None)

    async def _handoff_fn(task_ctx):
        return {"media_buy_id": "mb_no_params"}

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
    assert "context" not in rec["result"]


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
    # Returned the wire envelope, NOT the handoff marker. The wire
    # shape is {task_id, status} only — task_type lives on the
    # registry for tasks/get reads.
    assert isinstance(result, dict)
    assert result["status"] == "submitted"
    assert "task_type" not in result
