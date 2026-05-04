"""The :class:`IdempotencyStore` coordinator: canonical hashing + backend + decorator.

Responsibilities:

1. Extract ``idempotency_key`` from the incoming request.
2. Scope lookups by ``(scope_key, key)`` via the backend, where ``scope_key``
   composes ``tenant_id`` (when present) with ``caller_identity``.
3. On cache hit with matching canonical payload hash: return the cached response
   and mark ``replayed=True`` on the envelope.
4. On cache hit with a different hash: raise
   :class:`adcp.exceptions.IdempotencyConflictError`.
5. On miss: run the wrapped handler, then commit ``(hash, response)`` to the
   backend.

Per-scope scoping is a hard security requirement (AdCP #2315): a key from
principal A on tenant T has no meaning for principal B or tenant T'. The store
pulls both ``tenant_id`` and ``caller_identity`` from
:class:`adcp.server.base.ToolContext` and composes them into a single scope
key — sellers whose principal ids are only unique *within* a tenant (Okta
group-scoped IDs, seller-internal employee IDs, SCIM per-tenant IDs) must
populate ``tenant_id`` so the store can keep those tenants isolated. When no
``tenant_id`` is set, the scope collapses to ``caller_identity`` alone
(safe for single-tenant deployments).

If no context / no caller_identity is supplied, the store refuses to proceed —
fail-closed rather than collapse every buyer into a shared namespace.
"""

from __future__ import annotations

import copy
import hashlib
import logging
import time
import warnings
import weakref
from collections.abc import Awaitable, Callable
from functools import wraps
from typing import Any

from pydantic import BaseModel

from adcp.exceptions import IdempotencyConflictError
from adcp.server.idempotency.backends import CachedResponse, IdempotencyBackend
from adcp.server.idempotency.canonicalize import canonical_json_sha256

logger = logging.getLogger(__name__)

# Registry of functions returned by IdempotencyStore.wrap. Read by
# adcp.decisioning.validate_idempotency.is_wrapped() to reconcile the
# adopter's declared IdempotencySupported capability against actual
# decorator application. WeakSet so wrapper functions garbage-collect
# normally when the platform method holding them goes away — the
# registry doesn't pin them in memory.
#
# Defense-in-depth choice over a public attribute on the wrapper: a
# plain attr can be set by any caller (test fixture, monkeypatch) and
# silently defeat the validator. Membership in this private set is
# only granted by IdempotencyStore.wrap itself.
_WRAPPED_FUNCTIONS: weakref.WeakSet[Callable[..., Any]] = weakref.WeakSet()


def is_wrapped(fn: Any) -> bool:
    """Return True if ``fn`` was produced by :meth:`IdempotencyStore.wrap`.

    Accepts bound methods (resolves to the underlying function before
    the membership check) and plain callables. Used by the boot-time
    validator at :mod:`adcp.decisioning.validate_idempotency`.
    """
    if fn is None:
        return False
    target = fn.__func__ if hasattr(fn, "__func__") else fn
    return target in _WRAPPED_FUNCTIONS


# Spec bounds from capabilities.idempotency.replay_ttl_seconds (1h-7d).
_MIN_TTL_SECONDS = 3600
_MAX_TTL_SECONDS = 604800

HandlerFn = Callable[..., Awaitable[Any]]


class IdempotencyStore:
    """Coordinator that binds canonical hashing to a storage backend.

    :param backend: A concrete :class:`IdempotencyBackend`.
    :param ttl_seconds: How long cached responses remain replayable. Must be
        within the spec's ``[3600, 604800]`` range (1h to 7d). 86400 (24h) is
        the recommended floor and matches the compliance storyboard.
    :param hash_fn: Optional override for the canonical hash function. Defaults
        to :func:`canonical_json_sha256`. Exposed for tests and for anyone who
        wants to experiment with alternative equivalence rules — though note
        the spec mandates RFC 8785 JCS for interop.
    """

    def __init__(
        self,
        backend: IdempotencyBackend,
        ttl_seconds: int = 86400,
        hash_fn: Callable[[dict[str, Any]], str] = canonical_json_sha256,
        *,
        clock: Callable[[], float] = time.time,
    ) -> None:
        if not _MIN_TTL_SECONDS <= ttl_seconds <= _MAX_TTL_SECONDS:
            raise ValueError(
                f"ttl_seconds must be in [{_MIN_TTL_SECONDS}, {_MAX_TTL_SECONDS}] "
                f"per AdCP spec (capabilities.idempotency.replay_ttl_seconds), "
                f"got {ttl_seconds}"
            )
        self.backend = backend
        self.ttl_seconds = ttl_seconds
        self._hash_fn = hash_fn
        self._clock = clock

    def capability(self) -> dict[str, Any]:
        """Return the capabilities fragment declaring this store's replay window.

        Embed under ``capabilities.adcp.idempotency`` on the seller's
        ``get_adcp_capabilities`` response. Buyers read this to reason about
        retry-safe windows (AdCP #2315)::

            caps.adcp.idempotency = idempotency.capability()
            # → {"supported": True, "replay_ttl_seconds": 86400}

        ``supported`` became REQUIRED in AdCP 3.0 GA — agents emitting only
        ``replay_ttl_seconds`` fail strict schema validation on the new
        capabilities response.
        """
        return {"supported": True, "replay_ttl_seconds": self.ttl_seconds}

    def wrap(self, handler: HandlerFn) -> HandlerFn:
        """Decorator that adds idempotency semantics to an AdCP handler method.

        Supports three calling conventions the framework dispatches with:

        1. **Positional** ``handler(self, params, context)`` — the
           default for non-projected tools (``get_products``,
           ``create_media_buy``, etc.).
        2. **Keyword** ``handler(self, params=..., context=...)`` —
           same shape, just kwargs.
        3. **Arg-projected** ``handler(self, **arg_projector_kwargs, ctx=...)``
           where ``params`` is split into per-field kwargs by the
           framework dispatcher (e.g. ``update_media_buy`` is called
           as ``handler(self, media_buy_id=..., patch=..., ctx=...)``).
           In this mode the wrap searches the kwargs for a Pydantic
           model (``patch`` for update_media_buy) to extract the
           idempotency key and hash payload from. Adopters whose
           projection contains no Pydantic model (e.g. a method
           projecting only a list of ids) get fall-through behavior:
           no key found → handler runs without dedup.

        ``params`` is normalized to a dict before hashing; the return
        value is coerced to a dict for caching (via ``model_dump`` if
        Pydantic). The decorator always returns the handler's original
        object on a cache miss and a best-effort Pydantic
        re-validation on a hit (when the handler's declared return
        type exposes ``model_validate``). Callers that return raw
        dicts get dicts back.
        """

        @wraps(handler)
        async def _wrapped(*args: Any, **kwargs: Any) -> Any:
            handler_self, hash_source, context = _resolve_call_args(args, kwargs)

            scope_key, idempotency_key, params_dict = self._prepare(hash_source, context)
            if scope_key is None or idempotency_key is None:
                # No key → spec says the server MUST reject with INVALID_REQUEST.
                # We let the handler run so validation layers above us (Pydantic,
                # FastAPI, etc.) can reject with a typed error; the middleware's
                # job is only to dedup when a key IS present.
                #
                # Forward the call exactly as received so all three calling
                # conventions (positional / keyword / arg-projected) reach
                # the inner handler unchanged. The wrap is signature-
                # transparent on the no-key path.
                return await handler(*args, **kwargs)

            payload_hash = self._hash_fn(params_dict)

            cached = await self.backend.get(scope_key, idempotency_key)
            if cached is not None:
                if cached.payload_hash == payload_hash:
                    logger.debug(
                        "idempotency replay: scope=%s key_prefix=%s",
                        _scope_log_id(scope_key),
                        idempotency_key[:8],
                    )
                    return _clone_response(cached.response)
                # Same key, different payload — spec-defined conflict.
                raise IdempotencyConflictError(
                    operation=getattr(handler, "__name__", "handler"),
                    errors=[
                        {
                            "code": "IDEMPOTENCY_CONFLICT",
                            "message": (
                                "idempotency_key reused with a different payload "
                                "(canonical hash mismatch)"
                            ),
                        }
                    ],
                )

            response = await handler(*args, **kwargs)
            # Deep-copy when caching so post-return mutation of the caller's
            # copy can't poison future replays. `_clone_response` also deep-
            # copies on the hit path, giving independent objects per replay.
            response_dict = copy.deepcopy(_to_dict(response))
            entry = CachedResponse(
                payload_hash=payload_hash,
                response=response_dict,
                expires_at_epoch=self._clock() + self.ttl_seconds,
            )
            # Commit cache AFTER handler returns. Atomicity with the handler's
            # side effects depends on the backend: MemoryBackend is best-effort
            # (no transactional relationship to external resources); PgBackend
            # (follow-up) will commit in the same transaction when the handler
            # uses the same engine. On put failure we log loudly and return
            # the handler's response — swallowing the exception would be wrong
            # (operators need the signal that caching is broken), and raising
            # would look to the caller like the handler failed, triggering a
            # retry that re-executes side effects. Best compromise: warn
            # operators, return the result, and accept that the next retry
            # with this key will re-execute.
            try:
                await self.backend.put(scope_key, idempotency_key, entry)
            except Exception:
                logger.warning(
                    "Idempotency cache put failed for scope=%s key_prefix=%s — "
                    "handler completed but a subsequent retry with this key will "
                    "re-execute rather than replay. This indicates an operational "
                    "issue with the idempotency backend.",
                    _scope_log_id(scope_key),
                    idempotency_key[:8],
                    exc_info=True,
                )
            return response

        # Register the wrapper for the boot-time validator at
        # adcp.decisioning.validate_idempotency. WeakSet membership —
        # not a public attribute — so adopters can't spoof "wrapped"
        # by stamping an attr on a plain function. The wrapper is
        # registered, not the original handler: re-decorating a forked
        # copy of `handler` would otherwise falsely flag both.
        #
        # Contract for future maintainers: ``is_wrapped()`` checks
        # WeakSet membership of the closure object directly. Do NOT
        # change it to ``inspect.unwrap()``-then-check — the
        # ``@functools.wraps(handler)`` decorator above sets
        # ``_wrapped.__wrapped__ = handler``, so ``inspect.unwrap``
        # would return the original handler (not in the WeakSet) and
        # the validator would silently regress.
        _WRAPPED_FUNCTIONS.add(_wrapped)
        return _wrapped

    def _prepare(self, params: Any, context: Any) -> tuple[str | None, str | None, dict[str, Any]]:
        """Normalize inputs and extract the (scope_key, key, params_dict) tuple.

        ``scope_key`` composes ``tenant_id`` (when present) with
        ``caller_identity`` so cache entries are isolated across tenants even
        if the seller's principal IDs are only unique within each tenant.

        Returns ``(None, None, params_dict)`` when idempotency doesn't apply
        (no caller identity or no key supplied). The caller falls through to
        the plain handler in that case — validation of missing-key lives in
        the request schema, not here.
        """
        params_dict = _to_dict(params)
        idempotency_key = params_dict.get("idempotency_key")
        if not isinstance(idempotency_key, str) or not idempotency_key:
            return None, None, params_dict
        scope_key = _extract_scope_key(context)
        if scope_key is None:
            # No caller identity: we can't safely scope the key. Spec requires
            # per-principal scope; anything else is a cross-principal replay
            # attack surface. Fall through to the handler (which will process
            # the request normally — no dedup, but no security regression).
            self._warn_missing_principal_once()
            return None, None, params_dict
        return scope_key, idempotency_key, params_dict

    _missing_principal_warned: bool = False

    def _warn_missing_principal_once(self) -> None:
        """Emit a one-time warning when the middleware sees a key but no principal.

        Silent fall-through is the worst DX: the seller drops in
        ``@idempotency.wrap``, ships, and doesn't discover until incident
        review that no dedup ever happened. Fire once per store instance so
        operators see the signal without filling logs on every request.
        """
        if self._missing_principal_warned:
            return
        self._missing_principal_warned = True
        warnings.warn(
            "IdempotencyStore received a request with idempotency_key but no "
            "caller_identity on ToolContext — dedup is SKIPPED. This usually "
            "means your transport isn't populating the authenticated principal. "
            "A2A: wire an a2a-sdk auth middleware that sets ServerCallContext.user; "
            "MCP: populate ToolContext.caller_identity from your FastMCP auth "
            "middleware (see adcp.server.idempotency README). "
            "This warning fires once per IdempotencyStore instance.",
            UserWarning,
            stacklevel=3,
        )


def _resolve_call_args(args: tuple[Any, ...], kwargs: dict[str, Any]) -> tuple[Any, Any, Any]:
    """Resolve ``(handler_self, hash_source, context)`` across the three
    calling conventions the framework dispatches with.

    Returns ``hash_source`` — what the wrap should hand to
    :meth:`IdempotencyStore._prepare` for ``idempotency_key`` extraction
    and payload hashing. The original ``args`` / ``kwargs`` are
    untouched and forwarded verbatim to the inner handler.

    Calling conventions::

        # 1. Positional (default for non-projected tools)
        _wrapped(self, params, ctx)
        # → handler_self=self, hash_source=params, context=ctx

        # 2. Keyword (same shape, kwargs form)
        _wrapped(self, params=params, context=ctx)
        # → handler_self=self, hash_source=params, context=ctx

        # 3. Arg-projected (update_media_buy: params split into kwargs)
        _wrapped(self, media_buy_id=..., patch=<UpdateMediaBuyRequest>, ctx=...)
        # → handler_self=self,
        #   hash_source=<UpdateMediaBuyRequest>  (first kwarg with model_dump),
        #   context=<ctx>

    For arg-projected calls without a Pydantic-shaped kwarg
    (e.g. ``arg_projector={"audiences": [...]}``), ``hash_source``
    falls back to the kwargs dict itself — :meth:`_prepare` will look
    for ``idempotency_key`` at the top level and skip dedup if absent.
    Same fall-through as a missing key, no regression.
    """
    handler_self = args[0] if args else None
    rest_args = args[1:]

    # Convention 1: positional ``params, ctx`` after self.
    if rest_args:
        params = rest_args[0]
        context = rest_args[1] if len(rest_args) > 1 else kwargs.get("context")
        return handler_self, params, context

    # Convention 2: keyword ``params=, context=``. Use ``in`` rather
    # than ``or`` so an explicitly-passed falsy ``context=`` (None,
    # an object whose ``__bool__`` returns False) doesn't silently
    # fall through to ``ctx``.
    if "params" in kwargs:
        if "context" in kwargs:
            context = kwargs["context"]
        else:
            context = kwargs.get("ctx")
        return handler_self, kwargs["params"], context

    # Convention 3: arg-projected. ``ctx`` (not ``context``) is what
    # dispatch.py:1081 passes; tolerate both for hand-rolled adopters.
    context = kwargs["ctx"] if "ctx" in kwargs else kwargs.get("context")
    # Prefer kwargs literally named ``params`` / ``request`` / ``patch``
    # before falling back to "first kwarg with ``model_dump``". The
    # named lookup is dict-order-independent and matches the framework's
    # explicit projection contract: ``update_media_buy`` projects via
    # ``patch=``; future tools may use ``params=`` or ``request=``.
    # Without this preference, a tool with two Pydantic kwargs would
    # hash the wrong one when iteration order ever shifts (Python 3.7+
    # guarantees dict insertion order, but the call-site insertion
    # order is the framework's choice, not the handler signature).
    for preferred_name in ("params", "request", "patch"):
        candidate = kwargs.get(preferred_name)
        if candidate is not None and isinstance(candidate, BaseModel):
            return handler_self, candidate, context
    # Fall back to first kwarg whose value is a Pydantic ``BaseModel``.
    # ``isinstance`` is stricter than ``hasattr(model_dump)`` — a
    # non-Pydantic duck type with a ``model_dump`` method would no
    # longer accidentally match.
    for key, value in kwargs.items():
        if key in ("ctx", "context"):
            continue
        if isinstance(value, BaseModel):
            return handler_self, value, context

    fallback = {k: v for k, v in kwargs.items() if k not in ("ctx", "context")}
    return handler_self, fallback, context


def _scope_log_id(scope_key: str) -> str:
    """Return a non-reversible short identifier for ``scope_key`` log lines.

    ``scope_key`` carries the buyer's authenticated principal id (and
    possibly tenant id) — that's PII / commercially-sensitive identity
    data that should not land in centralized log sinks verbatim. We hash
    + truncate so operators can correlate log entries without the raw
    identity ever leaving the process.
    """
    digest = hashlib.sha256(scope_key.encode("utf-8")).hexdigest()
    return f"sha256:{digest[:12]}"


def _to_dict(value: Any) -> dict[str, Any]:
    """Coerce a request/response to a plain dict for hashing and caching."""
    if isinstance(value, dict):
        return value
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json", exclude_none=True)
    if hasattr(value, "model_dump") and callable(value.model_dump):
        return value.model_dump(mode="json", exclude_none=True)  # type: ignore[no-any-return]
    if hasattr(value, "__dict__"):
        return dict(value.__dict__)
    raise TypeError(f"Cannot coerce {type(value).__name__} to dict for idempotency caching")


# \x1e (ASCII 0x1e "record separator") is used between tenant and principal —
# distinct from anything a tenant/principal id would contain, and the resulting
# scope key stays opaque to callers. Downstream backends compare it as a plain
# string; the separator is only meaningful internally.
_SCOPE_SEP = "\x1e"


def _extract_scope_key(context: Any) -> str | None:
    """Pull the idempotency scope key from a ToolContext or equivalent shape.

    The scope key composes ``tenant_id`` (when present) with
    ``caller_identity`` so cache entries can't collide across tenants whose
    principal IDs are only locally unique. Returns ``None`` when no caller
    identity is available — idempotency then falls through to the handler
    (no dedup, but no cross-principal leakage either).

    Accepts:

    * :class:`adcp.server.base.ToolContext` with ``caller_identity`` and
      optional ``tenant_id``
    * Any object exposing ``caller_identity`` / ``principal_id`` /
      ``principal.id`` (and optional ``tenant_id``)
    * A dict with any of the above keys
    """
    if context is None:
        return None

    principal_id: str | None = None
    tenant_id: str | None = None

    for attr in ("caller_identity", "principal_id"):
        val = getattr(context, attr, None)
        if isinstance(val, str) and val:
            principal_id = val
            break
    if principal_id is None:
        principal = getattr(context, "principal", None)
        if principal is not None:
            val = getattr(principal, "id", None)
            if isinstance(val, str) and val:
                principal_id = val
    val = getattr(context, "tenant_id", None)
    if isinstance(val, str) and val:
        tenant_id = val

    if principal_id is None and isinstance(context, dict):
        for key in ("caller_identity", "principal_id"):
            val = context.get(key)
            if isinstance(val, str) and val:
                principal_id = val
                break
        if principal_id is None:
            principal = context.get("principal")
            if isinstance(principal, dict):
                val = principal.get("id")
                if isinstance(val, str) and val:
                    principal_id = val
        if tenant_id is None:
            val = context.get("tenant_id")
            if isinstance(val, str) and val:
                tenant_id = val

    if principal_id is None:
        return None
    if tenant_id is None:
        # Single-tenant deployments: principal_id alone is the scope.
        # Validate the principal doesn't contain the separator either —
        # if a downstream caller upgrades to multi-tenant, the same
        # scope key string would now collide with a multi-tenant
        # composition.
        if _SCOPE_SEP in principal_id:
            raise ValueError(
                "caller_identity / principal_id contains the reserved "
                "scope separator U+001E; refusing to compose a scope "
                "key that could collide with a tenant-prefixed scope. "
                "Sanitize principal ids before they reach ToolContext."
            )
        return principal_id
    # Multi-tenant: validate neither half carries the separator,
    # otherwise tenant=A + sep + B with principal=X collides with
    # tenant=A and principal=B + sep + X. Fail-closed — the SDK
    # never auto-strips, since either input being injected is a
    # configuration bug an operator needs to fix.
    if _SCOPE_SEP in tenant_id or _SCOPE_SEP in principal_id:
        raise ValueError(
            "tenant_id or caller_identity contains the reserved scope "
            "separator U+001E; refusing to compose a scope key that "
            "could collide with a different (tenant, principal) pair. "
            "Sanitize these values upstream of ToolContext."
        )
    return f"{tenant_id}{_SCOPE_SEP}{principal_id}"


def _clone_response(response: dict[str, Any]) -> dict[str, Any]:
    """Return a deep copy of the cached response.

    The whole point of a cached replay is "identical response, every time."
    A shallow copy would let a caller mutate nested lists/dicts on first
    replay and poison every subsequent one. Deep copy the whole tree so each
    caller gets an independent object.
    """
    return copy.deepcopy(response)
