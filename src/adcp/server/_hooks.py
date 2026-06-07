"""Shared pre-validation hook types, composition, and application.

Single source of truth for the per-tool pre-validation hook machinery used by
both the MCP and A2A transports. :mod:`adcp.server.spec_compat` re-exports the
public names (``PreValidationHook``, ``PreValidationHookChain``,
``PreValidationHooks``, ``compose_pre_validation_hooks``) for backward
compatibility, so adopters can keep importing them from either module.

This module has no transport or framework dependencies — it depends only on the
standard library — so it sits at the bottom of the server import layering and
every other ``adcp.server`` module may import from it freely.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from typing import Any, TypeAlias

PreValidationHook: TypeAlias = Callable[[str, dict[str, Any]], dict[str, Any]]
"""Callable shape for a pre-validation hook."""

PreValidationHookChain: TypeAlias = PreValidationHook | Sequence[PreValidationHook]
"""One hook or an ordered sequence of hooks for a single tool."""

PreValidationHooks: TypeAlias = dict[str, PreValidationHookChain]
"""Type alias for the ``pre_validation_hooks`` parameter of ``serve()``."""

__all__ = [
    "PreValidationHook",
    "PreValidationHookChain",
    "PreValidationHookError",
    "PreValidationHooks",
    "compose_pre_validation_hooks",
]


class PreValidationHookError(Exception):
    """A single hook in an ordered pre-validation chain failed.

    Carries the zero-based ``index`` of the failing hook within its chain and
    the resolved ``hook_name`` so the dispatcher can surface both in the
    ``INVALID_REQUEST`` message — naming the exact callable instead of a
    generic "pre_validation_hook raised ...".
    """

    def __init__(self, *, index: int, hook_name: str, message: str) -> None:
        self.index = index
        self.hook_name = hook_name
        super().__init__(f"pre_validation_hook[{index}] {hook_name} {message}")


def _hook_name(hook: PreValidationHook) -> str:
    """Best-effort human name for a hook (function ``__name__`` or class name)."""
    name = getattr(hook, "__name__", None)
    if isinstance(name, str) and name:
        return name
    return hook.__class__.__name__


def _flatten_pre_validation_hooks(
    hooks: PreValidationHookChain | None,
) -> tuple[PreValidationHook, ...]:
    """Normalize ``None`` / one hook / a sequence into a flat hook tuple."""
    if hooks is None:
        return ()
    if callable(hooks):
        return (hooks,)
    flattened = tuple(hooks)
    for hook in flattened:
        if not callable(hook):
            raise TypeError("pre-validation hook chains must contain callables")
    return flattened


def _apply_pre_validation_hooks(
    hooks: tuple[PreValidationHook, ...],
    tool_name: str,
    params: dict[str, Any],
) -> dict[str, Any]:
    """Run an ordered hook chain, threading each hook's output into the next.

    Each hook receives a shallow copy of the running params dict. A hook that
    raises, or returns a non-dict, is wrapped in :class:`PreValidationHookError`
    naming its chain index and callable.
    """
    next_params = params
    for index, hook in enumerate(hooks):
        hook_name = _hook_name(hook)
        try:
            next_params = hook(tool_name, dict(next_params))
        except Exception as exc:
            raise PreValidationHookError(
                index=index,
                hook_name=hook_name,
                message=f"raised {type(exc).__name__}: {exc}",
            ) from exc
        if not isinstance(next_params, dict):
            raise PreValidationHookError(
                index=index,
                hook_name=hook_name,
                message=f"returned {type(next_params).__name__}, expected dict",
            )
    return next_params


def compose_pre_validation_hooks(
    *hook_maps: Mapping[str, PreValidationHookChain] | None,
) -> dict[str, tuple[PreValidationHook, ...]]:
    """Compose ordered pre-validation hook maps.

    Later maps append to earlier maps for overlapping tool names. Each
    tool's hooks run left-to-right, feeding the returned args from one hook
    into the next.
    """
    composed: dict[str, list[PreValidationHook]] = {}
    for hook_map in hook_maps:
        if hook_map is None:
            continue
        for tool_name, chain in hook_map.items():
            composed.setdefault(tool_name, []).extend(_flatten_pre_validation_hooks(chain))
    return {tool_name: tuple(hooks) for tool_name, hooks in composed.items()}
