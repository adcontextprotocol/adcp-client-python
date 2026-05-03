"""``create_media_buy_store`` — opt-in framework wiring that gates
``targeting_overlay`` echo on the seller's declared specialisms.

The seller's spec contract on
``schemas/cache/<v>/media-buy/get-media-buys-response.json`` requires
that sellers claiming the ``property-lists`` or ``collection-lists``
specialism echo persisted ``property_list`` / ``collection_list``
references inside the ``packages[].targeting_overlay`` they return on
``get_media_buys``. This factory lets adopters wire that contract once,
at the framework boundary, instead of every adapter persisting +
echoing by hand.

Mirrors the JS-side ``createMediaBuyStore`` from ``@adcp/sdk@6.7``
(commit ``dda2a77e``) with one shape change: the JS factory builds the
persistence itself on top of an ``AdcpStateStore``. The Python SDK
doesn't ship an ``AdcpStateStore`` Protocol, so adopters supply their
own :class:`MediaBuyStore` implementation; this factory wraps it with
specialism-aware gating. Adopters who want a reference impl can crib
the in-memory pattern from the test suite.

Usage::

    from adcp.decisioning import create_media_buy_store

    platform.media_buy_store = create_media_buy_store(
        adopter_store, capabilities=platform.capabilities,
    )

Behavior:

* Seller claims ``property-lists`` or ``collection-lists`` →
  every method delegates to the adopter store.
* Seller claims neither → ``persist_from_create`` and
  ``merge_from_update`` are no-ops; ``backfill`` returns the response
  unchanged. The adopter store is never invoked.

The wrapper is always a fresh object so adopters can reason about
identity at the assignment site without aliasing surprises.
"""

from __future__ import annotations

import inspect
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

if TYPE_CHECKING:
    from adcp.decisioning.platform import DecisioningCapabilities

__all__ = [
    "MediaBuyStore",
    "create_media_buy_store",
]

#: Specialism slugs that require ``targeting_overlay`` echo on
#: ``get_media_buys``. A seller claiming either MUST echo the persisted
#: ``property_list`` / ``collection_list`` reference per
#: ``schemas/cache/<v>/media-buy/get-media-buys-response.json``.
_OVERLAY_ECHO_SPECIALISMS: frozenset[str] = frozenset({"property-lists", "collection-lists"})


@runtime_checkable
class MediaBuyStore(Protocol):
    """Adopter-supplied persistence + echo for ``targeting_overlay``.

    Three methods cover the full lifecycle of a per-package overlay:

    * :meth:`persist_from_create` records overlays from a successful
      ``create_media_buy``, joining the request's per-package overlay
      with the response's seller-assigned ``package_id`` (or
      ``buyer_ref`` when present).
    * :meth:`merge_from_update` applies ``update_media_buy`` patches
      with deep-merge semantics: keys absent from the patch keep prior
      values, keys present with non-null values replace, keys present
      and ``None`` clear.
    * :meth:`backfill` fills in missing
      ``packages[].targeting_overlay`` on a ``get_media_buys`` response
      from the persisted store. Mutates the response in place; packages
      the seller already echoed are left untouched.

    Methods may be sync or async — the wrapper awaits at call time.
    """

    def persist_from_create(
        self,
        account_id: str,
        request: Any,
        result: Any,
    ) -> Any: ...

    def merge_from_update(
        self,
        account_id: str,
        media_buy_id: str,
        patch: Any,
    ) -> Any: ...

    def backfill(self, account_id: str, result: Any) -> Any: ...


async def _await_maybe(value: Any) -> Any:
    """Resolve a value that may be a coroutine OR a plain return.

    Mirrors the helper in :mod:`adcp.decisioning.tenant_store`; adopter
    callbacks are sync OR async and the wrapper keeps its own dispatch
    uniform without forcing every adopter into ``async def``.
    """
    if inspect.isawaitable(value):
        return await value
    return value


class _ActiveMediaBuyStore:
    """Wrapper that delegates every method to the adopter store.

    Constructed when the seller claims ``property-lists`` or
    ``collection-lists``. Adds the sync/async normalization layer over
    the adopter callbacks so the framework can ``await`` uniformly.
    """

    __slots__ = ("_inner",)

    def __init__(self, inner: MediaBuyStore) -> None:
        self._inner = inner

    async def persist_from_create(self, account_id: str, request: Any, result: Any) -> None:
        await _await_maybe(self._inner.persist_from_create(account_id, request, result))

    async def merge_from_update(self, account_id: str, media_buy_id: str, patch: Any) -> None:
        await _await_maybe(self._inner.merge_from_update(account_id, media_buy_id, patch))

    async def backfill(self, account_id: str, result: Any) -> Any:
        return await _await_maybe(self._inner.backfill(account_id, result))


class _NoopMediaBuyStore:
    """Pass-through wrapper for sellers not claiming the echo specialisms.

    Holds a reference to the adopter store for parity with the active
    path — adopters can swap their seller's ``capabilities`` between
    builds without touching the wiring. The reference is kept private
    (``_inner``) to discourage adopter code from reaching past the
    wrapper to an unwrapped store.
    """

    __slots__ = ("_inner",)

    def __init__(self, inner: MediaBuyStore) -> None:
        self._inner = inner

    async def persist_from_create(self, account_id: str, request: Any, result: Any) -> None:
        del account_id, request, result  # no-op pass-through

    async def merge_from_update(self, account_id: str, media_buy_id: str, patch: Any) -> None:
        del account_id, media_buy_id, patch  # no-op pass-through

    async def backfill(self, account_id: str, result: Any) -> Any:
        del account_id
        return result


def create_media_buy_store(
    adopter_store: MediaBuyStore,
    *,
    capabilities: DecisioningCapabilities,
) -> MediaBuyStore:
    """Wrap an adopter :class:`MediaBuyStore` with specialism-aware
    ``targeting_overlay`` echo gating.

    :param adopter_store: The persistence + echo implementation. Must
        satisfy the :class:`MediaBuyStore` Protocol — three methods
        (``persist_from_create``, ``merge_from_update``, ``backfill``)
        sync or async.
    :param capabilities: The seller's :class:`DecisioningCapabilities`.
        Read once at construction to decide whether the wrapper
        delegates or no-ops; not re-read per request, so adopters who
        mutate ``capabilities.specialisms`` after building the store
        won't see the change reflected. Build-time decision matches the
        boot-time validation pattern used elsewhere
        (``validate_platform``).

    :returns: A :class:`MediaBuyStore` wrapper. When ``capabilities.specialisms``
        intersects ``{property-lists, collection-lists}``, every method
        delegates to ``adopter_store``. Otherwise every method is a
        no-op pass-through and the adopter store is never invoked.

    The returned object is always distinct from ``adopter_store`` — even
    on the no-op path — so adopters can reason about identity at the
    assignment site (``platform.media_buy_store = ...``) without
    aliasing the underlying persistence layer.
    """
    claimed = set(capabilities.specialisms) & _OVERLAY_ECHO_SPECIALISMS
    if claimed:
        return _ActiveMediaBuyStore(adopter_store)
    return _NoopMediaBuyStore(adopter_store)
