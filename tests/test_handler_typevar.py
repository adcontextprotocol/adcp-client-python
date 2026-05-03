"""Runtime coverage for ``ADCPHandler[TContext]`` — closes #223.

The TypeVar work is a typing-level refactor (mypy-visible), but the
contract it promises has runtime consequences too:

1. Existing ``class MyAgent(ADCPHandler)`` code keeps working without
   edits — unparameterised subclasses must not break.
2. Parameterising with a ``ToolContext`` subclass is a legal Generic
   subscription — ``ADCPHandler[MyContext]`` resolves at class-body
   time.
3. Protocol handlers (``BrandHandler``, ``ContentStandardsHandler`` etc.)
   propagate the same TypeVar — downstream can write
   ``class MyBrand(BrandHandler[MyContext])``.
4. At dispatch time, the handler method receives whatever ``ToolContext``
   subclass the transport hands it — no isinstance check loses the
   subclass type.

These tests are behavioural, not type-system assertions — they verify
the TypeVar machinery doesn't impose a runtime cost and that the
subclass flows through the A2A/MCP invocation paths.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest

from adcp.server import (
    ADCPHandler,
    BrandHandler,
    ComplianceHandler,
    ContentStandardsHandler,
    GovernanceHandler,
    SponsoredIntelligenceHandler,
    TmpHandler,
    ToolContext,
)
from adcp.server.base import TContext  # noqa: F401 — imported to pin the export


@dataclass
class _PlatformAdapter:
    """Stand-in for a real platform adapter — the typed field a downstream
    would carry on their ToolContext subclass."""

    name: str


@dataclass
class _TypedContext(ToolContext):
    """Demonstrates the multi-tenant pattern: handlers need typed access
    to tenant + adapter fields beyond what ToolContext names."""

    adapter: _PlatformAdapter | None = None


# ---------------------------------------------------------------------------
# Unparameterised subclasses — existing pattern must keep working
# ---------------------------------------------------------------------------


def test_unparameterised_subclass_still_works():
    """``class MyAgent(ADCPHandler)`` with no TypeVar argument must
    keep working for backward compat. The bulk of existing adopters
    aren't ready to introduce typed context subclasses yet."""

    class _MyAgent(ADCPHandler):
        _agent_type = "test"

        async def get_adcp_capabilities(self, params, context=None):
            return {"adcp": {"major_versions": [3]}}

    agent = _MyAgent()
    assert agent._agent_type == "test"


def test_unparameterised_protocol_handler_still_works():
    """Backward-compat check for every protocol handler base —
    unparameterised subclasses must keep instantiating.

    ``BrandHandler``, ``ComplianceHandler``, ``TmpHandler`` are
    non-abstract and subclass directly.

    ``ContentStandardsHandler``, ``GovernanceHandler``, and
    ``SponsoredIntelligenceHandler`` declare ``handle_<tool>`` abstract
    methods (predating this PR). We build minimal concrete subclasses
    that stub every abstract so we can prove the TypeVar refactor
    didn't accidentally add a new unimplementable abstract on the base.
    """
    for cls in (BrandHandler, ComplianceHandler, TmpHandler):

        class _Concrete(cls):  # type: ignore[valid-type,misc]
            _agent_type = "test"

            async def get_adcp_capabilities(self, params, context=None):
                return {"adcp": {"major_versions": [3]}}

        instance = _Concrete()
        assert instance._agent_type == "test"

    # Abstract bases — build concrete via a type() call so every
    # abstract handle_<tool> gets a stub in the class namespace at
    # creation time (ABC machinery freezes __abstractmethods__ there).
    for abstract_base in (
        ContentStandardsHandler,
        GovernanceHandler,
        SponsoredIntelligenceHandler,
    ):
        abstracts = {
            name
            for name in dir(abstract_base)
            if name.startswith("handle_")
            and getattr(getattr(abstract_base, name, None), "__isabstractmethod__", False)
        }

        async def _capabilities(self, params, context=None):  # noqa: ARG001
            return {"adcp": {"major_versions": [3]}}

        async def _stub(self, request, context=None):  # noqa: ARG001
            return {}

        namespace: dict[str, Any] = {
            "_agent_type": "test",
            "get_adcp_capabilities": _capabilities,
        }
        for _name in abstracts:
            namespace[_name] = _stub

        concrete = type(f"_{abstract_base.__name__}Concrete", (abstract_base,), namespace)
        instance = concrete()
        assert (
            instance._agent_type == "test"
        ), f"{abstract_base.__name__} unparameterised subclass failed to instantiate"


# ---------------------------------------------------------------------------
# Parameterised subclasses — the new capability
# ---------------------------------------------------------------------------


def test_parameterised_adcphandler_subclass_resolves():
    """``class MyAgent(ADCPHandler[MyContext])`` must construct without
    error — the Generic subscription is the core promise of #223."""

    class _TypedAgent(ADCPHandler[_TypedContext]):
        _agent_type = "typed"

        async def get_adcp_capabilities(self, params, context=None):
            return {"adcp": {"major_versions": [3]}}

    agent = _TypedAgent()
    # __class_getitem__ returned something sensible — we can subclass it
    # and instantiate the subclass.
    assert agent._agent_type == "typed"


def test_protocol_handler_propagates_typevar():
    """``BrandHandler[MyContext]`` must work the same way.  Without
    this the TypeVar on the base is useless for the specialised
    handler classes."""

    class _TypedBrand(BrandHandler[_TypedContext]):
        _agent_type = "typed brand"

    agent = _TypedBrand()
    assert agent._agent_type == "typed brand"


def test_handler_receives_subclass_at_dispatch_time():
    """The TypeVar is static-type narrowing, but the runtime path must
    preserve the subclass identity on the ``context`` argument — a
    handler that does ``context.adapter`` at runtime needs the subclass
    to survive the dispatch."""
    received: list[Any] = []

    class _TypedAgent(ADCPHandler[_TypedContext]):
        _agent_type = "adapter-reader"

        async def get_adcp_capabilities(self, params, context=None):
            received.append(context)
            return {"adcp": {"major_versions": [3]}}

    import asyncio

    agent = _TypedAgent()
    ctx = _TypedContext(
        caller_identity="p-1",
        tenant_id="t-1",
        adapter=_PlatformAdapter(name="demo"),
    )
    asyncio.run(agent.get_adcp_capabilities({}, ctx))

    assert len(received) == 1
    got = received[0]
    assert isinstance(got, _TypedContext)
    assert got.adapter is not None
    assert got.adapter.name == "demo"
    assert got.caller_identity == "p-1"
    assert got.tenant_id == "t-1"


def test_protocol_handler_subclass_receives_typed_context():
    """End-to-end for a specialised handler: BrandHandler[MyContext]
    subclass's methods receive the typed subclass at dispatch."""
    received: list[Any] = []

    class _TypedBrand(BrandHandler[_TypedContext]):
        _agent_type = "typed-brand"

        async def get_adcp_capabilities(self, params, context=None):
            received.append(context)
            return {"adcp": {"major_versions": [3]}}

    import asyncio

    agent = _TypedBrand()
    ctx = _TypedContext(
        caller_identity="brand-p",
        adapter=_PlatformAdapter(name="brand-adapter"),
    )
    asyncio.run(agent.get_adcp_capabilities({}, ctx))

    assert isinstance(received[0], _TypedContext)
    assert received[0].adapter is not None
    assert received[0].adapter.name == "brand-adapter"


# ---------------------------------------------------------------------------
# Negative case: the TypeVar has a bound
# ---------------------------------------------------------------------------


def test_typevar_is_bound_to_toolcontext():
    """The TypeVar bound prevents parameterising with an unrelated
    class.  At runtime Python doesn't enforce the bound (only mypy
    does), so this test asserts the bound resolves to ``ToolContext`` —
    not just that *some* bound exists. Previously this accepted the
    unresolved forward-reference string as proof enough, which meant a
    typo in the bound (e.g. ``ToolContect``) would have silently
    passed."""
    import typing

    from adcp.server import base as _base
    from adcp.server.base import TContext as _TContext

    bound = _TContext.__bound__
    if bound is None:
        pytest.fail("TContext has no bound")

    # Force forward-ref resolution against the module namespace TContext
    # lives in. typing.get_type_hints is the blessed API for this; it
    # walks the annotation through typing._eval_type and returns the
    # actual class the string resolves to.
    if hasattr(bound, "__forward_arg__"):
        resolved = typing.get_type_hints(
            type(
                "_Probe",
                (),
                {"__annotations__": {"x": bound}, "__module__": _base.__name__},
            ),
            globalns=vars(_base),
        )["x"]
    else:
        resolved = bound

    assert (
        resolved is ToolContext
    ), f"TContext bound did not resolve to ToolContext; got {resolved!r}"


# ---------------------------------------------------------------------------
# ADCPAgentExecutor integration — the subclass still flows through
# ---------------------------------------------------------------------------


async def test_typed_handler_works_under_a2a_executor():
    """A handler parameterised with a custom ToolContext subclass must
    still dispatch correctly under the A2A executor.  Runtime doesn't
    touch the TypeVar directly (the executor passes whatever context
    the context_factory returned), but this pins the no-regression
    promise: adding the TypeVar didn't break the A2A dispatch path."""
    from a2a import types as pb
    from a2a.server.agent_execution.context import RequestContext
    from a2a.server.events.event_queue import EventQueueLegacy as EventQueue

    from tests.a2a_compat_shim import DataPart, Message, Part, Role, Task

    def MessageSendParams(*, message):  # noqa: N802 (0.3 fixture shim)
        return pb.SendMessageRequest(message=message)

    from adcp.server.a2a_server import ADCPAgentExecutor

    class _TypedAgent(ADCPHandler[_TypedContext]):
        _agent_type = "typed-executor-test"

        async def get_adcp_capabilities(self, params, context=None):
            return {"adcp": {"major_versions": [3]}, "supported_protocols": ["media_buy"]}

    # validation=None opts out of strict-by-default wire-conformance —
    # this test asserts dispatch plumbing under a TypeVar'd handler,
    # not the schema shape of the stub's response.
    executor = ADCPAgentExecutor(_TypedAgent(), validation=None)
    msg = Message(
        message_id="m-1",
        role=Role.user,
        parts=[Part(root=DataPart(data={"skill": "get_adcp_capabilities", "parameters": {}}))],
    )
    from a2a.auth.user import UnauthenticatedUser
    from a2a.server.context import ServerCallContext

    ctx = RequestContext(
        call_context=ServerCallContext(user=UnauthenticatedUser()),
        request=MessageSendParams(message=msg),
    )
    queue = EventQueue()
    await executor.execute(ctx, queue)

    event = await queue.dequeue_event()
    assert isinstance(event, Task)
    assert event.status.state == pb.TaskState.TASK_STATE_COMPLETED


# ---------------------------------------------------------------------------
# Handler method signature annotations survive the TypeVar
# ---------------------------------------------------------------------------


def test_handler_method_signatures_preserve_parameter_order():
    """Sanity check on the mechanical rewrite of 57 method sigs — the
    ``context: ToolContext | None`` → ``context: TContext | None``
    change is a single-word swap in the annotation and must not have
    shifted parameter positions or renamed ``params``. Failure here
    typically means a stray sed corrupted a signature.
    """
    import inspect

    for method_name in ("get_adcp_capabilities", "get_products", "create_media_buy"):
        method = getattr(ADCPHandler, method_name, None)
        assert method is not None, f"{method_name} missing from ADCPHandler"
        sig = inspect.signature(method)
        params = list(sig.parameters.keys())
        # self, params, context — in that order, minimum.
        assert params[0] == "self"
        assert params[1] == "params"
        assert "context" in params


# ---------------------------------------------------------------------------
# AccountAwareToolContext — shipped subclass exercised through the TypeVar
# ---------------------------------------------------------------------------


async def test_account_aware_context_flows_through_a2a_executor():
    """End-to-end: the shipped ``AccountAwareToolContext`` must flow
    through ``ADCPAgentExecutor`` dispatch preserving its subclass
    identity and populated fields. This is the path salesagent exercises
    and the canonical example we point sellers at — a dispatch test is
    the only test that catches regressions in the transport's context
    plumbing against the shipped subclass."""
    from a2a import types as pb
    from a2a.server.agent_execution.context import RequestContext
    from a2a.server.events.event_queue import EventQueueLegacy as EventQueue

    from tests.a2a_compat_shim import DataPart, Message, Part, Role, Task

    def MessageSendParams(*, message):  # noqa: N802 (0.3 fixture shim)
        return pb.SendMessageRequest(message=message)

    from adcp.server import AccountAwareToolContext
    from adcp.server.a2a_server import ADCPAgentExecutor

    received: list[Any] = []

    class _AccountAwareAgent(ADCPHandler[AccountAwareToolContext]):
        _agent_type = "account-aware"

        async def get_adcp_capabilities(self, params, context=None):
            received.append(context)
            return {"adcp": {"major_versions": [3]}, "supported_protocols": ["media_buy"]}

    def _factory(meta):
        return AccountAwareToolContext(
            caller_identity="p-1",
            tenant_id="t-1",
            account_id="acct-42",
        )

    # See note on _TypedAgent above re: validation=None opt-out.
    executor = ADCPAgentExecutor(_AccountAwareAgent(), context_factory=_factory, validation=None)
    msg = Message(
        message_id="m-1",
        role=Role.user,
        parts=[Part(root=DataPart(data={"skill": "get_adcp_capabilities", "parameters": {}}))],
    )
    from a2a.auth.user import UnauthenticatedUser
    from a2a.server.context import ServerCallContext

    ctx = RequestContext(
        call_context=ServerCallContext(user=UnauthenticatedUser()),
        request=MessageSendParams(message=msg),
    )
    queue = EventQueue()
    await executor.execute(ctx, queue)

    event = await queue.dequeue_event()
    assert isinstance(event, Task)
    assert event.status.state == pb.TaskState.TASK_STATE_COMPLETED

    assert len(received) == 1
    got = received[0]
    assert isinstance(got, AccountAwareToolContext)
    assert got.account_id == "acct-42"
    assert got.tenant_id == "t-1"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
