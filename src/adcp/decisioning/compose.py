"""Method-level composition for :class:`DecisioningPlatform` adopters.

Provides :func:`compose_method` for wrapping a single platform method
with typed ``before`` / ``after`` hooks, plus three pre-built
``BeforeHook`` factories — :func:`require_account_match`,
:func:`require_advertiser_match`, :func:`require_org_scope` — for
common authorization gates.

Mirrors the JS-side ``composeMethod`` helper at
``src/lib/server/decisioning/compose.ts``. Adopters layer cross-cutting
concerns (security gates, audit, enrichment, caching) on individual
methods of an existing platform without re-typing every method by hand.

``before`` returning ``None`` falls through to the wrapped method.
Returning :class:`ShortCircuit` wraps the value to short-circuit;
``after`` (if any) still runs on the short-circuit value before it
flows back to the caller. The discriminated wrapper avoids the
``None``-as-sentinel footgun: adopters who forget the wrapper and
return a bare value get a :class:`TypeError` at runtime, not silent
short-circuit-with-``None``.

``after`` runs BEFORE response-schema validation — decorations must
satisfy the wire schema. Vendor-specific data goes under ``ext``
(the spec's typed extension surface), not at the top level.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Generic, TypeAlias

from typing_extensions import TypeVar

from adcp.decisioning.context import RequestContext
from adcp.decisioning.types import AdcpError

#: Request type — Pydantic request model the platform method accepts.
Req = TypeVar("Req")

#: Response type — Pydantic response model the platform method returns.
Res = TypeVar("Res")

#: Generic value type for :class:`ShortCircuit`.
T = TypeVar("T")


@dataclass(frozen=True)
class ShortCircuit(Generic[T]):
    """Wrapper a ``before`` hook returns to skip the inner method.

    Returning ``ShortCircuit(value=...)`` from a
    :data:`BeforeHook` skips the wrapped inner method and feeds the
    wrapped value through ``after`` (if any) back to the caller.
    Returning ``None`` falls through to the inner method.

    Discriminated wrapper rather than a sentinel ``None``: adopters
    who omit the wrapper and return a bare value get a
    :class:`TypeError` at runtime rather than silent
    short-circuit-with-``None`` behavior — the most common footgun
    when porting middleware between languages.

    :param value: The result to return in place of calling the inner
        method. Must satisfy the wire schema (any decoration via
        ``after`` runs before response-schema validation).
    """

    value: T


#: Platform-method signature — ``async (params, ctx) -> result``.
#:
#: All composed methods are async. Adopter platforms whose underlying
#: implementations are sync should wrap them in a thin
#: ``async def`` shim before passing to :func:`compose_method`.
PlatformMethod: TypeAlias = Callable[[Req, RequestContext[Any]], Awaitable[Res]]

#: Before-hook signature — ``async (params, ctx) -> ShortCircuit[Res] | None``.
#:
#: Returning ``None`` falls through to the inner method. Returning
#: :class:`ShortCircuit` wraps the value to short-circuit.
BeforeHook: TypeAlias = Callable[
    [Req, RequestContext[Any]],
    Awaitable[ShortCircuit[Res] | None],
]

#: After-hook signature — ``async (result, params, ctx) -> Res``.
#:
#: Runs whether the result came from the inner method or from a
#: ``before`` short-circuit. Receives the original ``params`` and
#: ``ctx`` for context-dependent enrichment.
AfterHook: TypeAlias = Callable[
    [Res, Req, RequestContext[Any]],
    Awaitable[Res],
]


def compose_method(
    inner: Callable[[Req, RequestContext[Any]], Awaitable[Res]],
    *,
    before: (
        Callable[
            [Req, RequestContext[Any]],
            Awaitable[ShortCircuit[Res] | None],
        ]
        | None
    ) = None,
    after: (
        Callable[
            [Res, Req, RequestContext[Any]],
            Awaitable[Res],
        ]
        | None
    ) = None,
) -> Callable[[Req, RequestContext[Any]], Awaitable[Res]]:
    """Wrap a platform method with typed ``before`` / ``after`` hooks.

    Type-preserving: the returned callable has the same
    ``async (params, ctx) -> Res`` signature as ``inner`` so it slots
    into a typed :class:`DecisioningPlatform` shape without casts.

    Validates ``inner`` is callable eagerly at wrap time so adopters
    who reference an optional method that wasn't implemented on the
    underlying platform get a clear :class:`TypeError` at module load
    rather than at first traffic.

    Example::

        from adcp.decisioning.compose import compose_method, ShortCircuit

        async def before_hook(req, ctx):
            if req.optimization == "price":
                return ShortCircuit(value=cached_price_opt)
            return None

        async def after_hook(result, req, ctx):
            return result.model_copy(
                update={
                    "ext": {
                        **(result.ext or {}),
                        "carbon_grams_per_impression": await score(result),
                    }
                }
            )

        wrapped = compose_method(
            base_platform.get_media_buy_delivery,
            before=before_hook,
            after=after_hook,
        )

    :param inner: The platform method to wrap. Must be callable;
        non-callables raise :class:`TypeError` immediately.
    :param before: Optional pre-call hook. Returning
        :class:`ShortCircuit` skips the inner method and feeds the
        wrapped value through ``after``. Returning ``None`` falls
        through. Returning a bare non-:class:`ShortCircuit` non-``None``
        value raises :class:`TypeError`.
    :param after: Optional post-call hook. Runs whether the result
        came from ``inner`` or from a ``before`` short-circuit, and
        BEFORE response-schema validation. Decorations must satisfy
        the wire schema; vendor-specific data goes under ``ext``.
    :returns: A wrapper with the same signature as ``inner``.
    :raises TypeError: when ``inner`` is not callable.
    """
    if not callable(inner):
        raise TypeError(
            f"compose_method: 'inner' must be callable, got "
            f"{type(inner).__name__}. Did you reference an optional "
            f"method that wasn't implemented on the platform?"
        )

    async def wrapper(req: Req, ctx: RequestContext[Any]) -> Res:
        result: Res
        if before is not None:
            early = await before(req, ctx)
            if early is None:
                result = await inner(req, ctx)
            elif isinstance(early, ShortCircuit):
                result = early.value
            else:
                raise TypeError(
                    f"compose_method: before hook returned "
                    f"{type(early).__name__}; expected None or "
                    f"ShortCircuit. Wrap the value: "
                    f"`return ShortCircuit(value=...)`."
                )
        else:
            result = await inner(req, ctx)
        if after is not None:
            result = await after(result, req, ctx)
        return result

    return wrapper


# ---------------------------------------------------------------------------
# Security composer helpers
# ---------------------------------------------------------------------------

# Generic message used on every denial path. Adopters who need
# custom messaging should write their own ``BeforeHook`` rather than
# parameterizing this — the framework's default keeps the message
# from being a side channel that leaks how each gate identified the
# mismatch.
_DENIED_MESSAGE = (
    "Caller is not authorized for this resource. The request's account "
    "scope does not match the authenticated principal's scope."
)


def _read_field(obj: Any, name: str) -> Any:
    """Read ``name`` off ``obj``. Pydantic models, dataclasses, dicts
    all supported. Missing field raises :class:`AdcpError` with
    ``PERMISSION_DENIED`` rather than ``AttributeError`` — a buyer
    referencing a non-existent field is denied uniformly with a
    field-mismatch, not crashed.
    """
    if isinstance(obj, dict):
        if name not in obj:
            raise AdcpError(
                "PERMISSION_DENIED",
                message=_DENIED_MESSAGE,
                recovery="correctable",
            )
        return obj[name]
    try:
        return getattr(obj, name)
    except AttributeError as exc:
        raise AdcpError(
            "PERMISSION_DENIED",
            message=_DENIED_MESSAGE,
            recovery="correctable",
        ) from exc


def _read_metadata_field(metadata: Any, name: str) -> Any:
    """Read ``name`` off the account metadata. Supports TypedDict /
    plain dict and adopter-defined dataclasses / Pydantic models
    uniformly. Missing field raises :class:`AdcpError`."""
    if metadata is None:
        raise AdcpError(
            "PERMISSION_DENIED",
            message=_DENIED_MESSAGE,
            recovery="correctable",
        )
    return _read_field(metadata, name)


def require_account_match(
    expected_account_field: str = "account_id",
) -> BeforeHook[Any, Any]:
    """Build a :data:`BeforeHook` that requires the request's account
    field equal ``ctx.account.id``.

    The most common security composer — gates a method so a buyer
    can only operate on their own account. Apply via
    :func:`compose_method`::

        wrapped = compose_method(
            base.get_media_buy_delivery,
            before=require_account_match(),
        )

    :param expected_account_field: Name of the field on the request
        Pydantic model carrying the buyer-supplied account id.
        Default ``"account_id"`` matches the AdCP wire convention.
    :returns: A :data:`BeforeHook` that raises :class:`AdcpError`
        with ``PERMISSION_DENIED`` on mismatch (or missing field) and
        falls through (returns ``None``) on match.
    """

    async def hook(req: Any, ctx: RequestContext[Any]) -> ShortCircuit[Any] | None:
        requested = _read_field(req, expected_account_field)
        if requested != ctx.account.id:
            raise AdcpError(
                "PERMISSION_DENIED",
                message=_DENIED_MESSAGE,
                recovery="correctable",
            )
        return None

    return hook


def require_advertiser_match(
    expected_advertiser_field: str = "advertiser_id",
) -> BeforeHook[Any, Any]:
    """Build a :data:`BeforeHook` that requires the request's
    advertiser field equal ``ctx.account.metadata['advertiser_id']``.

    Use for per-advertiser scope below the account level — adopters
    who run multi-advertiser accounts and need to prevent cross-
    advertiser access within an account.

    :param expected_advertiser_field: Name of the field on the request
        Pydantic model carrying the buyer-supplied advertiser id.
        Default ``"advertiser_id"`` matches the AdCP wire convention.
    :returns: A :data:`BeforeHook` that raises :class:`AdcpError`
        with ``PERMISSION_DENIED`` on mismatch (or missing
        ``advertiser_id`` in metadata) and falls through on match.
    """

    async def hook(req: Any, ctx: RequestContext[Any]) -> ShortCircuit[Any] | None:
        requested = _read_field(req, expected_advertiser_field)
        scoped = _read_metadata_field(ctx.account.metadata, "advertiser_id")
        if requested != scoped:
            raise AdcpError(
                "PERMISSION_DENIED",
                message=_DENIED_MESSAGE,
                recovery="correctable",
            )
        return None

    return hook


def require_org_scope(
    expected_org_field: str = "organization_id",
) -> BeforeHook[Any, Any]:
    """Build a :data:`BeforeHook` that requires the request's
    organization field equal ``ctx.account.metadata['organization_id']``.

    Use for org-level multi-tenancy where a single org owns multiple
    accounts and the authorization decision is at the org level
    (not the per-account level).

    :param expected_org_field: Name of the field on the request
        Pydantic model carrying the buyer-supplied organization id.
        Default ``"organization_id"`` matches the AdCP wire
        convention.
    :returns: A :data:`BeforeHook` that raises :class:`AdcpError`
        with ``PERMISSION_DENIED`` on mismatch (or missing
        ``organization_id`` in metadata) and falls through on match.
    """

    async def hook(req: Any, ctx: RequestContext[Any]) -> ShortCircuit[Any] | None:
        requested = _read_field(req, expected_org_field)
        scoped = _read_metadata_field(ctx.account.metadata, "organization_id")
        if requested != scoped:
            raise AdcpError(
                "PERMISSION_DENIED",
                message=_DENIED_MESSAGE,
                recovery="correctable",
            )
        return None

    return hook


__all__ = [
    "AfterHook",
    "BeforeHook",
    "PlatformMethod",
    "ShortCircuit",
    "compose_method",
    "require_account_match",
    "require_advertiser_match",
    "require_org_scope",
]
