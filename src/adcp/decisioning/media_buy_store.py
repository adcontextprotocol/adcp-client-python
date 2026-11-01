"""``create_media_buy_store`` — framework wiring for what ``get_media_buys``
must echo from ``create_media_buy``.

AdCP 3.2 ``get_media_buys`` read surfaces echo two things a seller persisted
at create time:

* **``context``**, on every media buy and every package. The
  ``create_media_buy`` request's ``context`` comes back on
  ``media_buys[].context``, and each package request's ``context`` on
  ``media_buys[].packages[].context``, unchanged
  (``schemas/cache/<v>/media-buy/get-media-buys-response.json``,
  ``core/package.json``: "echoed unchanged in responses, webhooks, and read
  surfaces"). This is a MUST for every seller, so it is on by default: the
  wrapper persists both levels into a :class:`MediaBuyContextStore` and
  backfills them on ``get_media_buys`` wherever the platform left them out.
  The default store is in-memory; pass
  :class:`adcp.decisioning.pg.PgMediaBuyContextStore` (or any
  :class:`MediaBuyContextStore`) so the echo survives restarts and spans
  workers. ``echo_context=False`` opts out.
* **``targeting_overlay``**, for sellers claiming the ``property-lists`` or
  ``collection-lists`` specialism, which must echo persisted
  ``property_list`` / ``collection_list`` references inside
  ``packages[].targeting_overlay``. The adopter supplies that persistence as
  a :class:`MediaBuyStore`; the wrapper delegates to it only for sellers
  claiming one of the two specialisms and never invokes it otherwise.

Mirrors the JS-side ``createMediaBuyStore`` from ``@adcp/sdk@6.7``
(commit ``dda2a77e``) with one shape change: the JS factory builds the
overlay persistence itself on top of an ``AdcpStateStore``. The Python SDK
doesn't ship an ``AdcpStateStore`` Protocol, so adopters supply their own
:class:`MediaBuyStore` for overlays; the SDK owns the ``context`` store.

Usage::

    from adcp.decisioning import create_media_buy_store
    from adcp.decisioning.pg import PgMediaBuyContextStore

    platform.media_buy_store = create_media_buy_store(
        adopter_store,  # optional unless claiming property-/collection-lists
        capabilities=platform.capabilities,
        context_store=PgMediaBuyContextStore(pool=pool),
    )

The wrapper is always a fresh object so adopters can reason about
identity at the assignment site without aliasing surprises.

AdCP 3.3 also requires the buy's ``revision`` to advance on every committed
change, whatever its origin, and ``history`` entries for changes made outside
an AdCP task to carry ``origin.kind``. The seller's own persistence owns the
revision counter (``update_media_buy`` compares it atomically with the
write), so :func:`record_media_buy_change` builds the next revision and its
history entry for the adopter to store in that same write.
"""

from __future__ import annotations

import inspect
import logging
from collections.abc import Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Literal, Protocol, get_args, runtime_checkable

if TYPE_CHECKING:
    from adcp.decisioning.platform import DecisioningCapabilities

__all__ = [
    "InMemoryMediaBuyContextStore",
    "MediaBuyChange",
    "MediaBuyChangeOrigin",
    "MediaBuyContextStore",
    "MediaBuyContexts",
    "MediaBuyStore",
    "create_media_buy_store",
    "record_media_buy_change",
]

logger = logging.getLogger(__name__)

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


@dataclass(frozen=True)
class MediaBuyContexts:
    """The buyer ``context`` objects a ``create_media_buy`` request carried.

    :param context: The request's top-level ``context``, echoed on
        ``media_buys[].context``. ``None`` when the request carried none.
    :param package_contexts: Each package request's ``context``, keyed by
        the seller-assigned ``package_id``, echoed on
        ``media_buys[].packages[].context``.

    Both are opaque buyer data, stored and returned unchanged — including
    ``null`` values, key order and number spelling.
    """

    context: dict[str, Any] | None = None
    package_contexts: Mapping[str, dict[str, Any]] = field(default_factory=dict)


@runtime_checkable
class MediaBuyContextStore(Protocol):
    """Persistence for the ``context`` a ``get_media_buys`` read echoes.

    Writes are first-wins: a ``create_media_buy`` request's ``context`` is
    echoed unchanged for the life of the buy, so :meth:`save_contexts` never
    replaces a stored object. It records the buy-level ``context`` if none is
    stored yet and each package ``context`` whose ``package_id`` is new, which
    makes an idempotent create replay harmless.

    Every read and write is scoped by ``account_id``; a store MUST NOT return
    another account's record. Methods may be sync or async.

    Shipped implementations: :class:`InMemoryMediaBuyContextStore` (the
    default, process-local) and
    :class:`adcp.decisioning.pg.PgMediaBuyContextStore` (durable).
    """

    def save_contexts(
        self,
        account_id: str,
        media_buy_id: str,
        contexts: MediaBuyContexts,
    ) -> Any: ...

    def load_contexts(
        self,
        account_id: str,
        media_buy_ids: Sequence[str],
    ) -> Any:
        """Return ``{media_buy_id: MediaBuyContexts}`` for the stored ids."""
        ...


class InMemoryMediaBuyContextStore:
    """Process-local :class:`MediaBuyContextStore`.

    The default for :func:`create_media_buy_store`. Records live as long as
    the process and are not shared across workers, so a restarted or
    load-balanced seller loses the echo for buys created elsewhere. Use
    :class:`adcp.decisioning.pg.PgMediaBuyContextStore` in production.
    """

    def __init__(self) -> None:
        self._records: dict[tuple[str, str], tuple[Any, dict[str, Any]]] = {}

    def save_contexts(self, account_id: str, media_buy_id: str, contexts: MediaBuyContexts) -> None:
        key = (account_id, media_buy_id)
        context, packages = self._records.get(key, (None, {}))
        if context is None and contexts.context is not None:
            context = deepcopy(contexts.context)
        for package_id, package_context in contexts.package_contexts.items():
            packages.setdefault(package_id, deepcopy(package_context))
        self._records[key] = (context, packages)

    def load_contexts(
        self, account_id: str, media_buy_ids: Sequence[str]
    ) -> dict[str, MediaBuyContexts]:
        found: dict[str, MediaBuyContexts] = {}
        for media_buy_id in media_buy_ids:
            record = self._records.get((account_id, media_buy_id))
            if record is not None:
                found[media_buy_id] = MediaBuyContexts(
                    context=deepcopy(record[0]), package_contexts=deepcopy(record[1])
                )
        return found


def _dict_items(value: Any) -> list[dict[str, Any]]:
    """The dict entries of a JSON array, or ``[]`` for anything else."""
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def _matching_request_package(
    index: int,
    response_package: dict[str, Any],
    request_packages: list[dict[str, Any]],
    response_count: int,
) -> dict[str, Any] | None:
    """Pair a created package with the package request that produced it.

    A ``product_id`` the response echoes and only one request carries is
    decisive. Otherwise the n-th response package answers the n-th request
    when the counts agree and no echoed ``product_id`` contradicts it --
    the order the reference overlay store also relies on.
    """
    product_id = response_package.get("product_id")
    if product_id is not None:
        same_product = [r for r in request_packages if r.get("product_id") == product_id]
        if len(same_product) == 1:
            return same_product[0]
    if len(request_packages) == response_count:
        candidate = request_packages[index]
        if product_id is None or candidate.get("product_id") in (None, product_id):
            return candidate
    return None


def _contexts_from_create(request: Any, result: Any) -> tuple[str, MediaBuyContexts] | None:
    """The ``context`` objects to persist for one successful create."""
    if not isinstance(request, dict) or not isinstance(result, dict):
        return None
    media_buy_id = result.get("media_buy_id")
    if not isinstance(media_buy_id, str) or not media_buy_id:
        return None
    context = request.get("context")
    if not isinstance(context, dict):
        echoed = result.get("context")
        context = echoed if isinstance(echoed, dict) else None
    request_packages = _dict_items(request.get("packages"))
    response_packages = _dict_items(result.get("packages"))
    package_contexts: dict[str, dict[str, Any]] = {}
    for index, package in enumerate(response_packages):
        package_id = package.get("package_id")
        if not isinstance(package_id, str) or not package_id:
            continue
        matched = _matching_request_package(
            index, package, request_packages, len(response_packages)
        )
        source = matched.get("context") if matched is not None else None
        if not isinstance(source, dict):
            source = package.get("context")
        if isinstance(source, dict):
            package_contexts[package_id] = source
    if context is None and not package_contexts:
        return None
    return media_buy_id, MediaBuyContexts(context=context, package_contexts=package_contexts)


def _fill_contexts(result: Any, stored: Mapping[str, MediaBuyContexts]) -> None:
    """Fill each media buy's and package's missing ``context`` in place."""
    for buy in _dict_items(result.get("media_buys")):
        record = stored.get(buy.get("media_buy_id", ""))
        if record is None:
            continue
        if buy.get("context") is None and record.context is not None:
            buy["context"] = deepcopy(record.context)
        for package in _dict_items(buy.get("packages")):
            package_context = record.package_contexts.get(package.get("package_id", ""))
            if package.get("context") is None and package_context is not None:
                package["context"] = deepcopy(package_context)


class _MediaBuyStoreWrapper:
    """What :func:`create_media_buy_store` returns.

    ``targeting_overlay`` work goes to the adopter store when ``overlays`` is
    set (the seller claims an overlay-echo specialism); ``context`` work goes
    to ``contexts`` unless the adopter opted out. Adopter callbacks may be
    sync or async; the wrapper awaits uniformly.
    """

    __slots__ = ("_inner", "_overlays", "_contexts")

    def __init__(
        self,
        inner: MediaBuyStore | None,
        *,
        overlays: bool,
        contexts: MediaBuyContextStore | None,
    ) -> None:
        self._inner = inner
        self._overlays = overlays
        self._contexts = contexts

    async def persist_from_create(self, account_id: str, request: Any, result: Any) -> None:
        if self._overlays and self._inner is not None:
            await _await_maybe(self._inner.persist_from_create(account_id, request, result))
        if self._contexts is not None:
            found = _contexts_from_create(request, result)
            if found is not None:
                media_buy_id, contexts = found
                # The buy already exists upstream: failing the create now
                # would report an error for a buy the buyer then cannot see.
                try:
                    await _await_maybe(
                        self._contexts.save_contexts(account_id, media_buy_id, contexts)
                    )
                except Exception:
                    logger.exception(
                        "MediaBuyContextStore.save_contexts failed for media buy %s; "
                        "get_media_buys will not echo its context",
                        media_buy_id,
                    )

    async def merge_from_update(self, account_id: str, media_buy_id: str, patch: Any) -> None:
        # ``context`` is echoed from the create request; an update never
        # replaces it.
        if self._overlays and self._inner is not None:
            await _await_maybe(self._inner.merge_from_update(account_id, media_buy_id, patch))

    async def backfill(self, account_id: str, result: Any) -> Any:
        if self._overlays and self._inner is not None:
            result = await _await_maybe(self._inner.backfill(account_id, result))
        if self._contexts is not None and isinstance(result, dict):
            ids = list(
                dict.fromkeys(
                    buy["media_buy_id"]
                    for buy in _dict_items(result.get("media_buys"))
                    if isinstance(buy.get("media_buy_id"), str)
                )
            )
            if ids:
                try:
                    stored = await _await_maybe(self._contexts.load_contexts(account_id, ids))
                except Exception:
                    logger.exception(
                        "MediaBuyContextStore.load_contexts failed; "
                        "get_media_buys returned without the persisted context"
                    )
                else:
                    _fill_contexts(result, stored or {})
        return result


def create_media_buy_store(
    adopter_store: MediaBuyStore | None = None,
    *,
    capabilities: DecisioningCapabilities,
    context_store: MediaBuyContextStore | None = None,
    echo_context: bool = True,
) -> MediaBuyStore:
    """Build the :class:`MediaBuyStore` the framework calls around
    ``create_media_buy``, ``update_media_buy`` and ``get_media_buys``.

    :param adopter_store: The ``targeting_overlay`` persistence + echo
        implementation. Must satisfy the :class:`MediaBuyStore` Protocol —
        three methods (``persist_from_create``, ``merge_from_update``,
        ``backfill``) sync or async. Invoked only when
        ``capabilities.specialisms`` intersects
        ``{property-lists, collection-lists}``, and required then. Omit it
        when the seller claims neither: ``context`` echo needs no adopter
        code.
    :param capabilities: The seller's :class:`DecisioningCapabilities`.
        Read once at construction to decide whether overlay calls delegate
        or no-op; not re-read per request, so adopters who mutate
        ``capabilities.specialisms`` after building the store won't see the
        change reflected. Build-time decision matches the boot-time
        validation pattern used elsewhere (``validate_platform``).
    :param context_store: Where buy- and package-level ``context`` from
        ``create_media_buy`` is persisted for the ``get_media_buys`` echo.
        Defaults to a fresh :class:`InMemoryMediaBuyContextStore`; pass
        :class:`adcp.decisioning.pg.PgMediaBuyContextStore` for a durable,
        multi-worker echo.
    :param echo_context: ``False`` disables ``context`` persistence and
        echo, for a platform that persists and echoes ``context`` itself.
        A ``context`` the platform returns is never overwritten either way.

    :returns: A :class:`MediaBuyStore` wrapper, always distinct from
        ``adopter_store`` so adopters can reason about identity at the
        assignment site (``platform.media_buy_store = ...``) without
        aliasing the underlying persistence layer.

    :raises ValueError: if the seller claims ``property-lists`` or
        ``collection-lists`` but no ``adopter_store`` was supplied.
    """
    # ``capabilities.specialisms`` is ``list[Specialism | str]`` per #479
    # — spec-known slugs are coerced to ``Specialism`` enum members at
    # construction; novel/typo slugs pass through as strings. Extract the
    # slug uniformly before set intersection.
    declared_slugs = {
        entry.value if hasattr(entry, "value") else entry for entry in capabilities.specialisms
    }
    claimed = declared_slugs & _OVERLAY_ECHO_SPECIALISMS
    if claimed and adopter_store is None:
        raise ValueError(
            f"create_media_buy_store: the seller claims {sorted(claimed)}, which "
            "requires an adopter MediaBuyStore to persist and echo "
            "targeting_overlay on get_media_buys"
        )
    contexts: MediaBuyContextStore | None = None
    if echo_context:
        contexts = context_store if context_store is not None else InMemoryMediaBuyContextStore()
    return _MediaBuyStoreWrapper(adopter_store, overlays=bool(claimed), contexts=contexts)


#: ``history[].origin.kind`` on ``get_media_buys`` (AdCP 3.3, DR-0031): the
#: same vocabulary as ``account.change.origin``. ``adcp`` is a change that
#: arrived through an AdCP task (``update_media_buy``, a ``sync_creatives``
#: assignment); the other kinds are changes made outside one.
MediaBuyChangeOrigin = Literal[
    "adcp", "seller_operator", "seller_system", "connected_platform", "unknown"
]

_CHANGE_ORIGINS: frozenset[str] = frozenset(get_args(MediaBuyChangeOrigin))
_SUMMARY_MAX = 500
_CONNECTION_ID_MAX = 255


@dataclass(frozen=True)
class MediaBuyChange:
    """One committed change to a media buy, as :func:`record_media_buy_change` built it.

    :param revision: The buy's ``revision`` after the change. Store it in the
        same write that applies the change, and report it on
        ``get_media_buys`` and ``update_media_buy`` from then on.
    :param history_entry: The ``get_media_buys`` ``history[]`` entry for the
        change, in wire form. Entries are append-only.
    """

    revision: int
    history_entry: dict[str, Any]


def _rfc3339(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamp must be timezone-aware")
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def record_media_buy_change(
    current_revision: int,
    *,
    action: str,
    origin: MediaBuyChangeOrigin,
    timestamp: datetime | None = None,
    actor: str | None = None,
    summary: str | None = None,
    package_id: str | None = None,
    connection_id: str | None = None,
    ext: Mapping[str, Any] | None = None,
) -> MediaBuyChange:
    """Build the next ``revision`` and ``history`` entry for a committed change.

    AdCP 3.3 ``core/media-buy.json``: ``revision`` advances on every committed
    change to what the buy's authoritative read reports, whatever its origin:
    an AdCP task, a seller operator, seller automation, another authorized
    principal or a connected platform. ``get_media_buys`` ``history[]``
    entries for a change that did not arrive through an AdCP task MUST carry
    ``origin.kind``, and entries for AdCP task changes SHOULD carry
    ``kind: "adcp"``.

    Call this when the seller commits a change the AdCP task path did not
    record itself: an operator edit in the seller's UI (``seller_operator``),
    an optimizer or pacing job (``seller_system``), an order changed in a
    connected ad server and ingested by the seller (``connected_platform``),
    or a creative assignment made through ``sync_creatives`` (``adcp``).
    Store the returned revision and entry in the same transaction as the
    change, under the same compare-and-set the seller uses for
    ``update_media_buy``. Reads, validation-only calls and idempotent replays
    are not changes and must not call this.

    ``origin`` is server-derived: never copy one a caller supplied. Use
    ``unknown`` when the seller cannot classify the change or the caller is
    not authorized to see its source.

    :param current_revision: The buy's revision before the change (``0`` for
        the ``created`` entry of a new buy).
    :param action: What happened. Standard actions: ``created``,
        ``activated``, ``paused``, ``resumed``, ``canceled``, ``rejected``,
        ``completed``, ``updated_budget``, ``updated_dates``,
        ``updated_packages``, ``package_canceled``, ``package_paused``,
        ``package_resumed``. Seller-specific actions such as
        ``creative_assigned`` are allowed.
    :param origin: The server-derived ``origin.kind``.
    :param timestamp: When the change occurred; timezone-aware. Defaults to
        now (UTC).
    :param actor: Who made the change, at the granularity the reader is
        authorized to see. Omit direct personal identifiers for changes made
        outside an AdCP task unless the reader is authorized for them.
    :param summary: Human-readable summary, at most 500 characters.
    :param package_id: The package the change targeted, if any.
    :param connection_id: Opaque, non-secret reference to the connected
        source; only with ``origin="connected_platform"``, at most 255
        characters.
    :param ext: Extension object for structured metadata about the change.

    :raises ValueError: on an unknown ``origin``, a negative
        ``current_revision``, an empty ``action``, a naive ``timestamp``, an
        over-long ``summary`` or ``connection_id``, or a ``connection_id``
        with an origin other than ``connected_platform``.
    """
    if type(current_revision) is not int or current_revision < 0:
        raise ValueError("current_revision must be a non-negative integer")
    if origin not in _CHANGE_ORIGINS:
        raise ValueError(f"origin must be one of {sorted(_CHANGE_ORIGINS)}, got {origin!r}")
    if not isinstance(action, str) or not action:
        raise ValueError("action must be a non-empty string")
    if summary is not None and len(summary) > _SUMMARY_MAX:
        raise ValueError(f"summary must be at most {_SUMMARY_MAX} characters")
    if connection_id is not None:
        if origin != "connected_platform":
            raise ValueError("connection_id is only meaningful with origin='connected_platform'")
        if not connection_id or len(connection_id) > _CONNECTION_ID_MAX:
            raise ValueError(f"connection_id must be 1 to {_CONNECTION_ID_MAX} characters")
    revision = current_revision + 1
    origin_value: dict[str, Any] = {"kind": origin}
    if connection_id is not None:
        origin_value["connection_id"] = connection_id
    entry: dict[str, Any] = {
        "revision": revision,
        "timestamp": _rfc3339(timestamp or datetime.now(timezone.utc)),
        "action": action,
        "origin": origin_value,
    }
    if actor is not None:
        entry["actor"] = actor
    if summary is not None:
        entry["summary"] = summary
    if package_id is not None:
        entry["package_id"] = package_id
    if ext is not None:
        entry["ext"] = deepcopy(dict(ext))
    return MediaBuyChange(revision=revision, history_entry=entry)
