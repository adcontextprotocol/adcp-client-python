"""Tests for the public ``register_handler_tools`` seam.

Covers:

* :func:`adcp.server.mcp_tools.register_handler_tools` idempotency,
  conflict detection, unknown-tool typo recovery, and integration with
  :func:`adcp.server.mcp_tools.get_tools_for_handler`.
* :meth:`adcp.server.base.ADCPHandler.__init_subclass__` auto-registration
  from the ``advertised_tools`` class attribute.
* The boot-time ``UserWarning`` in
  :func:`adcp.server.serve._warn_if_unregistered_subclass`.

Tests intentionally use direct imports of the (private)
``_HANDLER_TOOLS`` registry for cleanup. Production code reaches it
only through ``register_handler_tools``.
"""

from __future__ import annotations

import warnings
from typing import Any, ClassVar

import pytest

from adcp.server import ADCPHandler
from adcp.server.mcp_tools import (
    _HANDLER_TOOLS,
    get_tools_for_handler,
    register_handler_tools,
)
from adcp.server.serve import _warn_if_unregistered_subclass


@pytest.fixture
def cleanup_handler_registry():
    """Snapshot ``_HANDLER_TOOLS`` and restore on teardown so tests don't
    leak synthetic registrations into each other.
    """
    snapshot = {k: set(v) for k, v in _HANDLER_TOOLS.items()}
    yield
    _HANDLER_TOOLS.clear()
    _HANDLER_TOOLS.update(snapshot)


# ---- register_handler_tools — idempotency + conflict detection ----


def test_register_handler_tools_idempotent_on_equal_input(cleanup_handler_registry):
    """Calling twice with the same set is a no-op — module re-imports
    and reloadable test harnesses don't break."""
    register_handler_tools("_TestIdempotent", {"get_products", "create_media_buy"})
    register_handler_tools("_TestIdempotent", {"create_media_buy", "get_products"})
    assert _HANDLER_TOOLS["_TestIdempotent"] == {"get_products", "create_media_buy"}


def test_register_handler_tools_raises_on_conflicting_set(cleanup_handler_registry):
    """Calling twice with different sets raises so accidental drift is
    caught at registration, not at first dispatch."""
    register_handler_tools("_TestConflict", {"get_products"})
    with pytest.raises(ValueError, match="called twice"):
        register_handler_tools("_TestConflict", {"create_media_buy"})


def test_register_handler_tools_rejects_unknown_tool(cleanup_handler_registry):
    """Unknown tool names raise — adopters can't smuggle non-spec
    surface through the registry."""
    with pytest.raises(ValueError, match="unknown tool"):
        register_handler_tools("_TestUnknown", {"definitely_not_a_real_tool"})


def test_register_handler_tools_suggests_close_match(cleanup_handler_registry):
    """Typo recovery — ``difflib.get_close_matches`` surfaces the most
    likely intended name on the error message so adopters working from
    spec memory can fix the typo without a manual scan."""
    with pytest.raises(ValueError, match="did you mean 'create_media_buy'"):
        register_handler_tools("_TestTypo", {"create_media_buyy"})


def test_register_handler_tools_integrates_with_get_tools_for_handler(
    cleanup_handler_registry,
):
    """After registration, ``get_tools_for_handler`` returns the
    registered set (filtered by override-detection — direct
    ``ADCPHandler`` subclass with override of one tool advertises that
    one tool plus protocol/discovery)."""

    class _CustomBase(ADCPHandler):
        async def get_products(self, params, context=None):
            return {"products": []}

    register_handler_tools("_CustomBase", {"get_products", "create_media_buy"})
    tools = {t["name"] for t in get_tools_for_handler(_CustomBase())}
    assert "get_products" in tools
    # create_media_buy is in the registered set but the subclass didn't
    # override it, so override-detection filters it out (advertise_all=False).
    assert "create_media_buy" not in tools


def test_register_handler_tools_advertise_all_returns_full_registered_set(
    cleanup_handler_registry,
):
    """``advertise_all=True`` skips the override filter and returns
    every registered tool — exactly the explicit "yes, advertise
    everything" opt-in."""

    class _AdvertiseAllBase(ADCPHandler):
        pass

    register_handler_tools("_AdvertiseAllBase", {"get_products", "create_media_buy"})
    tools = {t["name"] for t in get_tools_for_handler(_AdvertiseAllBase(), advertise_all=True)}
    assert "get_products" in tools
    assert "create_media_buy" in tools


# ---- ADCPHandler.__init_subclass__ — auto-registration via class attr ----


def test_init_subclass_auto_registers_from_advertised_tools(cleanup_handler_registry):
    """A subclass declaring ``advertised_tools`` as a class attribute
    is auto-registered at class creation time — codegen targets emit
    this and adopters never need to call ``register_handler_tools``
    explicitly."""

    class _AutoRegisteredHandler(ADCPHandler):
        advertised_tools: ClassVar[set[str]] = {"get_products", "create_media_buy"}

    assert "_AutoRegisteredHandler" in _HANDLER_TOOLS
    assert _HANDLER_TOOLS["_AutoRegisteredHandler"] == {
        "get_products",
        "create_media_buy",
    }


def test_init_subclass_skips_inherited_advertised_tools(cleanup_handler_registry):
    """Inherited ``advertised_tools`` does NOT trigger re-registration —
    multi-level subclasses don't shadow their parent's registered set
    with the same set under a new class name."""

    class _ParentBase(ADCPHandler):
        advertised_tools: ClassVar[set[str]] = {"get_products"}

    class _ChildBase(_ParentBase):
        # No advertised_tools of its own. Inherits via MRO but
        # __init_subclass__ checks cls.__dict__ specifically so this
        # subclass isn't registered.
        pass

    assert "_ParentBase" in _HANDLER_TOOLS
    assert "_ChildBase" not in _HANDLER_TOOLS


def test_init_subclass_three_level_chain_with_intermediate_declaration(
    cleanup_handler_registry,
):
    """Three-level inheritance where the *intermediate* base declares
    ``advertised_tools`` registers AT the intermediate level. The leaf
    inherits via MRO and isn't separately registered. Regression for
    middle-of-the-chain registration."""

    class _Root(ADCPHandler):
        # No advertised_tools — root level.
        pass

    class _Intermediate(_Root):
        # Declares its own — registers at this level.
        advertised_tools: ClassVar[set[str]] = {"get_products", "create_media_buy"}

    class _Leaf(_Intermediate):
        # Inherits from intermediate; no own declaration.
        pass

    assert "_Root" not in _HANDLER_TOOLS
    assert "_Intermediate" in _HANDLER_TOOLS
    assert _HANDLER_TOOLS["_Intermediate"] == {"get_products", "create_media_buy"}
    assert "_Leaf" not in _HANDLER_TOOLS


def test_init_subclass_forwards_kwargs_to_super(cleanup_handler_registry):
    """``__init_subclass__`` calls ``super().__init_subclass__(**kwargs)``
    so PEP 487-style metaclass kwargs (e.g. from
    ``__init_subclass__`` declared on a custom metaclass) flow through.
    Without this, a future subclass using ``class X(ADCPHandler, mixin_kw=...)``
    would lose the kwarg silently. Regression coverage by declaring a
    bystander base that inspects ``cls`` on subclass creation."""
    seen: list[type] = []

    class _Bystander:
        def __init_subclass__(cls, **kwargs: Any) -> None:
            super().__init_subclass__(**kwargs)
            seen.append(cls)

    class _MultiInheritHandler(ADCPHandler, _Bystander):
        advertised_tools: ClassVar[set[str]] = {"get_products"}

    # Bystander's __init_subclass__ saw the new class — proves the
    # super() chain is intact even with ADCPHandler.__init_subclass__
    # in the chain. Without `super().__init_subclass__(**kwargs)`, the
    # bystander would never fire.
    assert _MultiInheritHandler in seen


def test_register_handler_tools_accepts_empty_iterable(cleanup_handler_registry):
    """An empty tool set is allowed — represents "this handler claims
    zero tool surface." Registering it makes the handler-name known to
    the registry without claiming any AdCP verbs. Useful for handlers
    that exist purely for typed test-context plumbing."""
    register_handler_tools("_EmptySetHandler", set())
    assert _HANDLER_TOOLS["_EmptySetHandler"] == set()


def test_init_subclass_idempotent_on_module_reload(cleanup_handler_registry):
    """Re-evaluating the same class body (test reload, ipython rerun)
    re-triggers ``__init_subclass__`` with the same tool set — must not
    raise. Idempotency on equal input."""

    def _define():
        class _ReloadHandler(ADCPHandler):
            advertised_tools: ClassVar[set[str]] = {"get_products"}

        return _ReloadHandler

    _define()
    _define()  # would raise on conflict if registry were strict
    assert "_ReloadHandler" in _HANDLER_TOOLS


# ---- serve() boot UserWarning ----


def test_warn_if_unregistered_subclass_fires_on_unregistered_direct_subclass(
    cleanup_handler_registry,
):
    """An ``ADCPHandler`` subclass with no registry entry, no
    ``advertised_tools`` declaration, and no specialized parent
    triggers the boot-time UserWarning."""

    class _UnregisteredHandler(ADCPHandler):
        pass

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always", UserWarning)
        _warn_if_unregistered_subclass(_UnregisteredHandler(), advertise_all=False)
    matched = [w for w in caught if "_UnregisteredHandler" in str(w.message)]
    assert len(matched) == 1
    assert "advertised_tools" in str(matched[0].message)
    assert "advertise_all=True" in str(matched[0].message)


def test_warn_if_unregistered_subclass_suppressed_by_advertise_all(
    cleanup_handler_registry,
):
    """``advertise_all=True`` silences the warning — explicit opt-in to
    full advertisement."""

    class _IntentionallyAllHandler(ADCPHandler):
        pass

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always", UserWarning)
        _warn_if_unregistered_subclass(_IntentionallyAllHandler(), advertise_all=True)
    matched = [w for w in caught if "_IntentionallyAllHandler" in str(w.message)]
    assert matched == []


def test_warn_if_unregistered_subclass_suppressed_when_advertised_tools_set(
    cleanup_handler_registry,
):
    """A subclass declaring ``advertised_tools`` is auto-registered via
    ``__init_subclass__``, so the warning never fires for it."""

    class _DeclaresAdvertisedHandler(ADCPHandler):
        advertised_tools: ClassVar[set[str]] = {"get_products"}

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always", UserWarning)
        _warn_if_unregistered_subclass(_DeclaresAdvertisedHandler(), advertise_all=False)
    matched = [w for w in caught if "_DeclaresAdvertisedHandler" in str(w.message)]
    assert matched == []


def test_warn_if_unregistered_subclass_suppressed_for_specialized_parent(
    cleanup_handler_registry,
):
    """Subclassing a specialized base (e.g. ``GovernanceHandler``) is
    the documented pattern; the subclass inherits the parent's tool
    set via MRO and no warning should fire even when the subclass
    itself isn't separately registered."""
    from adcp.server import GovernanceHandler

    # Concrete subclass providing the handle_* methods GovernanceHandler
    # declares abstract. Implementations are minimal — the test only
    # cares that the class instantiates so we can call the warning
    # check on an instance.
    class _CustomGovernanceAgent(GovernanceHandler):
        async def handle_check_governance(self, params, context=None):
            return {}

        async def handle_report_plan_outcome(self, params, context=None):
            return {}

        async def handle_get_plan_audit_logs(self, params, context=None):
            return {}

        async def handle_sync_plans(self, params, context=None):
            return {}

        async def handle_get_creative_features(self, params, context=None):
            return {}

        async def handle_create_property_list(self, params, context=None):
            return {}

        async def handle_get_property_list(self, params, context=None):
            return {}

        async def handle_list_property_lists(self, params, context=None):
            return {}

        async def handle_update_property_list(self, params, context=None):
            return {}

        async def handle_delete_property_list(self, params, context=None):
            return {}

        async def handle_create_collection_list(self, params, context=None):
            return {}

        async def handle_get_collection_list(self, params, context=None):
            return {}

        async def handle_list_collection_lists(self, params, context=None):
            return {}

        async def handle_update_collection_list(self, params, context=None):
            return {}

        async def handle_delete_collection_list(self, params, context=None):
            return {}

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always", UserWarning)
        _warn_if_unregistered_subclass(_CustomGovernanceAgent(), advertise_all=False)
    matched = [w for w in caught if "_CustomGovernanceAgent" in str(w.message)]
    assert matched == []
