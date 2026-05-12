"""Bearer-token HTTP authentication middleware for ADCP MCP servers.

`examples/mcp_with_auth_middleware.py` is the full, load-bearing
recipe for multi-tenant sellers. Five things have to be right at the
same time — a ``request.state`` carrier that survives the stateful
streamable-http session boundary, a ContextVar fallback for stateless
mode and A2A, constant-time token compare, the AdCP/MCP
discovery-method bypass, and reset-in-finally to prevent cross-request
leak. Getting any of them wrong is a security incident. This module
factors that recipe into a middleware class + matching
``context_factory`` so sellers write four lines of wiring instead of
four pages of auth code.

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
* **A2A auth.** A2A uses a different transport; the same
  :class:`BearerTokenAuth` config object drives both legs when wired
  via :func:`adcp.server.serve`'s ``auth=`` kwarg. The A2A side is
  authenticated by a :class:`BearerTokenContextBuilder` plumbed into
  ``a2a-sdk``'s ``create_jsonrpc_routes(context_builder=...)`` seam,
  not by a Starlette middleware — that placement bypasses the
  ``/.well-known/agent-card.json`` route automatically (which is
  registered separately and never invokes the builder), satisfying
  A2A spec §4.1's mandate that the agent card be publicly accessible.
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
from typing import TYPE_CHECKING, Any, Literal, Protocol, TypeVar

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
current_transport: ContextVar[Literal["mcp", "a2a"] | None] = ContextVar(
    "adcp_transport", default=None
)


# Well-known ``request.state`` attribute names. The middleware writes
# these alongside the ContextVars; ``auth_context_factory`` reads them
# off the request reachable via :class:`RequestMetadata.request_context`.
# The state path is the only auth-propagation channel that survives the
# stateful streamable-http session boundary — the session task is a
# separate async task from the middleware's, so the ContextVars set
# above are invisible during dispatch in stateful mode. Stateless mode
# happens to share the dispatch context, so the ContextVar path also
# works there. The factory reads request.state first and falls back to
# the ContextVar so adopters running on stateless mode don't have to
# change anything.
REQUEST_STATE_PRINCIPAL = "adcp_auth_principal"
REQUEST_STATE_TENANT = "adcp_auth_tenant"
REQUEST_STATE_PRINCIPAL_METADATA = "adcp_auth_principal_metadata"


def _set_request_state(
    request: Any,
    principal_identity: str | None,
    tenant_id: str | None,
    principal_metadata: dict[str, Any] | None,
) -> None:
    """Write the auth triple onto ``request.state``.

    Defensive: silently no-ops if ``request`` lacks a ``state``
    attribute (e.g., a test double). Real Starlette ``Request`` objects
    always have it.
    """
    state = getattr(request, "state", None)
    if state is None:
        return
    setattr(state, REQUEST_STATE_PRINCIPAL, principal_identity)
    setattr(state, REQUEST_STATE_TENANT, tenant_id)
    setattr(state, REQUEST_STATE_PRINCIPAL_METADATA, principal_metadata)


def _read_request_state_auth(
    request: Any,
) -> tuple[str | None, str | None, dict[str, Any] | None] | None:
    """Read the auth triple off ``request.state``, or ``None`` if not set.

    ``None`` means the middleware never ran for this request (e.g., the
    factory was invoked outside an HTTP path) — the caller should fall
    back to the ContextVars.
    """
    state = getattr(request, "state", None)
    if state is None:
        return None
    if not hasattr(state, REQUEST_STATE_PRINCIPAL):
        return None
    return (
        getattr(state, REQUEST_STATE_PRINCIPAL, None),
        getattr(state, REQUEST_STATE_TENANT, None),
        getattr(state, REQUEST_STATE_PRINCIPAL_METADATA, None),
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
        to ``False``. **Security note:** setting this to ``False``
        removes the prefix pre-filter; ``validate_token`` must be
        defensive about unexpected input shapes and unbounded lengths.
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
                _set_request_state(request, None, None, None)
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

            principal_metadata = dict(principal.metadata) if principal.metadata else None
            principal_token = current_principal.set(principal.caller_identity)
            tenant_token = current_tenant.set(principal.tenant_id)
            metadata_token = current_principal_metadata.set(principal_metadata)
            # Mirror onto ``request.state`` so the dispatch-side
            # ``context_factory`` can read the principal even when the
            # MCP server is in stateful mode (where the session task is a
            # separate async task than this middleware's task and does
            # not see the ContextVar set above). ``request.state`` is the
            # standard Starlette per-request scratchpad and travels with
            # the request through any nested ASGI app.
            _set_request_state(
                request,
                principal.caller_identity,
                principal.tenant_id,
                principal_metadata,
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
    """Build a :class:`~adcp.server.ToolContext` from auth state the
    :class:`BearerTokenAuthMiddleware` populated for the in-flight
    request.

    Pass this to :func:`~adcp.server.create_mcp_server` (or
    :func:`~adcp.server.serve`) alongside the middleware so handlers
    receive a typed context carrying the authenticated principal.

    Resolution order:

    1. ``meta.request_context.state`` — the standard Starlette
       per-request scratchpad. Survives the stateful streamable-http
       session-task boundary (the dispatch sub-task gets the originating
       Starlette ``Request`` via the upstream MCP ``request_ctx``
       contextvar). Works on both stateless and stateful streamable-http.
    2. Module-level :data:`current_principal` etc. ContextVars — the
       legacy carrier. Works only when the dispatch runs in the same
       async task as the middleware (i.e., stateless streamable-http
       and A2A). In stateful streamable-http, these read ``None``
       because the session task is a separate task.

    Populates ``caller_identity``, ``tenant_id``, and a ``metadata``
    dict containing the transport + tool name plus anything the
    :class:`Principal` provided. SDK-owned keys (``tool_name``,
    ``transport``) take precedence over principal-supplied keys, so a
    validator returning ``Principal(metadata={"tool_name": "x"})``
    cannot shadow audit fields the SDK populates. Returns a bare
    :class:`ToolContext` — agents that want a typed subclass
    (e.g. :class:`~adcp.server.AccountAwareToolContext`) should copy
    the three-line body and return their own subclass instead.

    Also sets ``metadata["adcp.auth_info"]`` to a typed
    :class:`~adcp.decisioning.AuthInfo` when the request is
    authenticated, so :meth:`~adcp.decisioning.PlatformHandler._extract_auth_info`
    surfaces a non-``None`` :attr:`~adcp.decisioning.RequestContext.auth_info`
    for bearer flows — the same typed surface signed-request flows already
    populate.  ``credential`` is ``None`` for bearer flows because inbound
    bearer tokens are not for upstream propagation; adopters who need
    :class:`~adcp.decisioning.BuyerAgentRegistry` dispatch must supply a
    typed credential in a custom ``context_factory`` subclass.

    ``adcp.auth_info`` is server-internal and never wire-echoed by the
    framework. Do not pass ``ctx.metadata`` wholesale to a JSON serializer
    — the ``AuthInfo`` object is not JSON-serializable.
    """
    principal_identity: str | None = None
    tenant_id: str | None = None
    principal_metadata: dict[str, Any] | None = None
    if meta.request_context is not None:
        triple = _read_request_state_auth(meta.request_context)
        if triple is not None:
            principal_identity, tenant_id, principal_metadata = triple
    if principal_identity is None and tenant_id is None and principal_metadata is None:
        # Either no Request was threaded (stdio MCP, A2A pre-builder
        # path) or the middleware didn't write to state — fall back to
        # the ContextVars. Works on stateless streamable-http and A2A
        # where dispatch shares the middleware's task context.
        principal_identity = current_principal.get()
        tenant_id = current_tenant.get()
        principal_metadata = current_principal_metadata.get()
    principal_metadata = principal_metadata or {}
    combined_metadata: dict[str, Any] = {
        **principal_metadata,
        "tool_name": meta.tool_name,
        "transport": meta.transport,
    }
    if principal_identity is not None:
        # Lazy import to keep module-load order safe — decisioning.context
        # imports adcp.server.base but not adcp.server.auth, so there is no
        # circular dependency, but hoisting this to module level would create
        # one if the import graph ever changes. Call-time import matches
        # the pattern already used in dispatch._build_request_context.
        from adcp.decisioning.context import AuthInfo  # noqa: PLC0415

        combined_metadata["adcp.auth_info"] = AuthInfo(
            kind="bearer",
            principal=principal_identity,
            credential=None,  # explicit None: no synthesis, no DeprecationWarning
        )
    return ToolContext(
        request_id=meta.request_id,
        caller_identity=principal_identity,
        tenant_id=tenant_id,
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


# ---------------------------------------------------------------------------
# Cross-transport auth config — drives both MCP middleware and A2A builder
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BearerTokenAuth:
    """Cross-transport bearer-token auth config for :func:`adcp.server.serve`.

    Single source of truth that wires the same ``validate_token``
    callback into both the MCP-side :class:`BearerTokenAuthMiddleware`
    and the A2A-side :class:`A2ABearerAuthMiddleware`. Pass via
    ``serve(auth=BearerTokenAuth(...))`` and both legs are
    authenticated against the same token store with no per-leg
    drift::

        from adcp.server import serve
        from adcp.server.auth import BearerTokenAuth, validator_from_token_map

        serve(
            handler,
            transport="both",
            auth=BearerTokenAuth(
                validate_token=validator_from_token_map({
                    "secret-token": Principal(caller_identity="p", tenant_id="acme"),
                }),
            ),
        )

    On MCP, requests without a valid token receive a JSON ``401``
    body. On A2A, requests without a valid token receive an HTTP
    ``401``. Discovery bypasses are transport-specific:

    * **MCP**: ``initialize`` / ``tools/list`` / ``notifications/initialized``
      / ``get_adcp_capabilities`` (JSON-RPC method-level bypass).
    * **A2A**: ``/.well-known/agent-card.json`` (path-based — the
      agent-card route is registered alongside the JSON-RPC routes
      and the middleware exempts the well-known path).

    **Canonical carrier: ``Authorization: Bearer <token>`` (RFC 6750).**
    Both legs default to this. It is the only header backed by an actual
    RFC, what every off-the-shelf MCP / A2A / HTTP client emits by
    default, and what the AdCP spec is moving toward as canonical for
    both transports. Reach for ``BearerTokenAuth(validate_token=...)``
    with no other knobs and you get the protocol-canonical setup —
    including a ``bearerAuth`` ``HTTPAuthSecurityScheme`` (``scheme="bearer"``)
    auto-published on the agent card so a2a-sdk-based clients attach
    credentials without seller-side intervention.

    **``x-adcp-auth`` is a legacy-compat alias, not a recommended
    default.** Some early MCP adopters baked in a custom ``x-adcp-auth``
    header carrying a raw token (no scheme prefix) before the spec
    settled. Sellers with deployed clients that can't be updated can
    opt in per-leg::

        BearerTokenAuth(
            validate_token=...,
            mcp_header_name="x-adcp-auth",          # legacy MCP clients only
            mcp_bearer_prefix_required=False,
            # A2A keeps the canonical RFC 6750 carrier by default
        )

    Selecting a non-``Authorization`` header on the A2A leg is
    discouraged — buyers using non-a2a-sdk HTTP clients may not parse
    the resulting :class:`APIKeySecurityScheme` shape, and you lose
    interop with off-the-shelf A2A tooling. Use only when you control
    every buyer client.

    **Legacy single-knob compatibility.** ``header_name`` and
    ``bearer_prefix_required`` are still accepted: when set, they
    apply to *both* legs and override the per-leg defaults. Setting
    both ``header_name`` and a per-leg ``*_header_name`` (or both
    ``bearer_prefix_required`` and a per-leg
    ``*_bearer_prefix_required``) raises at construction — the
    framework can't decide which the operator intended.
    """

    validate_token: TokenValidator
    # Legacy single-knob — applies to BOTH legs when set. Mutually
    # exclusive with the per-leg knobs below. Adopters who want the
    # canonical RFC 6750 setup should leave these unset (defaults
    # resolve to ``Authorization`` + ``Bearer`` prefix).
    header_name: str | None = None
    bearer_prefix_required: bool | None = None
    # Per-leg knobs — opt-in escape hatch for adopters with legacy
    # clients that send a raw token in a custom header (e.g.
    # ``x-adcp-auth``). The protocol-canonical carrier is
    # ``Authorization: Bearer <token>`` on both legs; reach for these
    # only when you can't update the client side.
    mcp_header_name: str | None = None
    mcp_bearer_prefix_required: bool | None = None
    a2a_header_name: str | None = None
    a2a_bearer_prefix_required: bool | None = None
    unauthenticated_response: dict[str, Any] | None = None

    def __post_init__(self) -> None:
        if self.header_name is not None and (
            self.mcp_header_name is not None or self.a2a_header_name is not None
        ):
            raise ValueError(
                "BearerTokenAuth: set either header_name (applies to both legs) "
                "or mcp_header_name / a2a_header_name (per-leg) — not both."
            )
        if self.bearer_prefix_required is not None and (
            self.mcp_bearer_prefix_required is not None
            or self.a2a_bearer_prefix_required is not None
        ):
            raise ValueError(
                "BearerTokenAuth: set either bearer_prefix_required (applies "
                "to both legs) or mcp_bearer_prefix_required / "
                "a2a_bearer_prefix_required (per-leg) — not both."
            )

        # Reject empty-string headers — they would silently 401 every
        # request because no wire header matches an empty name. A typo
        # like ``header_name=""`` should fail loudly at construction.
        for field_name in ("header_name", "mcp_header_name", "a2a_header_name"):
            value = getattr(self, field_name)
            if value is not None and not value.strip():
                raise ValueError(f"BearerTokenAuth: {field_name} must be a non-empty string.")

        # ``Authorization`` is reserved by RFC 7235 for ``<scheme>
        # <credentials>``. Carrying a raw token in ``Authorization``
        # breaks RFC-compliant intermediaries and a2a-sdk's auth
        # interceptor (which treats the header as bearer-shaped). If an
        # adopter wants a raw token, they need a custom header name.
        for header_field, prefix_field, leg in (
            ("header_name", "bearer_prefix_required", "both"),
            ("mcp_header_name", "mcp_bearer_prefix_required", "MCP"),
            ("a2a_header_name", "a2a_bearer_prefix_required", "A2A"),
        ):
            header = getattr(self, header_field)
            prefix = getattr(self, prefix_field)
            if header is not None and header.lower() == "authorization" and prefix is False:
                raise ValueError(
                    f"BearerTokenAuth: {header_field}='Authorization' with "
                    f"{prefix_field}=False on the {leg} leg violates RFC 7235 "
                    "(Authorization carries '<scheme> <credentials>'). Use a "
                    "custom header name (e.g. 'x-adcp-auth') for raw-token "
                    "schemes."
                )

    def resolved_mcp_header_name(self) -> str:
        """Effective MCP header name after legacy + default fallback.

        Resolution order: legacy ``header_name`` → ``mcp_header_name``
        → ``"authorization"`` (RFC 6750, the protocol-canonical carrier
        on MCP). Adopters with legacy clients sending ``x-adcp-auth``
        opt in via ``mcp_header_name``; the default itself stays on
        ``Authorization`` because that's what the spec is moving
        toward as canonical.
        """
        if self.header_name is not None:
            return self.header_name
        if self.mcp_header_name is not None:
            return self.mcp_header_name
        return "authorization"

    def resolved_mcp_bearer_prefix_required(self) -> bool:
        """Effective MCP bearer-prefix flag after legacy + default fallback.

        Resolution order: legacy ``bearer_prefix_required`` →
        ``mcp_bearer_prefix_required`` → ``True`` (RFC 6750 — the
        canonical setup is ``Authorization: Bearer <token>``).
        """
        if self.bearer_prefix_required is not None:
            return self.bearer_prefix_required
        if self.mcp_bearer_prefix_required is not None:
            return self.mcp_bearer_prefix_required
        return True

    def resolved_a2a_header_name(self) -> str:
        """Effective A2A header name after legacy + default fallback.

        Resolution order: legacy ``header_name`` → ``a2a_header_name``
        → ``"Authorization"`` (RFC 6750 — what a2a-sdk and every
        off-the-shelf HTTP library send by default). Setting
        ``a2a_header_name`` to anything else is discouraged: buyers
        using non-a2a-sdk HTTP clients may not parse the resulting
        :class:`APIKeySecurityScheme` shape on the agent card and
        you lose interop with off-the-shelf A2A tooling.
        """
        if self.header_name is not None:
            return self.header_name
        if self.a2a_header_name is not None:
            return self.a2a_header_name
        return "Authorization"

    def resolved_a2a_bearer_prefix_required(self) -> bool:
        """Effective A2A bearer-prefix flag after legacy + default fallback.

        Resolution order: legacy ``bearer_prefix_required`` →
        ``a2a_bearer_prefix_required`` → ``True`` (RFC 6750 — the
        canonical setup is ``Authorization: Bearer <token>``).
        """
        if self.bearer_prefix_required is not None:
            return self.bearer_prefix_required
        if self.a2a_bearer_prefix_required is not None:
            return self.a2a_bearer_prefix_required
        return True


# ---------------------------------------------------------------------------
# A2A: ASGI middleware that gates JSON-RPC requests, exempts agent-card
# ---------------------------------------------------------------------------
#
# Why an ASGI middleware (not a ServerCallContextBuilder)?
# The a2a-sdk v0.3 compat adapter wraps the entire dispatch in
# ``except Exception`` and converts any error — including a builder-
# raised :class:`HTTPException(401)` — into a 200 OK with a JSON-RPC
# error body. That breaks the spec-canonical HTTP 401 contract and
# leaks the auth path as a 200. Authenticating outside the dispatcher,
# at the ASGI layer, returns proper HTTP 401 every time.
#
# A2A discovery (``/.well-known/agent-card.json``) is exempted by URL
# path here because the agent-card route happens to live in the same
# Starlette app — the middleware can't rely on the route topology
# alone. Path-exemption keeps the spec §4.1 public-discovery mandate
# satisfied even if a future a2a-sdk refactor merges the routes.


# Canonical 1.0 path is sourced from a2a-sdk's own constant — if a
# future a2a-sdk release renames the well-known URI, the import-time
# reference here lifts to the new value automatically and
# ``test_discovery_paths_match_a2a_sdk_routes`` verifies that the
# frozenset still covers every route ``create_agent_card_routes``
# actually registers. Hardcoding the string would silently leak auth
# on the renamed route until someone notices.
from a2a.utils.constants import (  # noqa: E402  (intentional placement after BearerTokenAuth definition)
    AGENT_CARD_WELL_KNOWN_PATH as _A2A_AGENT_CARD_PATH,
)

_A2A_DISCOVERY_PATHS: frozenset[str] = frozenset(
    {
        _A2A_AGENT_CARD_PATH,  # 1.0 canonical: ``/.well-known/agent-card.json``.
        # Legacy 0.3 alias — route registered explicitly in create_a2a_server.
        "/.well-known/agent.json",
    }
)


class A2ABearerAuthMiddleware:
    """Pure-ASGI middleware that gates A2A JSON-RPC on a bearer token.

    Wrap the Starlette app produced by
    :func:`adcp.server.a2a_server.create_a2a_server` with this
    middleware to require a valid bearer header on every JSON-RPC
    request, while leaving the spec-mandated public discovery
    surface (``/.well-known/agent-card.json`` and the 0.3 alias
    ``/.well-known/agent.json``) accessible.

    Designed to compose with a2a-sdk's
    :class:`DefaultServerCallContextBuilder`: on auth success the
    middleware writes a duck-typed user object into
    ``scope['user']`` and the principal into ``scope['auth']``,
    matching Starlette's :class:`AuthenticationMiddleware` contract.
    The default builder reads ``scope['user']`` and adapts it via
    :class:`a2a.server.routes.common.StarletteUser`, so downstream
    handlers see ``ServerCallContext.user.user_name`` populated with
    the principal's ``caller_identity`` without a custom builder.

    Also populates :data:`current_principal`, :data:`current_tenant`,
    and :data:`current_principal_metadata` for the duration of the
    downstream call — symmetric with
    :class:`BearerTokenAuthMiddleware`'s contract. Adopters reading
    ``current_principal.get()`` from a platform method see identical
    state on MCP and A2A.

    Composition order matters when ``transport="both"`` is in play:
    wrap the per-leg apps before any outer dispatcher closes over
    them. See ``serve.py:_build_mcp_and_a2a_app`` for the wiring.
    """

    def __init__(self, app: Any, config: BearerTokenAuth) -> None:
        self._app = app
        self._config = config
        self._header_name = config.resolved_a2a_header_name().lower()
        self._bearer_prefix_required = config.resolved_a2a_bearer_prefix_required()

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        # Lifespan + websocket pass through unchanged. Auth applies to
        # HTTP requests only.
        if scope.get("type") != "http":
            await self._app(scope, receive, send)
            return

        # CORS preflight is part of the public surface — browser-origin
        # clients send ``OPTIONS`` before any auth'd POST. Returning 401
        # here breaks the preflight and the buyer never gets a chance to
        # retry with a token. Pass through; let the inner app's CORS
        # handler (or operator-supplied ``asgi_middleware``) respond.
        if scope.get("method") == "OPTIONS":
            await self._app(scope, receive, send)
            return

        path = scope.get("path", "")
        if path in _A2A_DISCOVERY_PATHS:
            await self._app(scope, receive, send)
            return

        principal = self._authenticate_scope(scope)
        if principal is None:
            await self._send_unauthenticated(send)
            return

        # Stash both the duck-typed user (for DefaultServerCallContextBuilder)
        # and the raw Principal (for downstream code reading scope['auth']).
        # Mutating the scope dict before delegating propagates state to
        # nested apps without copying.
        principal_metadata = dict(principal.metadata) if principal.metadata else None
        scope["user"] = _A2AAuthenticatedUser(
            display_name=principal.caller_identity,
            tenant_id=principal.tenant_id,
            principal_metadata=principal_metadata,
        )
        scope["auth"] = principal

        # Populate the same ContextVars MCP's ``BearerTokenAuthMiddleware``
        # sets, so adopters reading ``current_principal.get()`` (or the
        # other two) from a platform method see identical state across
        # transports. Without this, A2A handlers fall through to the
        # ``None`` default while MCP handlers see the principal — a silent
        # transport-coupled divergence that breaks tenant policies that
        # require principal-bound calls. See issue #590.
        #
        # ContextVars carry on the A2A leg because the dispatch runs in
        # the same async task as this middleware (no session-task seam
        # like MCP stateful streamable-http). The MCP leg's mirror onto
        # ``request.state`` is what survives the stateful session-task
        # boundary; A2A's dispatcher reads ContextVars directly. If A2A
        # ever grows a long-lived dispatch task that decouples from the
        # request task, we'll need to thread the request through
        # ``RequestMetadata`` on the A2A side too.
        principal_token = current_principal.set(principal.caller_identity)
        tenant_token = current_tenant.set(principal.tenant_id)
        metadata_token = current_principal_metadata.set(principal_metadata)
        try:
            await self._app(scope, receive, send)
        finally:
            current_principal.reset(principal_token)
            current_tenant.reset(tenant_token)
            current_principal_metadata.reset(metadata_token)

    def _authenticate_scope(self, scope: Any) -> Principal | None:
        """Read + validate the bearer header off raw ASGI scope.

        Validator exceptions are projected to :data:`None` (logged for
        operators) so a buggy validator never leaks 500-level stack
        traces or signals path existence to unauthenticated callers.
        Auth-rejection branches log at INFO with a coarse reason code
        so SOC dashboards can detect scanning without bloating logs.
        """
        # ASGI ``headers`` is a list of ``(bytes_lower, bytes)`` tuples.
        target = self._header_name.encode("latin-1")
        raw_value: bytes | None = None
        for name, value in scope.get("headers", ()):
            if name == target:
                raw_value = value
                break

        if raw_value is None:
            logger.info("a2a auth rejected", extra={"reason": "missing_header"})
            return None

        try:
            raw_header = raw_value.decode("latin-1")
        except UnicodeDecodeError:
            logger.info("a2a auth rejected", extra={"reason": "header_decode"})
            return None

        if self._bearer_prefix_required:
            bearer = _parse_bearer_header(raw_header)
        else:
            stripped = raw_header.strip()
            bearer = stripped or None
        if not bearer:
            logger.info("a2a auth rejected", extra={"reason": "wrong_scheme"})
            return None

        try:
            raw = self._config.validate_token(bearer)
        except Exception:
            logger.exception("token validator raised on A2A request")
            return None

        if inspect.isawaitable(raw):
            # Should be unreachable — :func:`_assert_sync_validator` at
            # config time rejects async validators before any traffic
            # lands. This branch is the in-depth catch in case an
            # adopter swaps in an async validator at runtime via a
            # closure that conditionally awaits.
            logger.error(
                "a2a auth rejected: validator returned awaitable at request "
                "time. Async validators are not supported on the A2A leg; "
                "wrap with a sync bridge."
            )
            return None

        if raw is None:
            logger.info("a2a auth rejected", extra={"reason": "invalid_token"})
            return None
        return raw

    async def _send_unauthenticated(self, send: Any) -> None:
        body_obj = self._config.unauthenticated_response or {
            "error": "invalid_token",
            "error_description": "Bearer token missing or invalid",
        }
        body = json.dumps(body_obj).encode("utf-8")
        # RFC 6750 §3 + RFC 7235 §3.1 require ``WWW-Authenticate: Bearer``
        # on every 401. Without it, RFC-compliant clients (including
        # browsers and many HTTP libraries) won't surface the auth
        # challenge to the user — they treat the 401 as a generic
        # error. Always emit; even when the operator overrides
        # ``unauthenticated_response``, the header stays for protocol
        # compliance.
        await send(
            {
                "type": "http.response.start",
                "status": 401,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode("latin-1")),
                    (b"www-authenticate", b'Bearer realm="a2a", error="invalid_token"'),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})


@dataclass(frozen=True)
class _A2AAuthenticatedUser:
    """Minimal Starlette-BaseUser-shaped object for :class:`StarletteUser`.

    a2a-sdk's :class:`StarletteUser` adapter wants ``is_authenticated``
    (bool) and ``display_name`` (str). It doesn't import Starlette's
    :class:`BaseUser` directly — duck-typing works. We synthesize a
    frozen dataclass so the principal's identity flows through with no
    Starlette dependency on the auth side.
    """

    display_name: str
    tenant_id: str | None = None
    principal_metadata: dict[str, Any] | None = None

    @property
    def is_authenticated(self) -> bool:
        return True
