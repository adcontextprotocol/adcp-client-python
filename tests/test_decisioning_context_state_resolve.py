"""D15 — RequestContext typed sub-readers.

Covers the surface added in round-4 of the dispatch design review:

* :class:`adcp.decisioning.StateReader` Protocol structural matching
* :class:`adcp.decisioning.ResourceResolver` Protocol structural matching
* :class:`adcp.decisioning.state._NotYetWiredStateReader` v6.0 stub —
  empty returns + one-time UserWarning per method
* :class:`adcp.decisioning.resolve._NotYetWiredResolver` v6.0 stub —
  raises NotImplementedError with design-doc anchor
* ``creative_format(revalidate=True)`` parameter contract — the stub
  raises identically regardless of the flag (parameter is part of the
  Protocol, not gated on backing impl)
* ``dataclasses.replace(ctx, state=fake)`` test-double substitution
  round-trip
* ``capabilities.governance_aware`` opt-in and the default
  :data:`adcp.decisioning.GOVERNANCE_SPECIALISMS` constant
"""

from __future__ import annotations

import dataclasses
import warnings
from collections.abc import Sequence

import pytest

from adcp.decisioning import (
    GOVERNANCE_SPECIALISMS,
    DecisioningCapabilities,
    Proposal,
    RequestContext,
    ResourceResolver,
    StateReader,
    WorkflowObjectType,
    WorkflowStep,
)
from adcp.decisioning.resolve import _NotYetWiredResolver
from adcp.decisioning.state import (
    _NotYetWiredStateReader,
    _reset_state_stub_warned,
)


@pytest.fixture(autouse=True)
def reset_state_stub_warned():
    """Clear the module-level warned-once set before each test so
    one-time UserWarning assertions don't see prior tests' state."""
    _reset_state_stub_warned()


# ---- Protocol structural matching ----


def test_state_reader_protocol_runtime_checkable() -> None:
    """``StateReader`` is a runtime-checkable Protocol — adopters
    writing custom impls satisfy the contract structurally without
    inheritance."""
    assert isinstance(_NotYetWiredStateReader(), StateReader)


def test_resource_resolver_protocol_runtime_checkable() -> None:
    """Same structural check for ``ResourceResolver``."""
    assert isinstance(_NotYetWiredResolver(), ResourceResolver)


def test_custom_state_reader_satisfies_protocol() -> None:
    """An adopter-written class with the right method shapes satisfies
    the Protocol without subclassing."""

    class _CustomStateReader:
        def find_by_object(
            self, object_type: WorkflowObjectType, object_id: str
        ) -> Sequence[WorkflowStep]:
            return ()

        def find_proposal_by_id(self, proposal_id: str) -> Proposal | None:
            return None

        def governance_context(self):  # type: ignore[no-untyped-def]
            return None

        def workflow_steps(self) -> Sequence[WorkflowStep]:
            return ()

    assert isinstance(_CustomStateReader(), StateReader)


# ---- _NotYetWiredStateReader: empty returns + one-time UserWarning ----


def test_state_stub_find_by_object_returns_empty_and_warns_once() -> None:
    """First call emits ``UserWarning``; subsequent calls return empty
    silently. Regression: warned-once state is module-level so concurrent
    request handlers share suppression after the first call per process."""
    reader = _NotYetWiredStateReader()
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always", UserWarning)
        first = reader.find_by_object("media_buy", "mb_1")
        second = reader.find_by_object("media_buy", "mb_2")
    assert first == ()
    assert second == ()
    matched = [w for w in caught if "find_by_object" in str(w.message)]
    assert len(matched) == 1
    assert "v6.0 stub" in str(matched[0].message)
    assert "v6.1" in str(matched[0].message)
    assert "#d15" in str(matched[0].message)


def test_state_stub_find_proposal_by_id_returns_none_and_warns_once() -> None:
    reader = _NotYetWiredStateReader()
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always", UserWarning)
        result = reader.find_proposal_by_id("proposal_xyz")
        reader.find_proposal_by_id("proposal_abc")  # 2nd call, no warning
    assert result is None
    matched = [w for w in caught if "find_proposal_by_id" in str(w.message)]
    assert len(matched) == 1


def test_state_stub_governance_context_returns_none_and_warns_once() -> None:
    """Reaching ``governance_context()`` against the stub means the
    governance opt-in fail-fast wasn't tripped — adopter is in a
    non-governance flow. Warning fires once; result is ``None`` (no
    governance threaded)."""
    reader = _NotYetWiredStateReader()
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always", UserWarning)
        result = reader.governance_context()
        reader.governance_context()
    assert result is None
    matched = [w for w in caught if "governance_context" in str(w.message)]
    assert len(matched) == 1


def test_state_stub_workflow_steps_returns_empty_and_warns_once() -> None:
    reader = _NotYetWiredStateReader()
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always", UserWarning)
        result = reader.workflow_steps()
        reader.workflow_steps()
    assert result == ()
    matched = [w for w in caught if "workflow_steps" in str(w.message)]
    assert len(matched) == 1


def test_state_stub_separate_methods_warn_independently() -> None:
    """Each method's warned-once is keyed by method name — calling
    ``find_by_object`` once doesn't suppress the first
    ``workflow_steps`` warning."""
    reader = _NotYetWiredStateReader()
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always", UserWarning)
        reader.find_by_object("media_buy", "mb_1")
        reader.workflow_steps()
        reader.find_by_object("creative", "cr_1")  # suppressed
        reader.workflow_steps()  # suppressed
    methods_warned = [
        m
        for m in (
            "find_by_object",
            "workflow_steps",
            "find_proposal_by_id",
            "governance_context",
        )
        if any(m in str(w.message) for w in caught)
    ]
    assert sorted(methods_warned) == ["find_by_object", "workflow_steps"]


def test_state_stub_warned_once_is_cross_instance() -> None:
    """``_STATE_STUB_WARNED`` is module-level so concurrent ``serve()``
    instances share the warned-once state — emitting per process per
    method, not per request. Two stub instances back-to-back must not
    re-warn for the same method."""
    first = _NotYetWiredStateReader()
    second = _NotYetWiredStateReader()
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always", UserWarning)
        first.find_by_object("media_buy", "mb_1")
        # Different instance, same method — must NOT re-warn.
        second.find_by_object("media_buy", "mb_2")
    matched = [w for w in caught if "find_by_object" in str(w.message)]
    assert len(matched) == 1, (
        f"Expected exactly one warning across instances; got {len(matched)}: "
        f"{[str(w.message) for w in matched]}"
    )


def test_state_stub_governance_context_warning_text() -> None:
    """The ``governance_context`` warning text is special-cased to
    explain that ``None`` IS the v6.1 answer for non-governance flows
    — not the generic "different values once wired" message that
    applies to other methods. Adopters in non-governance flows
    shouldn't be told the value will change when it won't."""
    reader = _NotYetWiredStateReader()
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always", UserWarning)
        reader.governance_context()
    msg = next(str(w.message) for w in caught if "governance_context" in str(w.message))
    assert "non-governance flows get the same answer" in msg
    assert "fail-fast" in msg


def test_default_state_reader_is_module_singleton() -> None:
    """Round-4 review: ``_make_default_state_reader`` returns the same
    instance across calls (module-level singleton). Per-RequestContext
    stub allocation buys nothing since the warned-once set is also
    module-level — singleton matches the contract and avoids stub
    churn."""
    from adcp.decisioning.state import _make_default_state_reader

    a = _make_default_state_reader()
    b = _make_default_state_reader()
    assert a is b


def test_default_resolver_is_module_singleton() -> None:
    """Same singleton pattern for ``_make_default_resolver``."""
    from adcp.decisioning.resolve import _make_default_resolver

    a = _make_default_resolver()
    b = _make_default_resolver()
    assert a is b


def test_request_context_default_factories_share_singleton() -> None:
    """Each RequestContext instance shares the same default stub
    instances — no per-context allocation. Verifies the field
    default_factory plumbing reads the singletons correctly."""
    a = RequestContext()
    b = RequestContext()
    assert a.state is b.state
    assert a.resolve is b.resolve


def test_property_list_alias_pinned_to_reference() -> None:
    """``adcp.decisioning.PropertyList`` aliases
    ``PropertyListReference`` deliberately (the spec models both as
    one Pydantic class). If a future spec rev introduces a distinct
    resolved-list type, adopter code typed against ``PropertyList``
    would silently re-target — this contract test trips first so the
    rename is visible at CI time rather than deploy time."""
    from adcp.decisioning import PropertyList, PropertyListReference

    assert PropertyList is PropertyListReference, (
        "PropertyList must alias PropertyListReference. If the spec has "
        "introduced a distinct resolved-list type, update "
        "adcp.decisioning.resolve to point PropertyList at the new class "
        "and migrate adopter code accordingly."
    )


# ---- _NotYetWiredResolver: raises with design-doc anchor ----


@pytest.mark.asyncio
async def test_resolve_stub_property_list_raises_with_anchor() -> None:
    """Resolver stub raises ``NotImplementedError`` with the design-doc
    anchor in the message — adopters reaching for ``ctx.resolve.*`` get
    a locatable failure pointing at the v6.1 follow-up."""
    resolver = _NotYetWiredResolver()
    with pytest.raises(NotImplementedError) as exc_info:
        await resolver.property_list("list_xyz")
    msg = str(exc_info.value)
    assert "list_xyz" in msg
    assert "v6.0 stub" in msg
    assert "v6.1" in msg
    assert "#d15" in msg


@pytest.mark.asyncio
async def test_resolve_stub_collection_list_raises() -> None:
    resolver = _NotYetWiredResolver()
    with pytest.raises(NotImplementedError):
        await resolver.collection_list("coll_xyz")


@pytest.mark.asyncio
async def test_resolve_stub_creative_format_raises_with_revalidate_false() -> None:
    """Default ``revalidate=False`` raises with the same shape as the
    other stubs."""
    from adcp.types import FormatReferenceStructuredObject

    resolver = _NotYetWiredResolver()
    fmt = FormatReferenceStructuredObject(
        agent_url="https://creative.adcontextprotocol.org",
        id="display_static",
    )
    with pytest.raises(NotImplementedError) as exc_info:
        await resolver.creative_format(fmt)
    assert "creative_format" in str(exc_info.value)
    assert "revalidate=False" in str(exc_info.value)


@pytest.mark.asyncio
async def test_resolve_stub_creative_format_raises_with_revalidate_true() -> None:
    """``revalidate=True`` ALSO raises — the parameter is part of the
    Protocol contract, NOT gated on the backing impl. Adopters who need
    ``revalidate=True`` semantics in v6.0 wire a custom resolver; they
    don't get a different stub path for the flag."""
    from adcp.types import FormatReferenceStructuredObject

    resolver = _NotYetWiredResolver()
    fmt = FormatReferenceStructuredObject(
        agent_url="https://creative.adcontextprotocol.org",
        id="display_static",
    )
    with pytest.raises(NotImplementedError) as exc_info:
        await resolver.creative_format(fmt, revalidate=True)
    assert "creative_format" in str(exc_info.value)
    assert "revalidate=True" in str(exc_info.value)


# ---- RequestContext: defaults wire the stubs ----


def test_request_context_defaults_to_stubs() -> None:
    """Constructing ``RequestContext()`` without explicit ``state`` /
    ``resolve`` wires the v6.0 stub impls. Test fixtures and
    ``examples/hello_seller.py`` rely on this for zero-config setup."""
    ctx = RequestContext()
    assert isinstance(ctx.state, _NotYetWiredStateReader)
    assert isinstance(ctx.resolve, _NotYetWiredResolver)
    assert ctx.auth_principal is None


# ---- dataclasses.replace test-double substitution ----


def test_dataclasses_replace_substitutes_state_reader() -> None:
    """Tests substitute test doubles via ``dataclasses.replace``, NOT
    raw construction (which would bypass the framework hydration helper
    in production)."""

    class _FakeStateReader:
        def __init__(self) -> None:
            self.calls: list[str] = []

        def find_by_object(self, object_type, object_id):  # type: ignore[no-untyped-def]
            self.calls.append(f"find_by_object({object_type},{object_id})")
            return ()

        def find_proposal_by_id(self, proposal_id):  # type: ignore[no-untyped-def]
            return None

        def governance_context(self):  # type: ignore[no-untyped-def]
            return None

        def workflow_steps(self):  # type: ignore[no-untyped-def]
            return ()

    fake = _FakeStateReader()
    base_ctx = RequestContext()
    test_ctx = dataclasses.replace(base_ctx, state=fake)

    test_ctx.state.find_by_object("media_buy", "mb_1")
    assert fake.calls == ["find_by_object(media_buy,mb_1)"]
    assert isinstance(base_ctx.state, _NotYetWiredStateReader), (
        "replace should NOT mutate the original ctx — base_ctx.state stays " "the default stub"
    )


def test_dataclasses_replace_substitutes_resolver() -> None:
    """Same substitution pattern for ``resolve``."""

    class _FakeResolver:
        async def property_list(self, list_id):  # type: ignore[no-untyped-def]
            return f"resolved:{list_id}"

        async def collection_list(self, list_id):  # type: ignore[no-untyped-def]
            return f"coll:{list_id}"

        async def creative_format(self, format_id, *, revalidate=False):  # type: ignore[no-untyped-def]
            return f"fmt:{format_id}:{revalidate}"

    fake = _FakeResolver()
    test_ctx = dataclasses.replace(RequestContext(), resolve=fake)
    assert test_ctx.resolve is fake


# ---- governance opt-in / GOVERNANCE_SPECIALISMS ----


def test_capabilities_governance_aware_defaults_false() -> None:
    """Non-governance adopters never touch this flag — it stays
    ``False`` by default. Adopters claiming ``governance-*`` specialisms
    must explicitly opt in (and wire a real ``StateReader``); otherwise
    server boot fails fast in ``validate_platform``."""
    caps = DecisioningCapabilities()
    assert caps.governance_aware is False


def test_governance_specialisms_pinned() -> None:
    """The constant tracks every ``governance-*`` slug in the spec
    enum (``schemas/cache/enums/specialism.json``). Drift here is a
    foundation-PR-level decision; this test is the locked contract.

    Includes ``governance-aware-seller`` — a seller agent that
    composes with a buyer's governance agent reads governance context
    per-request, so the gate must catch it claiming the specialism
    without wiring the StateReader (round-5 Emma P0)."""
    assert GOVERNANCE_SPECIALISMS == frozenset(
        {
            "governance-aware-seller",
            "governance-delivery-monitor",
            "governance-spend-authority",
        }
    )


def test_capabilities_can_opt_into_governance_aware() -> None:
    """Adopters wiring real governance set this True alongside their
    custom ``StateReader``. The flag itself doesn't validate; the
    fail-fast logic lives in dispatch ``validate_platform`` (foundation
    PR). v6.0 ships the contract."""
    caps = DecisioningCapabilities(
        specialisms=["governance-spend-authority"],
        governance_aware=True,
    )
    assert caps.governance_aware is True
    assert "governance-spend-authority" in caps.specialisms
