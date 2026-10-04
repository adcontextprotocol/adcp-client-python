"""Cached adapter factories, registered for explicit test-suite prewarming."""

from collections.abc import Callable
from functools import cache
from typing import Any, TypeVar

from pydantic import TypeAdapter

T = TypeVar("T")
_FACTORIES: list[Callable[[], TypeAdapter[Any]]] = []


def deferred_adapter(factory: Callable[[], TypeAdapter[T]]) -> Callable[[], TypeAdapter[T]]:
    """Cache a first-use factory without constructing a schema at import time."""
    cached = cache(factory)
    _FACTORIES.append(cached)
    return cached


def build_registered_adapters() -> None:
    """Build adapters registered by imported SDK modules; retain rebuild caches."""
    for factory in _FACTORIES:
        factory().rebuild(force=False)
