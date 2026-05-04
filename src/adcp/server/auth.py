"""Bearer-token HTTP authentication middleware for ADCP MCP servers.

`examples/mcp_with_auth_middleware.py` is the full, load-bearing
recipe for multi-tenant sellers. Four things have to be right at the
same time — a ContextVar carrier for the authenticated principal,
constant-time token compare, the AdCP/MCP discovery-method bypass, and
reset-in-finally to prevent cross-request leak. Getting any of them
wrong is a security incident. This module factors that recipe into a
middleware class + matching ``context_factory`` so sellers write four
lines of wiring instead of four pages of auth code.

Typical usage::

    from adcp.server import create_mcp_server
    from adcp.server.auth import (
        BearerTokenAuthMiddleware,
        Principal,
        auth_context_factory,
    )

    async def validate_token(token: str) -> Principal | None:
        row = await db.fetch_token(token)
        if row is None or row.revoked:
            return None
        return Principal(
            caller_identity=row.principal_id,
            tenant_id=row.tenant_id,
        )

    mcp = create_mcp_server(MyAgent(), context_factory=auth_context_factory)
    app = mcp.streamable_http_app()
    app.add_middleware(BearerTokenAuthMiddleware, validate_token=validate_token)

The middleware populates module-level ``ContextVar``s that
``auth_context_factory`` reads to build a
:class:`~adcp.server.ToolContext` per call. The same module-level
vars compose with any other auth layer a seller writes on top — e.g.,
an additional role-check middleware that reads
:data:`current_principal`.

Security invariants the middleware enforces:

* Tokens are compared with :func:`hmac.compare_digest` over SHA-256
  hashes, not raw string equality — :meth:`dict.__contains__` leaks
  match-prefix timing.
* ``initialize`` and ``tools/list`` (MCP handshake) plus
  ``get_adcp_capabilities`` (AdCP handshake) are exempt per spec;
  every other request requires a valid bearer token.
* ``ContextVar``s are reset in ``finally`` so a later task sharing the
  context can't read a stale principal.
* The JSON-RPC body is peeked but not consumed — downstream handlers
  still read the same bytes (Starlette caches the body via the
  ``_body`` attribute on the request).

What this middleware does NOT do:

* **Token storage.** You supply ``validate_token``; where tokens live
  (Postgres, Redis, Vault, an IdP) is yours to design.
* **Authorization.** The middleware answers "who is this?", not "can
  they do X?". Authorization checks run on the authenticated principal
  inside your handlers or as :data:`~adcp.server.SkillMiddleware`.
* **A2A auth.** A2A uses a different transport; wire a2a-sdk's
  ``ServerCallContext.user`` via a2a-sdk auth middleware on that side.
  The ``Principal`` / ``ToolContext`` shape is the same, so handlers
  work unchanged across transports.
"""

from __future__ import annotations

import hashlib
import hmac
import inspect
import json
import logging
from collections.abc import Awaitable, Mapping
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol, TypeVar

_V = TypeVar("_V")

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

from adcp.server.base import ToolContext
from adcp.server.mcp_tools import DISCOVERY_METHODS, DISCOVERY_TOOLS

logger = logging.getLogger("adcp.server.auth")


def _parse_bearer_header(header: str) -> str | None:
    """Parse ``Authorization: Bearer <token>`` per RFC 7235.

    Scheme comparison is case-insensitive and tolerates folded
    whitespace (any run of spaces, tabs, or newlines) between the
    scheme and the token — some clients send ``bearer`` (lowercase),
    ``Bearer\\t``, or ``Bearer  <token>`` (double space). Returns
    ``None`` when the scheme doesn't match or the token is empty /
    whitespace-only.
    """
    parts = header.split(maxsplit=1)
    if len(parts) != 2:
        return None
    scheme, token = parts
    if scheme.lower() != "bearer":
        return None
    return token.strip() or None


if TYPE_CHECKING:
    from starlette.requests import Request

    from adcp.server.serve import RequestMetadata


@dataclass(frozen=True)
class Principal:
    """An authenticated principal — the result of token validation.

    Returned by a :data:`TokenValidator` on success. Used to populate
    the transport-layer ``ContextVar``s that :func:`auth_context_factory`
    reads when building per-call :class:`~adcp.server.ToolContext`.

    :param caller_identity: Stable, globally-unique principal id within
        the tenant. See the
        :class:`~adcp.server.ToolContext.caller_identity` docstring for
        the stability contract and the failure mode when this is
        reused across logical principals.
    :param tenant_id: Tenant the principal belongs to. Populate unless
        your principal ids are globally unique across tenants — the
        server-side idempotency store scopes cache keys on
        ``(tenant_id, caller_identity)``. See
        :doc:`/multi-tenant-contract` for the full invariants.
    :param metadata: Optional extra fields the context_factory should
        propagate into :class:`~adcp.server.ToolContext.metadata`.
    """

    caller_identity: str
    tenant_id: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


class SyncTokenValidator(Protocol):
    """Synchronous token validator — ``def validate_token(token) -> Principal | None``."""

    def __call__(self, token: str) -> Principal | None: ...


class AsyncTokenValidator(Protocol):
    """Asynchronous token validator —
    ``async def validate_token(token) -> Principal | None``."""

    def __call__(self, token: str) -> Awaitable[Principal | None]: ...


TokenValidator = SyncTokenValidator | AsyncTokenValidator
"""Seller-supplied callable that validates a bearer token.

Called with the raw token string (``Authorization: Bearer <token>``
with the prefix already stripped). Return a :class:`Principal` on
success, ``None`` to reject. Sync and async callables are both
accepted — the middleware awaits the result when it's awaitable.

Declared as a union of two Protocols (rather than a
``Callable[[str], Principal | None | Awaitable[...]]`` alias)
because mypy narrows Protocol unions per-call-site: downstream code
using ``async def validate_token`` gets the async branch without
``type: ignore`` noise. Either protocol is a valid ``TokenValidator``.

**Do not raise on invalid tokens.** Exceptions become ``500 Internal
Server Error`` responses, which leak the presence of an auth path
to attackers who can't know a valid token. Return ``None`` instead.
"""


# Module-level ``ContextVar``s populated by the middleware, read by the
# matching ``context_factory``. Exported so sellers can read them from
# their own composed middleware layers (rate-limiter keyed by
# principal, per-tenant feature flags, etc.) without re-authenticating.
#
# Named ``current_*`` to match the FastAPI / Starlette convention for
# per-request state carriers. Keep ``default=None`` so a pre-auth or
# discovery-exempt request reads ``None`` instead of raising
# ``LookupError``.
current_principal: ContextVar[str | None] = ContextVar("adcp_auth_principal", default=None)
current_tenant: ContextVar[str | None] = ContextVar("adcp_auth_tenant", default=None)
current_principal_metadata: ContextVar[dict[str, Any] | None] = ContextVar(
    "adcp_auth_principal_metadata", default=None
)


class BearerTokenAuthMiddleware(BaseHTTPMiddleware):
    """Starlette HTTP middleware that gates every non-discovery JSON-RPC
    request on a valid bearer token.

    Instantiate via ``app.add_middleware`` with a seller-supplied
    :data:`TokenValidator`::

        app.add_middleware(
            BearerTokenAuthMiddleware,
            validate_token=my_validate_token,
        )

    On success, populates :data:`current_principal`,
    :data:`current_tenant`, and :data:`current_principal_metadata`
    for the duration of the downstream call. On failure, returns
    ``401`` without invoking the handler.

    **Discovery bypass.** ``initialize``, ``notifications/initialized``,
    and ``tools/list`` (MCP handshake) plus ``get_adcp_capabilities``
    (AdCP handshake) are always exempt — these run before any client
    has credentials. Operators who consider their tool surface
    sensitive can subclass and override :meth:`is_discovery_request`
    to tighten the bypass (e.g. require auth on ``tools/list``).

    **Body is peeked, not consumed.** The middleware reads the
    JSON-RPC payload to identify the ``method`` / ``tool`` name for
    the discovery gate; Starlette caches the body on the request so
    handlers still read it normally.

    :param app: The inner ASGI app. Passed by Starlette —
        ``app.add_middleware`` supplies it automatically.
    :param validate_token: Your token lookup. See :data:`TokenValidator`.
    :param unauthenticated_response: Optional override for the 401
        response body. Default is ``{"error": "unauthenticated"}``.
    :param header_name: Which HTTP header carries the credential.
        Default ``"authorization"`` (the spec-canonical bearer header).
        Adopters with legacy clients sending tokens via a custom header
        (e.g. ``"x-adcp-auth"``) override this. Header lookup is
        case-insensitive (Starlette normalizes).
    :param bearer_prefix_required: When ``True`` (default), the
        middleware strips a ``"Bearer "`` prefix and rejects headers
        without it. When ``False``, the raw header value is passed
        verbatim to ``validate_token`` — appropriate for non-OAuth
        custom-header schemes (``X-Api-Key: <token>``,
        ``x-adcp-auth: <token>``, etc.). Adopters changing
        ``header_name`` to a non-standard value usually want this set
        to ``False``.
    """

    def __init__(
        self,
        app: Any,
        *,
        validate_token: TokenValidator,
        unauthenticated_response: dict[str, Any] | None = None,
        header_name: str = "authorization",
        bearer_prefix_required: bool = True,
    ) -> None:
        super().__init__(app)
        self._validate_token = validate_token
        self._unauth_body = unauthenticated_response or {"error": "unauthenticated"}
        # Lower-cased once at construction so the per-request lookup
        # avoids the normalization. Starlette's Headers does
        # case-insensitive matching, so this is belt-and-suspenders.
        self._header_name = header_name.lower()
        self._bearer_prefix_required = bearer_prefix_required

    async def dispatch(self, request: Request, call_next: Any) -> Any:
        method, tool = await self._peek_jsonrpc(request)

        principal_token = None
        tenant_token = None
        metadata_token = None
        try:
            if self.is_discovery_request(method, tool):
                principal_token = current_principal.set(None)
                tenant_token = current_tenant.set(None)
                metadata_token = current_principal_metadata.set(None)
                return await call_next(request)

            raw_header = request.headers.get(self._header_name, "")
            if self._bearer_prefix_required:
                bearer = _parse_bearer_header(raw_header)
            else:
                # Custom-header schemes (X-Api-Key, x-adcp-auth, etc.) —
                # pass the raw value through unchanged. Strip whitespace
                # since copy-paste tokens often pick up trailing newlines.
                stripped = raw_header.strip()
                bearer = stripped or None
            if not bearer:
                return self._unauthenticated()

            try:
                raw = self._validate_token(bearer)
                principal: Principal | None
                if inspect.isawaitable(raw):
                    principal = await raw
                else:
                    principal = raw
            except Exception:
                # Validator failure must not leak stack info to the caller.
                # Fail closed — a buggy validator is an auth failure, not a
                # 500. Logged for operators.
                logger.exception("token validator raised")
                return self._unauthenticated()

            if principal is None:
                return self._unauthenticated()

            principal_token = current_principal.set(principal.caller_identity)
            tenant_token = current_tenant.set(principal.tenant_id)
            metadata_token = current_principal_metadata.set(
                dict(principal.metadata) if principal.metadata else None
            )
            return await call_next(request)
        finally:
            # Reset unconditionally so a later task sharing this context
            # doesn't read a stale principal. Matches the idempotency
            # store's "fail fast on missing caller_identity" contract.
            if principal_token is not None:
                current_principal.reset(principal_token)
            if tenant_token is not None:
                current_tenant.reset(tenant_token)
            if metadata_token is not None:
                current_principal_metadata.reset(metadata_token)

    def is_discovery_request(self, method: str | None, tool: str | None) -> bool:
        """True when the request should bypass auth.

        Defaults to the spec-mandated discovery set. Subclass + override
        to tighten (e.g. require auth on ``tools/list``) or loosen
        (e.g. add a seller-specific unauthenticated ping method).
        """
        if method in DISCOVERY_METHODS:
            return True
        return method == "tools/call" and tool in DISCOVERY_TOOLS

    def _unauthenticated(self) -> JSONResponse:
        return JSONResponse(self._unauth_body, status_code=401)

    @staticmethod
    async def _peek_jsonrpc(request: Request) -> tuple[str | None, str | None]:
        """Inspect the JSON-RPC body without preventing handlers from
        reading it downstream. Returns ``(method, tool_name)``.

        Explicitly caches the body on ``request._body`` so downstream
        handlers receive the same bytes. Starlette's ``Request`` caches
        the first ``.body()`` call via this attribute, but relying on
        that behavior implicitly is fragile — nested ASGI apps that
        read the raw ``receive`` callable (as FastMCP's streamable-HTTP
        transport does) will otherwise observe an empty body. The
        explicit assignment matches the documented Starlette middleware
        body-peek pattern.

        Fails closed on batch arrays — the JSON-RPC 2.0 spec allows
        them, but the handshake methods never come in batches and
        permitting them here would let a client smuggle a mutation past
        the discovery gate inside a batch.
        """
        body = await request.body()
        # Ensure the body is cached for downstream reads. ``request.body()``
        # already sets ``_body``; the explicit re-assignment is a belt-and-
        # suspenders guard against Starlette internals changing and a
        # pinned target for the body-round-trip test.
        request._body = body
        if not body:
            return None, None
        try:
            payload = json.loads(body)
        except ValueError:
            return None, None
        if not isinstance(payload, dict):
            return None, None
        method = payload.get("method")
        method = method if isinstance(method, str) else None
        if method != "tools/call":
            return method, None
        params = payload.get("params") or {}
        name = params.get("name") if isinstance(params, dict) else None
        return method, (name if isinstance(name, str) else None)


# ------------------------------------------------------------------
# Matching context_factory — reads what the middleware populated.
# ------------------------------------------------------------------


def auth_context_factory(meta: RequestMetadata) -> ToolContext:
    """Build a :class:`~adcp.server.ToolContext` from the ContextVars
    :class:`BearerTokenAuthMiddleware` populates.

    Pass this to :func:`~adcp.server.create_mcp_server` (or
    :func:`~adcp.server.serve`) alongside the middleware so handlers
    receive a typed context carrying the authenticated principal.

    Populates ``caller_identity``, ``tenant_id``, and a ``metadata``
    dict containing the transport + tool name plus anything the
    :class:`Principal` provided. SDK-owned keys (``tool_name``,
    ``transport``) take precedence over principal-supplied keys, so a
    validator returning ``Principal(metadata={"tool_name": "x"})``
    cannot shadow audit fields the SDK populates. Returns a bare
    :class:`ToolContext` — agents that want a typed subclass
    (e.g. :class:`~adcp.server.AccountAwareToolContext`) should copy
    the three-line body and return their own subclass instead.
    """
    principal_metadata = current_principal_metadata.get() or {}
    combined_metadata: dict[str, Any] = {
        **principal_metadata,
        "tool_name": meta.tool_name,
        "transport": meta.transport,
    }
    return ToolContext(
        request_id=meta.request_id,
        caller_identity=current_principal.get(),
        tenant_id=current_tenant.get(),
        metadata=combined_metadata,
    )


# ------------------------------------------------------------------
# Helpers sellers sometimes need when building their own validator.
# ------------------------------------------------------------------


def constant_time_token_match(token: str, stored_hashes: Mapping[str, _V]) -> _V | None:
    """Look up a token in a dict of SHA-256 hashes using
    :func:`hmac.compare_digest` rather than dict-containment.

    Dict lookup + equality (``candidate_hash in stored_hashes``) leaks
    prefix-match timing because the hash comparison short-circuits on
    first byte mismatch. Iterating every stored hash with
    ``compare_digest`` makes the wall-clock runtime independent of
    how much of the candidate matches any entry.

    Use this when your token store is small enough to iterate linearly
    (hundreds to low-thousands). For larger stores, use a database
    column of hashed tokens with an equality index + one
    ``compare_digest`` check on the single returned row.

    :param token: Raw bearer token supplied by the client.
    :param stored_hashes: ``{sha256_hex: value}`` dictionary. Returns
        ``value`` on the matching entry, ``None`` on no match.
    """
    if not token:
        return None
    candidate = hashlib.sha256(token.encode()).hexdigest()
    for stored_hash, value in stored_hashes.items():
        if hmac.compare_digest(candidate, stored_hash):
            return value
    return None


def validator_from_token_map(
    token_map: Mapping[str, Principal],
) -> SyncTokenValidator:
    """Build a :data:`TokenValidator` from a ``{raw_token: Principal}`` map.

    The shape most demo/test agents actually need — a fixed set of
    tokens mapped to principals — without having to write the
    constant-time plumbing. The returned validator hashes each raw
    token at construction time and does constant-time lookups via
    :func:`hmac.compare_digest` on every call, matching the security
    properties of a hand-rolled validator::

        validate_token = validator_from_token_map({
            "token-acme": Principal(caller_identity="p-acme", tenant_id="acme"),
            "token-globex": Principal(caller_identity="p-globex", tenant_id="globex"),
        })
        app.add_middleware(BearerTokenAuthMiddleware, validate_token=validate_token)

    Production agents looking tokens up in Postgres / Redis / Vault
    should write their own async validator instead — this helper is
    for the small-fixed-set case (demo, test, CI fixtures).

    :param token_map: Mapping of raw bearer tokens to their resolved
        :class:`Principal`. Tokens are hashed at construction; the
        plaintext is not retained.
    :returns: A :data:`SyncTokenValidator` (which satisfies
        :data:`TokenValidator`).
    """
    stored_hashes: dict[str, Principal] = {
        hashlib.sha256(token.encode()).hexdigest(): principal
        for token, principal in token_map.items()
    }

    def _validate(token: str) -> Principal | None:
        return constant_time_token_match(token, stored_hashes)

    return _validate
