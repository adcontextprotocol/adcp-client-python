"""Sync workflow-state reader for :class:`RequestContext`.

Defines:

* :class:`StateReader` — Protocol for sync reads of framework-owned
  in-flight workflow state. Platform methods read this without
  re-querying their own DB; the framework owns the cache.
* :class:`WorkflowStep`, :class:`WorkflowObjectType`,
  :data:`GovernanceContextJWS` — framework-internal types referenced
  by :class:`StateReader` methods. Defined here (not in
  ``adcp.types.generated_poc/``) because they're framework-only —
  not on the wire.
* :class:`_NotYetWiredStateReader` — v6.0 stub. Returns type-correct
  empty values; emits a one-time :class:`UserWarning` per method on
  first call so adopters notice they're reading uninitialized state.
  Backing store lands in v6.1.

The asymmetry between this stub (returns empty) and
:class:`adcp.decisioning.resolve._NotYetWiredResolver` (raises) is
deliberate. ``state.*`` reads are read-only inspections of
framework-owned state — an empty workflow-steps list IS the correct
answer for a fresh tenant. ``resolve.*`` fetches are validated
lookups — an empty :class:`PropertyList` in v6.0 vs. a real one in
v6.1 is divergence the framework cannot silently paper over. See
``docs/proposals/decisioning-platform-dispatch-design.md#d15`` for
the full rationale.
"""

from __future__ import annotations

import warnings
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal, NewType, Protocol, runtime_checkable

# Wire types referenced through the StateReader. ``Proposal`` is
# exported from adcp.types; importing from there keeps the layering
# rule in CLAUDE.md happy (only adcp.types/{stable,aliases,_ergonomic}
# may import from generated_poc/).
from adcp.types import Proposal

#: Object types a workflow step can touch. Framework-internal — not on
#: the wire (the wire-side ``status-change-resource-type.json`` enum
#: covers a different surface).
WorkflowObjectType = Literal[
    "media_buy",
    "creative",
    "product",
    "plan",
    "audience",
    "rights_grant",
    "task",
]

#: JWS-signed governance context. The framework verifies signature,
#: plan-binding, seller-binding, and phase-binding before exposing the
#: token to platform code; adopters can trust the value. Don't unwrap
#: or modify — re-pass to downstream framework calls instead.
GovernanceContextJWS = NewType("GovernanceContextJWS", str)


@dataclass(frozen=True)
class WorkflowStep:
    """A chronological event the framework recorded against an object.

    Frozen because the framework writes the step record once at the
    transition; platform code reads but does not mutate. The shape
    mirrors the TS-side ``WorkflowStep`` interface so cross-language
    adopters get the same fields.

    :param id: Stable step identifier (framework-allocated UUID).
    :param object_type: The object this step touched.
    :param object_id: Stable id of the touched object within
        :attr:`object_type`.
    :param tool: Wire verb that ran the step
        (e.g. ``'create_media_buy'``, ``'sync_creatives'``).
    :param at: ISO 8601 timestamp of the step.
    :param actor: Who initiated the step. ``agent_url`` for an agent
        principal, ``principal`` for a service-account principal,
        possibly both.
    :param status: Step outcome. ``'submitted'`` for a kicked-off task,
        ``'completed'``/``'failed'`` for terminal states,
        ``'progress'`` for a mid-flight update.
    """

    id: str
    object_type: WorkflowObjectType
    object_id: str
    tool: str
    at: str
    actor: dict[str, str]
    status: Literal["submitted", "completed", "failed", "progress"]


@runtime_checkable
class StateReader(Protocol):
    """Sync reads of framework-owned in-flight workflow state.

    Platform methods read prior workflow context (recent media-buy
    transitions, related proposals, in-flight governance bindings)
    without re-querying their own DB. The framework owns the cache; the
    Protocol surface is purely read.

    Framework-supplied; never constructed by adopter code. The
    ``RequestContext.state`` field is populated by the dispatch
    hydration helper. Adopters substituting test doubles use
    :func:`dataclasses.replace` on the context, not direct
    construction.

    Mirrors the TS-side ``WorkflowStateReader`` interface in
    ``src/lib/server/decisioning/context.ts``. v6.0 ships the contract
    + the no-op stub; v6.1 lands the backing store.

    .. note::
       :class:`runtime_checkable` Protocols match by attribute *name*
       only — return types (including :data:`GovernanceContextJWS`,
       which is a :func:`typing.NewType` invisible at runtime) and
       method signatures are NOT enforced by ``isinstance``. A custom
       impl that returns ``int`` from ``governance_context()`` will
       pass the structural check; mypy is the only enforcement for
       return-type contracts. Coverage gap is acceptable for v6.0.
    """

    def find_by_object(
        self,
        object_type: WorkflowObjectType,
        object_id: str,
    ) -> Sequence[WorkflowStep]:
        """Return workflow steps that touched the given object,
        chronological. Used for "what's happened to this buy?" reads
        without a platform-side fetch."""
        ...

    def find_proposal_by_id(self, proposal_id: str) -> Proposal | None:
        """Resolve a ``proposal_id`` threaded across
        ``get_products → refine → create_media_buy`` without platform
        code. Returns ``None`` if the framework doesn't recognize the
        id."""
        ...

    def governance_context(self) -> GovernanceContextJWS | None:
        """Currently in-flight verified governance context (the JWS
        token). ``None`` for non-governance flows. Framework verifies
        before exposure; platform code can trust the value.

        Adopters claiming ``governance-*`` specialisms in
        ``capabilities.specialisms`` MUST set
        ``capabilities.governance_aware=True`` and wire a real
        ``StateReader`` that returns real JWS tokens. The default stub
        returns ``None``, which would silently skip the gate — server
        boot fails fast if a governance specialism is claimed without
        the opt-in. See
        ``docs/proposals/decisioning-platform-dispatch-design.md#d15``.
        """
        ...

    def workflow_steps(self) -> Sequence[WorkflowStep]:
        """All chronological steps for this request's account.
        Audit-read shape."""
        ...


# ---------------------------------------------------------------------------
# v6.0 stub — empty returns + one-time UserWarning per method
# ---------------------------------------------------------------------------

#: Module-level set tracking which stub methods have already warned.
#: Module-scoped so concurrent ``serve()`` instances share the
#: warned-once state — emitting the warning per process per method,
#: not per request.
_STATE_STUB_WARNED: set[str] = set()


class _NotYetWiredStateReader:
    """v6.0 stub. Returns type-correct empty values for every method;
    emits a one-time :class:`UserWarning` per method on first call.

    Adopters who reach for ``ctx.state.*`` against the stub get the
    legitimate "no history yet" semantics for fresh tenants AND a
    visible warning the first time so accidentally-uninitialized state
    doesn't ship silently. Adopters claiming ``governance-*``
    specialisms get the fail-fast path at server boot before this stub
    is ever invoked (see :class:`StateReader.governance_context`
    docstring).

    Framework-internal — not exported. Adopters write custom
    ``StateReader`` impls when they need the v6.1-style behavior
    before the backing store lands.
    """

    def _warn_once(self, method_name: str) -> None:
        if method_name in _STATE_STUB_WARNED:
            return
        _STATE_STUB_WARNED.add(method_name)
        # ``governance_context`` is a load-bearing security stub —
        # adopters claiming governance-* specialisms get the fail-fast
        # path at server boot before this branch is reached, so any
        # code path that lands here is a non-governance flow where
        # ``None`` is also the v6.1 answer (no governance threaded for
        # this request). Other state methods will return real values
        # in v6.1, so adopter branches on empty results would diverge.
        if method_name == "governance_context":
            tail = (
                "Returning None — non-governance flows get the same answer "
                "in v6.1; governance-claiming platforms hit the server-boot "
                "fail-fast before this stub is invoked."
            )
        else:
            tail = (
                "Reading empty results — adopter code branching on this "
                "state will see different values once the backing store is "
                "wired."
            )
        warnings.warn(
            f"ctx.state.{method_name}() called against the v6.0 stub "
            f"StateReader; backing store lands in v6.1. {tail} See "
            "docs/proposals/decisioning-platform-dispatch-design.md#d15",
            UserWarning,
            stacklevel=3,
        )

    def find_by_object(
        self,
        object_type: WorkflowObjectType,
        object_id: str,
    ) -> Sequence[WorkflowStep]:
        self._warn_once("find_by_object")
        return ()

    def find_proposal_by_id(self, proposal_id: str) -> Proposal | None:
        self._warn_once("find_proposal_by_id")
        return None

    def governance_context(self) -> GovernanceContextJWS | None:
        self._warn_once("governance_context")
        return None

    def workflow_steps(self) -> Sequence[WorkflowStep]:
        self._warn_once("workflow_steps")
        return ()


def _reset_state_stub_warned() -> None:
    """Test helper — clears the module-level warned-once set.

    Production code never calls this; tests use it to assert the
    one-time semantics deterministically (each test starts with a
    fresh warned set).
    """
    _STATE_STUB_WARNED.clear()


__all__ = [
    "GovernanceContextJWS",
    "Proposal",
    "StateReader",
    "WorkflowObjectType",
    "WorkflowStep",
]


# Re-exports needed by ``RequestContext`` field defaults but not part
# of the public adopter-facing surface — keep below ``__all__``.
def _make_default_state_reader() -> StateReader:
    return _NotYetWiredStateReader()
