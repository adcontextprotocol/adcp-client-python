"""Seller half of conditional wholesale feed reads.

A wholesale ``get_products`` (``buying_mode='wholesale'``) or ``get_signals``
(``discovery_mode='wholesale'``) response carries an opaque
``wholesale_feed_version`` (and optionally ``pricing_version``). A buyer echoes
it as ``if_wholesale_feed_version`` / ``if_pricing_version``; when nothing has
changed the seller answers ``unchanged: true`` with the item list omitted.
:mod:`adcp.feed_mirror` is the buyer half. This module gives the seller the
three pieces it otherwise hand-rolls:

- :func:`wholesale_feed_version` / :func:`pricing_version` — content tokens.
  Same buyer-visible content, same token; any buyer-visible change, a new one.
- :func:`check_feed_preconditions` — the request's mode and pairing rules.
- :func:`feed_unchanged` — whether the buyer's tokens are current. Answer
  with ``products_response(unchanged=True, ...)`` /
  ``signals_response(unchanged=True, ...)``.

Example::

    from adcp.server import feed_unchanged, products_response, wholesale_feed_version

    async def get_products(params, context=None):
        scope = "account" if has_overrides(params.account) else "public"
        feed = await load_wholesale_feed(params, scope)  # the whole feed, every page
        version = wholesale_feed_version(feed, cache_scope=scope)
        if feed_unchanged(params, feed_version=version):
            return products_response(
                unchanged=True, wholesale_feed_version=version, cache_scope=scope
            )
        page, pagination = paginate(feed, params.pagination)
        return products_response(
            page, wholesale_feed_version=version, cache_scope=scope, pagination=pagination
        )

Pass the same token as ``wholesale_feed_version`` to
:meth:`adcp.webhooks.WebhookSender.send_wholesale_feed` after a change, so a
buyer that uses the webhook version as a gap detector sees the version its
next conditional read returns.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable, Mapping
from typing import TYPE_CHECKING, Any

import rfc8785
from pydantic_core import to_jsonable_python

from adcp.server.responses import _serialize

if TYPE_CHECKING:
    from adcp.types import GetProductsRequest, GetSignalsRequest

__all__ = [
    "FRESHNESS_FIELDS",
    "PRICING_FIELDS",
    "check_feed_preconditions",
    "feed_unchanged",
    "pricing_version",
    "wholesale_feed_version",
]

# Item-relative paths of fields that record when a value was computed rather
# than what it is. A regenerated forecast with the same numbers is not a change
# a buyer needs to refetch for. ``valid_until`` stays in: a buyer reads it to
# decide whether its mirrored forecast has expired.
FRESHNESS_FIELDS: frozenset[str] = frozenset(
    {
        "forecast.generated_at",  # Product.forecast (DeliveryForecast)
        "coverage_forecast.generated_at",  # get_signals signals[].coverage_forecast
    }
)

# The pricing layer, per GetProductsResponse.pricing_version: "product
# pricing_options and nested signal_targeting_options pricing_options".
# Signals carry ``pricing_options`` at the top level of each item.
PRICING_FIELDS: frozenset[str] = frozenset(
    {
        "pricing_options",
        "signal_targeting_options.pricing_options",
    }
)

# Stable item identity: the keys FeedMirror indexes products and signals by.
_ITEM_ID_FIELDS = ("product_id", "signal_agent_segment_id")

_CACHE_SCOPES = ("public", "account")

_Path = tuple[str, ...]


def wholesale_feed_version(
    items: Iterable[Any],
    *,
    cache_scope: str,
    ignore: Iterable[str] = FRESHNESS_FIELDS,
    extra: Mapping[str, Any] | None = None,
) -> str:
    """Return a content token for a wholesale product or signal feed.

    ``items`` is the *whole* feed for the request's scoping tuple, not one
    page: ``pagination.cursor`` is outside the version's scope, so every page
    carries the same token. Items may be dicts or Pydantic models; each is
    serialized exactly as :func:`~adcp.server.responses.products_response`
    puts it on the wire, so the token follows what the buyer sees.

    Canonicalization:

    - Each item is RFC 8785 (JCS) canonical JSON — sorted keys, no
      whitespace, ES6 number form — hashed with SHA-256. The token is the
      SHA-256 of the sorted item digests, so the order of top-level items
      does not matter (a feed is a set keyed by ``product_id`` /
      ``signal_agent_segment_id``). Arrays *inside* an item keep their
      order: some are ordered by contract (``forecast.points`` ascend by
      budget), the schema declares no set semantics for the rest, and a
      reordering costs at worst one spurious refetch.
    - ``ignore`` names fields left out of the hash, as dot-separated paths
      from the item root (``"forecast.generated_at"``). Arrays are traversed
      without a path segment, so ``"pricing_options.price_guidance"``
      reaches every pricing option. A bare key matches only at the item root,
      never by name anywhere. Default: :data:`FRESHNESS_FIELDS`. A seller that
      versions pricing separately passes ``FRESHNESS_FIELDS | PRICING_FIELDS``
      and also returns :func:`pricing_version`.
    - ``cache_scope`` is part of the hash. The spec keys the comparator on
      ``(cache_scope, wholesale_feed_version)``; a public token can then never
      match an account-scoped read with the same content, or vice versa.
    - ``extra`` carries response-level content the buyer sees beside the
      items and that must move the version: ``collections`` when the request
      set ``include_collections`` (the spec requires distinct tokens for the
      two settings; an empty list still differs from ``None``), or get_signals'
      ``signal_pricing_group_rules``. It is hashed as-is, without ``ignore``.

    :returns: ``"sha256:"`` followed by 64 hex digits. The schema puts no
        format, length, or pattern constraint on the token; buyers treat it as
        opaque.
    :raises ValueError: an unknown ``cache_scope``, a malformed ``ignore``
        path, or a value outside I-JSON (for example a NaN or an integer
        beyond 2**53).
    """
    paths = _parse_paths(ignore)
    digests = sorted(_digest(_without(item, paths)) for item in _wire_items(items))
    return _token(cache_scope, digests, extra)


def pricing_version(items: Iterable[Any], *, cache_scope: str) -> str:
    """Return a token for the pricing layer of a wholesale feed.

    Covers :data:`PRICING_FIELDS` of every item, bound to the item by its
    ``product_id`` / ``signal_agent_segment_id`` so moving a price from one
    product to another is a change. Use it with
    ``wholesale_feed_version(items, ignore=FRESHNESS_FIELDS | PRICING_FIELDS)``:
    a re-price then moves only this token, and a structural change moves the
    feed token (which forces a full payload on its own). Canonicalization and
    ``cache_scope`` keying are as for :func:`wholesale_feed_version`.

    Separate pricing is optional; the spec lets a seller omit
    ``pricing_version`` and let ``wholesale_feed_version`` cover both. Note
    that a wholesale feed webhook carries only ``wholesale_feed_version``,
    which a price-only change does not move when pricing is separated.

    :raises ValueError: an item with neither identity field, or as for
        :func:`wholesale_feed_version`.
    """
    keep = _parse_paths(PRICING_FIELDS)
    digests = []
    for item in _wire_items(items):
        if not isinstance(item, dict) or not any(key in item for key in _ITEM_ID_FIELDS):
            raise ValueError(
                "pricing_version items need product_id or signal_agent_segment_id "
                "to bind each price to its item"
            )
        projection = {key: item[key] for key in _ITEM_ID_FIELDS if key in item}
        projection.update(_only(item, keep))
        digests.append(_digest(projection))
    return _token(cache_scope, sorted(digests), None)


def check_feed_preconditions(request: GetProductsRequest | GetSignalsRequest | Any) -> None:
    """Enforce the conditional-read rules of a get_products / get_signals request.

    - ``if_pricing_version`` requires ``if_wholesale_feed_version`` (pricing
      has no structural baseline on its own).
    - Either token requires a wholesale read: ``buying_mode='wholesale'`` on
      get_products, ``discovery_mode='wholesale'`` on get_signals.

    ``request`` is a request model or its wire dict. A request without
    either token passes. Complements
    :func:`adcp.decisioning.refine.assert_buying_mode_consistent`, which
    checks ``brief`` / ``refine`` but not these tokens.

    :raises adcp.decisioning.AdcpError: ``INVALID_REQUEST`` with ``field``
        naming the offending token.
    """
    from adcp.decisioning.types import AdcpError

    feed_token = _get(request, "if_wholesale_feed_version")
    pricing_token = _get(request, "if_pricing_version")
    if pricing_token is not None and feed_token is None:
        raise AdcpError(
            "INVALID_REQUEST",
            message=(
                "if_pricing_version must be sent together with "
                "if_wholesale_feed_version; a pricing version has no "
                "structural baseline to compare against on its own."
            ),
            recovery="correctable",
            field="if_pricing_version",
        )
    if feed_token is None:
        return
    mode_field, mode = _mode(request)
    if mode != "wholesale":
        raise AdcpError(
            "INVALID_REQUEST",
            message=(
                f"if_wholesale_feed_version is only valid on a wholesale feed read "
                f"({mode_field}='wholesale'); got {mode_field}={mode!r}."
            ),
            recovery="correctable",
            field="if_wholesale_feed_version",
        )


def feed_unchanged(
    request: GetProductsRequest | GetSignalsRequest | Any,
    *,
    feed_version: str,
    pricing_version: str | None = None,
) -> bool:
    """Return whether the buyer's cached feed is current.

    ``feed_version`` / ``pricing_version`` are the seller's current tokens
    for the cache_scope it resolved *this* request to (from
    :func:`wholesale_feed_version` / :func:`pricing_version`, which key the
    token on that scope). Pass ``pricing_version`` only when the seller
    versions pricing separately.

    True only when ``if_wholesale_feed_version`` matches and — when the
    seller passes ``pricing_version`` — ``if_pricing_version`` is present and
    matches. A buyer that omits ``if_pricing_version`` from a seller that
    separates pricing gets the full payload: its feed token alone cannot show
    that prices are current. A seller that does not track pricing (no
    ``pricing_version``) ignores ``if_pricing_version``, as the spec allows.
    A continuation page (``pagination.cursor`` set) is never unchanged — the
    tokens version the feed's first page read.

    On True, answer with ``products_response(unchanged=True,
    wholesale_feed_version=..., cache_scope=..., pricing_version=...)`` (or
    :func:`~adcp.server.responses.signals_response`), echoing
    ``pricing_version`` when used.

    :raises adcp.decisioning.AdcpError: as :func:`check_feed_preconditions`.
    """
    check_feed_preconditions(request)
    feed_token = _get(request, "if_wholesale_feed_version")
    if feed_token is None or feed_token != feed_version:
        return False
    pagination = _get(request, "pagination")
    if pagination is not None and _get(pagination, "cursor") is not None:
        return False
    if pricing_version is not None:
        return bool(_get(request, "if_pricing_version") == pricing_version)
    return True


# ----------------------------------------------------------------------
# Internals
# ----------------------------------------------------------------------


def _get(obj: Any, name: str) -> Any:
    if isinstance(obj, Mapping):
        return obj.get(name)
    return getattr(obj, name, None)


def _enum_value(value: Any) -> Any:
    return getattr(value, "value", value)


def _mode(request: Any) -> tuple[str, Any]:
    """Return the request's wholesale-mode field and value."""
    from adcp.types import GetSignalsRequest

    if isinstance(request, GetSignalsRequest) or (
        isinstance(request, Mapping) and "buying_mode" not in request
    ):
        # get_signals defaults discovery_mode to 'brief' when absent.
        return "discovery_mode", _enum_value(_get(request, "discovery_mode")) or "brief"
    return "buying_mode", _enum_value(_get(request, "buying_mode"))


def _wire_items(items: Iterable[Any]) -> list[Any]:
    # ``_serialize`` is the products_response / signals_response item
    # serializer; to_jsonable_python then settles anything a plain dict item
    # left as a Python object (a datetime, an enum) to its JSON form.
    return [to_jsonable_python(item) for item in _serialize(list(items))]


def _parse_paths(paths: Iterable[str]) -> frozenset[_Path]:
    if isinstance(paths, str):
        raise ValueError("ignore must be a collection of paths, not a single string")
    parsed = set()
    for path in paths:
        parts = tuple(path.split("."))
        if not all(parts):
            raise ValueError(f"invalid field path {path!r}")
        parsed.add(parts)
    return frozenset(parsed)


def _descend(paths: frozenset[_Path], key: str) -> frozenset[_Path]:
    return frozenset(path[1:] for path in paths if len(path) > 1 and path[0] == key)


def _without(value: Any, paths: frozenset[_Path]) -> Any:
    """Copy ``value`` minus the fields at ``paths``."""
    if not paths:
        return value
    if isinstance(value, list):
        return [_without(entry, paths) for entry in value]
    if isinstance(value, dict):
        return {
            key: _without(entry, _descend(paths, key))
            for key, entry in value.items()
            if (key,) not in paths
        }
    return value


def _only(value: Any, paths: frozenset[_Path]) -> Any:
    """Copy only the fields at ``paths`` out of ``value``, keeping their structure."""
    if isinstance(value, list):
        return [_only(entry, paths) for entry in value]
    if isinstance(value, dict):
        kept: dict[str, Any] = {}
        for key, entry in value.items():
            if (key,) in paths:
                kept[key] = entry
            elif nested := _descend(paths, key):
                kept[key] = _only(entry, nested)
        return kept
    return None


def _digest(value: Any) -> str:
    return hashlib.sha256(rfc8785.dumps(value)).hexdigest()


def _token(cache_scope: str, digests: list[str], extra: Mapping[str, Any] | None) -> str:
    scope = _enum_value(cache_scope)
    if scope not in _CACHE_SCOPES:
        raise ValueError(f"cache_scope must be 'public' or 'account', got {cache_scope!r}")
    document: dict[str, Any] = {"cache_scope": scope, "items": digests}
    if extra is not None:
        document["extra"] = to_jsonable_python(
            {
                key: _serialize(v) if isinstance(v, list) else _serialize([v])[0]
                for key, v in extra.items()
            }
        )
    return f"sha256:{_digest(document)}"
