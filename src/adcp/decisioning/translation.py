"""Bidirectional, type-safe key translation between AdCP and upstream values.

Every translator-pattern adapter owes the framework two things: a thin
HTTP client (see :mod:`adcp.decisioning.upstream`) and a set of small
bidirectional key maps (AdCP ``"olv"`` ↔ upstream ``"video"``, AdCP
``"guaranteed"`` ↔ upstream ``"GUARANTEED_AT_PRICE"``, …).
:func:`create_translation_map` ships the map primitive so adapters
declare the mapping once and the framework projects it both ways.

Mirrors the JS ``createTranslationMap`` helper from
``@adcp/sdk@6.7`` (``src/lib/server/upstream-helpers.ts``).
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Generic, TypeVar

A = TypeVar("A")
U = TypeVar("U")


class TranslationMap(Generic[A, U]):
    """Reversible mapping between AdCP wire values and upstream platform values.

    Construct via :func:`create_translation_map`. Both lookup methods
    raise :class:`KeyError` for unknown keys unless a default is
    configured. Use the ``has_*`` predicates to guard a lookup, or
    pass ``default_adcp`` / ``default_upstream`` for graceful
    fallback.

    Example::

        channel_map = create_translation_map({
            "olv": "video",
            "ctv": "ctv",
            "display": "display",
            "streaming_audio": "audio",
        })
        channel_map.to_upstream("olv")     # "video"
        channel_map.to_adcp("video")       # "olv"
        channel_map.has_adcp("olv")        # True
        channel_map.has_upstream("audio")  # True
    """

    __slots__ = ("_forward", "_reverse", "_default_adcp", "_default_upstream")

    def __init__(
        self,
        adcp_to_upstream: Mapping[A, U],
        *,
        default_adcp: A | None = None,
        default_upstream: U | None = None,
    ) -> None:
        self._forward: dict[A, U] = dict(adcp_to_upstream)
        self._default_adcp = default_adcp
        self._default_upstream = default_upstream

        # Build reverse map and detect collisions in the same pass. Two
        # AdCP keys mapping to the same upstream value would silently
        # overwrite without this check — fail loud at construction so
        # the bug is caught at boot, not on a request.
        reverse: dict[U, A] = {}
        for adcp_key, upstream_key in self._forward.items():
            if upstream_key in reverse:
                raise ValueError(
                    f"translation collision: AdCP keys "
                    f"{reverse[upstream_key]!r} and {adcp_key!r} both map to "
                    f"upstream value {upstream_key!r}"
                )
            reverse[upstream_key] = adcp_key
        self._reverse: dict[U, A] = reverse

    def to_upstream(self, adcp_key: A) -> U:
        """Translate an AdCP wire value to the upstream platform value.

        :raises KeyError: when ``adcp_key`` isn't in the map and no
            ``default_upstream`` was provided.
        """
        if adcp_key in self._forward:
            return self._forward[adcp_key]
        if self._default_upstream is not None:
            return self._default_upstream
        raise KeyError(f"unknown AdCP key: {adcp_key!r}")

    def to_adcp(self, upstream_key: U) -> A:
        """Translate an upstream platform value back to the AdCP wire value.

        :raises KeyError: when ``upstream_key`` isn't in the map and
            no ``default_adcp`` was provided.
        """
        if upstream_key in self._reverse:
            return self._reverse[upstream_key]
        if self._default_adcp is not None:
            return self._default_adcp
        raise KeyError(f"unknown upstream key: {upstream_key!r}")

    def has_adcp(self, value: object) -> bool:
        """True when ``value`` is a known AdCP-side key."""
        return value in self._forward

    def has_upstream(self, value: object) -> bool:
        """True when ``value`` is a known upstream-side key."""
        return value in self._reverse


def create_translation_map(
    adcp_to_upstream: Mapping[A, U],
    *,
    default_adcp: A | None = None,
    default_upstream: U | None = None,
) -> TranslationMap[A, U]:
    """Build a bidirectional translation map from an AdCP→upstream record.

    Keys on the left side are AdCP wire values; values on the right
    are upstream platform values. Collisions in either direction (two
    AdCP keys mapping to the same upstream value) are detected at
    construction time and raise :class:`ValueError` — silent
    overwrite would produce wrong-tenant routing in production.

    :param adcp_to_upstream: ``{adcp_key: upstream_key}`` mapping.
    :param default_adcp: Returned by :meth:`TranslationMap.to_adcp`
        when the upstream key isn't in the map. ``None`` (default)
        raises ``KeyError``.
    :param default_upstream: Returned by
        :meth:`TranslationMap.to_upstream` when the AdCP key isn't in
        the map. ``None`` (default) raises ``KeyError``.

    Example::

        delivery_map = create_translation_map({
            "guaranteed": "GUARANTEED_AT_PRICE",
            "non_guaranteed": "STANDARD",
        })
        delivery_map.to_upstream("guaranteed")  # "GUARANTEED_AT_PRICE"
        delivery_map.to_adcp("STANDARD")        # "non_guaranteed"
    """
    return TranslationMap(
        adcp_to_upstream,
        default_adcp=default_adcp,
        default_upstream=default_upstream,
    )


__all__ = ["TranslationMap", "create_translation_map"]
