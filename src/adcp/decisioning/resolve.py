"""Async framework-mediated resource resolver for :class:`RequestContext`.

Defines:

* :class:`ResourceResolver` — Protocol for async fetches of
  framework-validated resources (property lists, collection lists,
  creative formats). The framework owns the cache + validation;
  platform methods get pre-validated typed results.
* :class:`_NotYetWiredResolver` — v6.0 stub. Raises
  :class:`NotImplementedError` on every call with a pointer to the
  v6.1 follow-up. Asymmetry vs. the ``state`` stub (which returns
  empty + warns) is deliberate: an empty :class:`PropertyList` in v6.0
  vs. a real one in v6.1 is divergence the framework cannot silently
  paper over. See ``docs/proposals/decisioning-platform-dispatch-design.md#d15``.

The :class:`Format` and :class:`PropertyListReference` types are
re-exported from :mod:`adcp.types.generated_poc` so adopters import
once from :mod:`adcp.decisioning`. :class:`PropertyList` and
:class:`CollectionList` use the spec-defined wire shapes; the
resolver returns the same Pydantic models adopters would construct
themselves.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

# Wire types — already exported from adcp.types. Re-export for
# one-stop import from adcp.decisioning. Per CLAUDE.md import
# architecture rules, only adcp.types/{stable,aliases,_ergonomic} may
# import from generated_poc/; everywhere else uses the public
# adcp.types surface.
from adcp.types import (
    CollectionList,
    Format,
    FormatReferenceStructuredObject,
    PropertyListReference,
)

# ``PropertyList`` is the resolved-list shape (vs.
# ``PropertyListReference`` which is the wire-encoded reference). The
# spec currently models both as the same Pydantic class — the
# reference carries populated members on the response — so we alias
# for clarity in adopter call sites and on D15's StateReader contract.
# If a future spec rev introduces a distinct resolved-list type,
# adopter code typed against ``PropertyList`` would silently re-target;
# the contract test ``test_property_list_alias_pinned_to_reference`` in
# tests/test_decisioning_context_state_resolve.py tripwires that drift
# so the rename is visible at CI time, not deploy time.
PropertyList = PropertyListReference


@runtime_checkable
class ResourceResolver(Protocol):
    """Async fetches of framework-mediated resources.

    Platforms call ``ctx.resolve.property_list(list_id)`` instead of
    fetching from their own DB; the framework returns a validated
    typed result. The resolver routes through
    ``capabilities.creative_agents`` for creative-format reads, hits
    the framework's local ``CreativePlatform.list_formats`` for
    self-hosted formats, and reads the seller's declared property /
    collection lists with id-validation built in.

    Framework-supplied; never constructed by adopter code. The
    ``RequestContext.resolve`` field is populated by the dispatch
    hydration helper. Adopters substituting test doubles use
    :func:`dataclasses.replace` on the context, not direct
    construction.

    Mirrors the TS-side ``ResourceResolver`` interface in
    ``src/lib/server/decisioning/context.ts``. v6.0 ships the contract
    + the no-op stub (raises ``NotImplementedError`` on every call);
    v6.1 lands the backing fetchers.

    .. note::
       :class:`runtime_checkable` Protocols only check attribute
       *presence*. Whether a method is ``async def`` is irrelevant to
       the runtime ``isinstance`` check — a sync method named
       ``property_list`` would pass the structural check but fail at
       ``await`` time. Use mypy to enforce ``async def`` signatures
       across adopter impls.
    """

    async def property_list(self, list_id: str) -> PropertyList:
        """Fetch a property list by id. Framework validates the id
        exists in the seller's declared lists before returning;
        consumers can trust the result."""
        ...

    async def collection_list(self, list_id: str) -> CollectionList:
        """Fetch a collection list by id. Same id-validation
        guarantee as :meth:`property_list`."""
        ...

    async def creative_format(
        self,
        format_id: FormatReferenceStructuredObject,
        *,
        revalidate: bool = False,
    ) -> Format:
        """Fetch a creative format definition.

        Routes through ``capabilities.creative_agents`` declaration
        with a framework-managed cache; self-hosted formats hit the
        local ``CreativePlatform.list_formats``. Returns the resolved
        :class:`Format` with full asset slot definitions.

        :param revalidate: When ``True``, bypasses the framework cache
            and re-fetches from the upstream creative-agent. Adopters
            with freshness needs (e.g., creative submission validating
            against the latest format spec) pass ``revalidate=True``;
            most reads use the default (``False``) to amortize the
            agent round-trip.

        Cache TTL is implementation detail (defaults to 1h on the
        reference impl); adopters who need stricter freshness use
        ``revalidate=True`` rather than depending on the TTL value.
        """
        ...


class _NotYetWiredResolver:
    """v6.0 stub. Raises :class:`NotImplementedError` on every method
    with a pointer to the v6.1 follow-up.

    Adopters reaching for ``ctx.resolve.*`` against the stub get an
    immediate, locatable failure rather than a silent empty
    ``PropertyList`` that diverges from real v6.1 behavior. Adopters
    write custom ``ResourceResolver`` impls when they need real
    fetching before the framework's backing impl ships.

    Framework-internal — not exported.
    """

    async def property_list(self, list_id: str) -> PropertyList:
        raise NotImplementedError(
            f"ResourceResolver.property_list({list_id!r}) called against "
            "the v6.0 stub. Backing fetcher lands in v6.1 — see "
            "docs/proposals/decisioning-platform-dispatch-design.md#d15. "
            "Foundation-PR adopters should not invoke ctx.resolve.* yet, "
            "or wire a custom ResourceResolver via "
            "serve(resolver=...) for the v6.1-style behavior."
        )

    async def collection_list(self, list_id: str) -> CollectionList:
        raise NotImplementedError(
            f"ResourceResolver.collection_list({list_id!r}) called against "
            "the v6.0 stub. Backing fetcher lands in v6.1 — see "
            "docs/proposals/decisioning-platform-dispatch-design.md#d15."
        )

    async def creative_format(
        self,
        format_id: FormatReferenceStructuredObject,
        *,
        revalidate: bool = False,
    ) -> Format:
        raise NotImplementedError(
            f"ResourceResolver.creative_format({format_id!r}, revalidate="
            f"{revalidate}) called against the v6.0 stub. Backing "
            "fetcher lands in v6.1 — see "
            "docs/proposals/decisioning-platform-dispatch-design.md#d15."
        )


#: Module-level singleton — one stub instance per process. The stub
#: methods always raise (no warned-once state to share, but consistency
#: with state.py's pattern + avoiding per-RequestContext allocation).
_DEFAULT_RESOLVER: ResourceResolver = _NotYetWiredResolver()


def _make_default_resolver() -> ResourceResolver:
    """Return the module-level :class:`_NotYetWiredResolver` singleton."""
    return _DEFAULT_RESOLVER


__all__ = [
    "CollectionList",
    "Format",
    "FormatReferenceStructuredObject",
    "PropertyList",
    "PropertyListReference",
    "ResourceResolver",
]
