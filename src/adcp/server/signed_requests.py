"""Framework RFC 9421 request-signature verification, run before dispatch.

A seller that declares ``request_signing`` in ``get_adcp_capabilities`` opts
in by passing :class:`RequestSignatureVerification` to
:func:`adcp.server.serve` (``adcp.decisioning.serve`` builds one from the
platform's declared capabilities when given ``signer_keys=``). The framework
then, for every JSON-RPC POST on the MCP and A2A legs:

* rejects an unsigned request to an operation in ``required_for`` (or a
  JSON-RPC method in ``protocol_methods_required_for``) with ``401`` and
  ``WWW-Authenticate: Signature error="request_signature_required"``;
* verifies any presented signature against the key a
  :class:`~adcp.signing.SignerKeyResolver` maps the ``keyid`` to, rejecting a
  failure with the spec error code (``warn_for`` operations log and continue
  without identity instead, except for a malformed signature pair);
* on success, populates ``ToolContext.caller_identity`` with the signer's
  agent URL and ``metadata["adcp.auth_info"]`` with
  :meth:`AuthInfo.from_verified_signer <adcp.decisioning.AuthInfo.from_verified_signer>`,
  so :class:`~adcp.decisioning.BuyerAgentRegistry` dispatch resolves the
  buyer agent with no seller glue.

Verification runs at the ASGI edge because it needs the raw body bytes; the
result travels to dispatch on ``scope["state"]`` (which survives the stateful
MCP session task, like bearer auth's principal) and a ContextVar.

Sellers who already verify with :func:`adcp.signing.verify_starlette_request`
in their own ``asgi_middleware`` keep working: that helper records its result
on the request scope, and the framework reuses it instead of verifying again
— so the nonce is claimed exactly once.
"""

from __future__ import annotations

import contextvars
import copy
import json
import logging
import os
import time
import warnings
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from functools import partial
from typing import TYPE_CHECKING, Any, Literal, cast

import anyio
import anyio.from_thread
import anyio.to_thread
from starlette.requests import Request
from starlette.responses import JSONResponse

from adcp.server._size_limit import make_replay_receive
from adcp.signing.bindings import OriginBindingResolver
from adcp.signing.errors import (
    REQUEST_SIGNATURE_HEADER_MALFORMED,
    REQUEST_SIGNATURE_JWKS_UNAVAILABLE,
    REQUEST_SIGNATURE_REQUIRED,
    SignatureVerificationError,
)
from adcp.signing.middleware import VERIFIED_SIGNER_SCOPE_KEY, unauthorized_response_headers
from adcp.signing.replay import InMemoryReplayStore, ReplayStore
from adcp.signing.revocation import RevocationChecker
from adcp.signing.signer_keys import WbaSignerKeyResolver
from adcp.signing.verifier import (
    VerifiedSigner,
    VerifierCapability,
    VerifyOptions,
    verify_request_signature,
)
from adcp.signing.wba import (
    GENERIC_AUTHENTICATION_ERROR,
    OriginRevocationChecker,
    WbaRevokedKeyError,
    WbaUnavailableError,
    WbaVerificationError,
    accept_signature_value,
    check_wba_revocation,
    uses_web_bot_auth,
    verify_wba_request,
)

if TYPE_CHECKING:
    from adcp.decisioning.types import AdcpError
    from adcp.server.base import ToolContext
    from adcp.server.wba_authorization import WbaAuthorizer
    from adcp.signing.signer_keys import ResolvedSignerKey, SignerKeyResolver
    from adcp.signing.trust import TrustRecordEvaluator
    from adcp.signing.verifier import CoversDigestPolicy, SigningProfileVersion

logger = logging.getLogger(__name__)

#: Verified signer for the in-flight request. Carries on stateless MCP and
#: A2A, where dispatch shares the middleware's task; stateful MCP reads the
#: ``scope["state"]`` mirror instead.
current_verified_signer: contextvars.ContextVar[VerifiedSigner | None] = contextvars.ContextVar(
    "adcp_current_verified_signer", default=None
)
current_wba_authorization_error: contextvars.ContextVar[AdcpError | None] = contextvars.ContextVar(
    "adcp_wba_authorization_error", default=None
)
_WBA_AUTHORIZATION_ERROR_KEY = "adcp.server.wba_authorization_error"
_WBA_ERROR_PROJECTION_ONLY_KEY = "adcp.server.wba_error_projection_only"
_WBA_SPEND_COMMITTING_KEY = "adcp.wba_spend_committing"

#: Always allowed to an identity-established MCP signer, in addition to
#: ``unbound_for``: without them it cannot open a session to reach the
#: operations ``unbound_for`` allows. They invoke no tool.
_MCP_SESSION_METHODS = frozenset({"initialize", "notifications/initialized", "ping", "tools/list"})


current_wba_spend_committing: contextvars.ContextVar[bool | None] = contextvars.ContextVar(
    "adcp_wba_spend_committing", default=None
)
current_wba_error_projection_only: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "adcp_wba_error_projection_only", default=False
)


def is_wba_spend_committing(operation: str, params: Mapping[str, Any] | None = None) -> bool:
    """Classify spend-committing operations for the trust and revocation outage rules.

    Buys, acquisitions and ``activate_signal`` commit spend; so do unknown
    operations. ``update_media_buy`` and ``control_media_buy`` commit spend
    unless every change is a rename, a pause or a cancellation. Sellers that
    can compare a change with their own state (for example, a budget
    reduction) can supply their own ``spend_classifier``.
    """
    if operation in {
        "create_media_buy",
        "buy_products",
        "accept_proposal",
        "activate_signal",
    } or operation.startswith("acquire_"):
        return True
    if operation not in {"update_media_buy", "control_media_buy"}:
        from adcp.server.mcp_tools import ADCP_TOOL_DEFINITIONS

        protocol_reads = {
            "initialize",
            "tools/list",
            "ping",
            "tasks/get",
            "tasks/list",
            "tasks/cancel",
            "tasks/resubscribe",
            "GetTask",
            "ListTasks",
            "CancelTask",
            "SubscribeToTask",
            "notifications/initialized",
            "notifications/cancelled",
        }
        return operation not in protocol_reads and not any(
            tool["name"] == operation for tool in ADCP_TOOL_DEFINITIONS
        )
    if params is None:
        return True
    return not _is_known_nonspend_change(params)


# update_media_buy / control_media_buy change spend unless every change is one
# of these. Anything else (budgets, bids, impressions, flight dates, spend
# targets, new packages, resumes, unknown fields) counts as spend: fail closed.
_NONSPEND_BUY_FIELDS = frozenset(
    {
        "media_buy_id",
        "account",
        "idempotency_key",
        "context",
        "ext",
        "governance_context",
        "revision",
        "push_notification_config",
        "reporting_webhook",
        "adcp_major_version",
        "adcp_version",
        "name",
        "cancellation_reason",
    }
)
_NONSPEND_PACKAGE_FIELDS = frozenset(
    {"package_id", "buyer_ref", "context", "ext", "cancellation_reason"}
)


def _is_known_nonspend_change(params: Mapping[str, Any]) -> bool:
    """Only renames, pauses and cancellations are known not to commit spend."""

    def stops(change: Mapping[str, Any], allowed: frozenset[str]) -> bool:
        for key, value in change.items():
            if key in {"paused", "canceled"}:
                if value is not True:
                    return False
            elif key not in allowed:
                return False
        return True

    if not stops({k: v for k, v in params.items() if k != "packages"}, _NONSPEND_BUY_FIELDS):
        return False
    packages = params.get("packages")
    if packages is None:
        return True
    return isinstance(packages, list) and all(
        isinstance(package, Mapping) and stops(package, _NONSPEND_PACKAGE_FIELDS)
        for package in packages
    )


def check_wba_authorization(context: ToolContext | None) -> None:
    """Raise deferred identity authorization through the transport's AdCP error binding."""
    from adcp.decisioning.types import AdcpError

    error = context.metadata.get(_WBA_AUTHORIZATION_ERROR_KEY) if context is not None else None
    if isinstance(error, AdcpError):
        raise error


SignaturePosture = Literal["required", "warn", "supported"]

# Set only by SignedRequestVerificationMiddleware; bearer auth lets a request
# carrying it through without a token.
_FRAMEWORK_VERIFIED_SCOPE_KEY = "adcp.server.signature_verified"

# Set when a request lacks a usable signature but may proceed only if bearer
# auth succeeds; the value is the signature error code for the 401 challenge.
_SIGNATURE_FALLBACK_SCOPE_KEY = "adcp.server.signature_fallback"


def _accepts_signer_origin(resolver: Any) -> bool:
    """Whether ``resolver`` can be called with ``signer_origin=...``."""
    import inspect

    try:
        parameters = inspect.signature(resolver).parameters.values()
    except (TypeError, ValueError):
        return False
    return any(
        parameter.kind is inspect.Parameter.VAR_KEYWORD
        or (
            parameter.name == "signer_origin"
            and parameter.kind
            in {inspect.Parameter.KEYWORD_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD}
        )
        for parameter in parameters
    )


@dataclass(frozen=True, kw_only=True)
class RequestSignatureVerification:
    """Configuration for framework request-signature verification.

    Build from a declared capability block with :meth:`from_capability` so
    the enforced policy cannot drift from what ``get_adcp_capabilities``
    advertises::

        from adcp.server import RequestSignatureVerification, serve
        from adcp.signing import StaticSignerKeys

        serve(
            handler,
            request_signature_verification=RequestSignatureVerification.from_capability(
                capabilities.request_signing,
                signer_keys=StaticSignerKeys({"https://buyer.example": buyer_jwks}),
                replay_store=pg_replay_store,
            ),
        )

    :param signer_keys: Maps a ``keyid`` to the owning agent URL and JWK.
    :param required_for: AdCP operation names that reject unsigned requests.
    :param warn_for: Operation names in shadow mode — missing or failed
        signatures are logged, and the request continues without identity.
    :param supported_for: Operation names that verify when signed. Advisory;
        a presented signature on any operation outside ``warn_for`` is
        verified and must pass.
    :param protocol_methods_required_for: JSON-RPC ``method`` names (exact,
        case-sensitive, e.g. ``tasks/cancel``) that reject unsigned requests.
    :param protocol_methods_warn_for: JSON-RPC methods in shadow mode.
    :param covers_content_digest: Enforced digest-coverage policy. Declare
        ``"required"`` for AdCP 3.2.
    :param allow_bearer_fallback: Accept an unsigned request to a required
        operation when bearer auth (``serve(auth=...)``) authenticates it —
        the spec's "independently valid configured fallback authenticator".
        The bearer must then be presented and valid: discovery and
        network-trust bypasses do not apply, and a failure is a ``401``
        carrying both the ``Signature`` and ``Bearer`` challenges. Requires
        ``auth=``. Default ``False`` rejects every unsigned required request.
    :param replay_store: Nonce store shared by every request. The default is
        one per-process :class:`~adcp.signing.InMemoryReplayStore`, which
        does not protect a multi-replica deployment; wire a shared store
        (e.g. ``PgReplayStore``) there. ``None`` disables replay checks for
        the legacy profile and is rejected when Web Bot Auth is enabled.
    :param wba_revocation_checker: Origin revocation checker for the
        authorization layer, run after verification. When Web Bot Auth is
        enabled, defaults to ``OriginRevocationChecker`` using the configured
        signer keys. A revoked key is refused with 403 and no
        ``Accept-Signature``; an origin with no list stays
        identity-established; a stale list fails spend closed with
        ``SERVICE_UNAVAILABLE``. Never consulted on the signature path.
    :param trust_records: Operator membership evaluator used when authorizing
        an existing WBA binding. Defaults to ``TrustRecordEvaluator``. Its
        cache bounds binding freshness; it never participates in key discovery.
    :param wba_authorizer: The authorization seam for verified Web Bot Auth
        origins: an async ``(signer, operation, params) -> principal | None``
        (:class:`~adcp.server.wba_authorization.WbaAuthorizer`). Defaults to
        :class:`~adcp.server.wba_authorization.OriginBindingGate` built from
        ``origin_bindings``, ``trust_records``, ``binding_role`` and
        ``spend_classifier``. Supply your own to keep binding and trust
        evaluation in your application.
    :param unbound_for: Operations an identity-established caller may call
        with its verified identity attached. Any other operation is served
        exactly as an anonymous request (no verified signer), so the caller
        gets what an anonymous caller gets; an operation in ``required_for``
        or ``protocol_methods_required_for`` is refused with
        ``PERMISSION_DENIED``. ``get_principal`` and, on MCP, the session
        methods (``initialize``, ``notifications/initialized``, ``ping``,
        ``tools/list``) are always allowed. Add ``sync_principal`` only when
        its handler implements an open policy.
    :param wba_discovery_failure: Local policy for a Web Bot Auth request
        whose signer's key directory cannot be resolved (WBA §6.10) and no
        usable cached copy holds the key. ``"reject"`` (default) answers 403
        with ``Accept-Signature``; ``"anonymous"`` serves it exactly as an
        unsigned request, except operations in ``required_for`` /
        ``protocol_methods_required_for``, which are still refused.
    :param authorities: The ``host[:port]`` values this endpoint serves. A Web
        Bot Auth request whose ``@authority`` is not among them is refused
        before key discovery, so a signature captured at another seller
        cannot be replayed here under that seller's ``Host``. Strongly
        recommended for any endpoint reachable under more than one name.
    :param rfc9421_profile: Whether this verifier also advertises the RFC 9421
        profile. With Web Bot Auth enabled, an unsigned request to a required
        operation is answered as the RFC 9421 profile does (401
        ``request_signature_required``) plus ``Accept-Signature`` for Web Bot
        Auth. Set ``False`` when only ``web-bot-auth`` is advertised: such a
        request, and any RFC 9421 signature, gets 403 with ``Accept-Signature``.
    """

    signer_keys: SignerKeyResolver
    required_for: frozenset[str] = frozenset()
    warn_for: frozenset[str] = frozenset()
    supported_for: frozenset[str] = frozenset()
    protocol_methods_required_for: frozenset[str] = frozenset()
    protocol_methods_warn_for: frozenset[str] = frozenset()
    covers_content_digest: CoversDigestPolicy = "either"
    allow_bearer_fallback: bool = False
    replay_store: ReplayStore | None = field(default_factory=InMemoryReplayStore)
    revocation_checker: RevocationChecker | None = None
    signing_profile_version: SigningProfileVersion = "3.1"
    enable_web_bot_auth: bool = False
    origin_bindings: OriginBindingResolver | None = None
    wba_revocation_checker: Any = None
    trust_records: TrustRecordEvaluator | None = None
    binding_role: str = "adcp:buying"
    unbound_for: frozenset[str] = frozenset({"get_adcp_capabilities", "get_principal"})
    spend_classifier: Callable[[str, Mapping[str, Any] | None], bool] = is_wba_spend_committing
    wba_authorizer: WbaAuthorizer | None = None
    _default_wba_authorizer: WbaAuthorizer | None = field(
        default=None, init=False, repr=False, compare=False
    )
    wba_discovery_failure: Literal["reject", "anonymous"] = "reject"
    authorities: frozenset[str] | None = None
    rfc9421_profile: bool = True

    def __post_init__(self) -> None:
        if self.wba_discovery_failure not in {"reject", "anonymous"}:
            raise ValueError("wba_discovery_failure must be 'reject' or 'anonymous'")
        if not self.rfc9421_profile and not self.enable_web_bot_auth:
            raise ValueError("a verifier needs at least one signing profile")
        if self.enable_web_bot_auth and self.replay_store is None:
            raise ValueError("Web Bot Auth requires a replay store")
        if self.enable_web_bot_auth and not _accepts_signer_origin(self.signer_keys):
            raise TypeError(
                "Web Bot Auth requires signer_keys that resolve by origin: "
                "its __call__ must accept the keyword argument signer_origin "
                "(use DiscoveredSignerKeys)"
            )
        if self.enable_web_bot_auth and self.wba_revocation_checker is None:
            object.__setattr__(
                self,
                "wba_revocation_checker",
                OriginRevocationChecker(cast(WbaSignerKeyResolver, self.signer_keys)),
            )
        if self.enable_web_bot_auth and self.trust_records is None:
            from adcp.signing.trust import TrustRecordEvaluator

            object.__setattr__(self, "trust_records", TrustRecordEvaluator())
        if self.enable_web_bot_auth and self.wba_authorizer is None:
            from adcp.server.wba_authorization import OriginBindingGate

            # Derived, not stored in wba_authorizer, so dataclasses.replace
            # with new bindings or trust records rebuilds it.
            object.__setattr__(
                self,
                "_default_wba_authorizer",
                OriginBindingGate(
                    self.origin_bindings,
                    self.trust_records,
                    role=self.binding_role,
                    spend_classifier=self.spend_classifier,
                ),
            )
        from adcp.signing._profile_deprecation import rfc9421_deprecation_active

        if not self.enable_web_bot_auth and rfc9421_deprecation_active():
            warnings.warn(
                "RFC 9421-profile verification is deprecated in AdCP 3.3 and remains "
                "supported through 3.x; enable_web_bot_auth opts into the experimental profile.",
                DeprecationWarning,
                stacklevel=2,
            )
        overlap = self.required_for & self.warn_for
        if overlap:
            raise ValueError(
                f"operations {sorted(overlap)} appear in both required_for and warn_for"
            )
        method_overlap = self.protocol_methods_required_for & self.protocol_methods_warn_for
        if method_overlap:
            raise ValueError(
                f"methods {sorted(method_overlap)} appear in both "
                "protocol_methods_required_for and protocol_methods_warn_for"
            )
        if "tools/call" in (self.protocol_methods_required_for | self.protocol_methods_warn_for):
            raise ValueError(
                "'tools/call' cannot be listed as a protocol method; list the AdCP "
                "operation (its params.name) in required_for / warn_for instead"
            )

    @property
    def effective_wba_authorizer(self) -> WbaAuthorizer | None:
        """The configured :class:`WbaAuthorizer`, or the default binding gate."""
        return self.wba_authorizer or self._default_wba_authorizer

    @classmethod
    def from_capability(
        cls,
        capability: Any,
        *,
        signer_keys: SignerKeyResolver,
        **overrides: Any,
    ) -> RequestSignatureVerification:
        """Build from a ``request_signing`` capability block.

        Accepts the generated ``RequestSigning`` model or its dict form.
        ``overrides`` set the fields the capability does not carry
        (``replay_store``, ``revocation_checker``, ``signing_profile_version``,
        ``allow_bearer_fallback``).
        """
        if hasattr(capability, "model_dump"):
            capability = capability.model_dump(mode="json", exclude_none=True)
        if not isinstance(capability, Mapping):
            raise TypeError(
                "capability must be a RequestSigning model or mapping, got "
                f"{type(capability).__name__}"
            )

        def names(key: str) -> frozenset[str]:
            return frozenset(str(item) for item in capability.get(key) or ())

        kwargs: dict[str, Any] = {
            "required_for": names("required_for"),
            "warn_for": names("warn_for"),
            "supported_for": names("supported_for"),
            "protocol_methods_required_for": names("protocol_methods_required_for"),
            "protocol_methods_warn_for": names("protocol_methods_warn_for"),
        }
        digest = capability.get("covers_content_digest")
        if digest is not None:
            kwargs["covers_content_digest"] = digest
        profiles = capability.get("profiles")
        if isinstance(profiles, (list, tuple)) and "web-bot-auth" in profiles:
            # The experimental profile list says which profiles are offered.
            kwargs["enable_web_bot_auth"] = True
            kwargs["rfc9421_profile"] = "adcp/request-signing/v1" in profiles
        kwargs.update(overrides)
        return cls(signer_keys=signer_keys, **kwargs)

    def posture_for(
        self, method: str | None, operation: str | None, *, opaque: bool = False
    ) -> SignaturePosture | None:
        """Signing posture for a JSON-RPC ``method`` / AdCP ``operation``.

        Precedence is ``required`` > ``warn`` > ``supported`` across both
        namespaces. An ``opaque`` request — batch, unparseable body, or an A2A
        message whose skill cannot be determined — is ``required`` whenever
        anything is required, so a mutation cannot slip past enforcement
        inside an envelope the verifier cannot read.
        """
        if opaque:
            if self.required_for or self.protocol_methods_required_for:
                return "required"
            return None
        if operation in self.required_for or method in self.protocol_methods_required_for:
            return "required"
        if operation in self.warn_for or method in self.protocol_methods_warn_for:
            return "warn"
        if operation in self.supported_for:
            return "supported"
        return None


_A2A_MESSAGE_METHODS = frozenset(
    {"message/send", "message/stream", "SendMessage", "SendStreamingMessage"}
)


@dataclass(frozen=True)
class _Target:
    method: str | None = None
    operation: str | None = None
    opaque: bool = False
    a2a_parsed: Any = None


_OPAQUE = _Target(opaque=True)


def _jsonrpc_target(
    body: bytes, transport: str, message_parser: Any, *, parse_skill: bool
) -> _Target:
    """Classify a request body by JSON-RPC ``method`` and AdCP operation.

    The operation is ``params.name`` for MCP ``tools/call`` and the parsed
    skill for A2A message sends (parsed only when ``parse_skill``; the
    operation namespace matters only if something in it is required or
    warned). Anything the verifier cannot read is opaque.
    """
    try:
        payload = json.loads(body) if body else None
    except ValueError:
        return _OPAQUE
    if not isinstance(payload, dict):
        return _OPAQUE
    method = payload.get("method")
    if not isinstance(method, str):
        if "method" not in payload and ("result" in payload or "error" in payload):
            # A JSON-RPC response (e.g. an MCP elicitation reply) invokes nothing.
            return _Target()
        return _OPAQUE
    if transport == "mcp":
        if method != "tools/call":
            return _Target(method=method)
        params = payload.get("params")
        name = params.get("name") if isinstance(params, dict) else None
        return _Target(method=method, operation=name) if isinstance(name, str) else _OPAQUE

    if method not in _A2A_MESSAGE_METHODS or not parse_skill:
        return _Target(method=method)

    from adcp.server.a2a_server import parse_a2a_jsonrpc_skill

    try:
        parsed = parse_a2a_jsonrpc_skill(payload, message_parser)
    except Exception:
        # Fail closed: dispatch has recovery paths that can still find an
        # operation in a body the parser rejected. Log coarsely — parser
        # errors can embed payload text.
        logger.info("a2a signed-request parse rejected", extra={"reason": "invalid_message"})
        return _OPAQUE
    skill, _params = parsed
    if not isinstance(skill, str) or not skill:
        return _OPAQUE
    return _Target(method=method, operation=skill, a2a_parsed=parsed)


def _header(scope: Mapping[str, Any], name: bytes) -> str | None:
    for raw_name, raw_value in scope.get("headers", ()):
        if raw_name.lower() == name:
            return str(raw_value.decode("latin-1"))
    return None


def _received_target_uri(scope: Mapping[str, Any]) -> str:
    """The request's target URI as received (RFC 9421 §2.2.2).

    The scheme, the ``Host`` field and the request-target bytes as they
    arrived (``raw_path`` plus the query string), so percent-encoding is
    preserved. Starlette's ``request.url`` decodes the path and must not be
    used for ``@target-uri``.
    """
    scheme = str(scope.get("scheme") or "http")
    host = _header(scope, b"host")
    if host is None:
        server = scope.get("server")
        if isinstance(server, (tuple, list)) and len(server) == 2:
            name = f"[{server[0]}]" if ":" in str(server[0]) else str(server[0])
            host = (
                name
                if (scheme, server[1]) in {("https", 443), ("http", 80)}
                else (f"{name}:{server[1]}")
            )
        else:
            host = ""
    raw_path = scope.get("raw_path")
    if isinstance(raw_path, bytes) and raw_path:
        path = raw_path.decode("latin-1")
    else:
        # Without raw_path the decoded path is the best available; a target
        # that was percent-encoded then fails verification (fails closed).
        path = str(scope.get("path", ""))
    if "?" in path:
        # Some servers keep the query in raw_path; never append it twice.
        path = path.split("?", 1)[0]
    query = scope.get("query_string") or b""
    target = f"{scheme}://{host}{path}"
    if query:
        target += "?" + (query.decode("latin-1") if isinstance(query, bytes) else str(query))
    return target


class SignedRequestVerificationMiddleware:
    """Pure-ASGI middleware enforcing :class:`RequestSignatureVerification`.

    Under Web Bot Auth it runs the two layers in order. The signature path
    (:func:`~adcp.signing.verify_wba_request`, key directory and replay store
    only) answers failures with Web Bot Auth statuses and headers and the
    generic authentication error. The authorization layer (revocation, then
    the binding re-evaluated against the operator's trust record) answers
    with the standard AdCP error envelope, never 403 with
    ``Accept-Signature``. Infrastructure outages are application-layer
    ``SERVICE_UNAVAILABLE``.
    """

    def __init__(
        self,
        app: Any,
        config: RequestSignatureVerification,
        *,
        transport: Literal["mcp", "a2a"],
        message_parser: Any = None,
        bearer_configured: bool = False,
        defer_authorization_errors: bool = False,
    ) -> None:
        self._app = app
        self._config = config
        self._transport = transport
        self._message_parser = message_parser
        self._parse_skill = bool(
            config.required_for or config.warn_for or config.enable_web_bot_auth
        )
        self._bearer_configured = bearer_configured
        self._defer_authorization_errors = defer_authorization_errors

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        # Only JSON-RPC POSTs carry an operation; GET (SSE, agent card),
        # DELETE (session end), and OPTIONS (CORS preflight) pass through.
        if scope.get("type") != "http" or scope.get("method") != "POST":
            await self._app(scope, receive, send)
            return

        existing = scope.get(VERIFIED_SIGNER_SCOPE_KEY)
        if isinstance(existing, VerifiedSigner):
            # A seller's own verify_starlette_request already claimed the
            # nonce, so re-verifying would reject the request as replayed.
            # Its identity is trusted only if the seller named the agent;
            # the framework's resolver never vouches for a key it did not
            # check.
            if existing.agent_url is not None:
                from adcp.signing.canonical import canonical_origin

                origin = canonical_origin(existing.agent_url)
                if existing.profile == "web-bot-auth":
                    if not self._config.enable_web_bot_auth:
                        await self._reject_wba(
                            scope,
                            receive,
                            send,
                            WbaVerificationError("Web Bot Auth is not enabled", challenge=False),
                        )
                        return
                    body = json.dumps(existing.parsed_body).encode()
                    target = _jsonrpc_target(
                        body, self._transport, self._message_parser, parse_skill=True
                    )
                    await self._serve_verified_wba(scope, receive, send, (existing,), body, target)
                    return
                principal = (
                    await self._config.origin_bindings(origin)
                    if self._config.origin_bindings is not None
                    else None
                )
                if principal is not None:
                    from adcp.decisioning.types import AdcpError

                    await self._call_with_signer(
                        scope,
                        receive,
                        send,
                        replace(existing, principal_id=None),
                        authorization_error=AdcpError(
                            "PERMISSION_DENIED",
                            message="Bound origin requires Web Bot Auth",
                            recovery="correctable",
                        ),
                    )
                    return
                existing = replace(existing, principal_id=principal)
                await self._call_with_signer(scope, receive, send, existing)
            else:
                await self._app(scope, receive, send)
            return

        chunks: list[bytes] = []
        more_body = True
        while more_body:
            message = await receive()
            if message.get("type") == "http.disconnect":
                return
            if message.get("type") != "http.request":
                continue
            chunks.append(message.get("body", b""))
            more_body = bool(message.get("more_body", False))
        body = b"".join(chunks)
        replay = make_replay_receive(chunks, receive=receive)

        target = _jsonrpc_target(
            body, self._transport, self._message_parser, parse_skill=self._parse_skill
        )
        if target.a2a_parsed is not None:
            from adcp.server.a2a_server import _A2A_PARSED_REQUEST_SCOPE_KEY

            scope[_A2A_PARSED_REQUEST_SCOPE_KEY] = target.a2a_parsed
        posture = self._config.posture_for(target.method, target.operation, opaque=target.opaque)
        label = target.operation or target.method or ""

        signature_input = _header(scope, b"signature-input")
        signature = _header(scope, b"signature")
        wba = uses_web_bot_auth(dict(Request(scope).headers))
        if signature_input is None and signature is None and not wba:
            if posture == "required":
                if self._config.allow_bearer_fallback and self._bearer_configured:
                    scope[_SIGNATURE_FALLBACK_SCOPE_KEY] = REQUEST_SIGNATURE_REQUIRED
                    await self._app(scope, replay, send)
                    return
                if self._config.enable_web_bot_auth and not self._config.rfc9421_profile:
                    # Only web-bot-auth is advertised: the WBA challenge.
                    await self._reject_wba(
                        scope,
                        replay,
                        send,
                        WbaVerificationError("operation requires a signature"),
                    )
                    return
                await self._reject(
                    send,
                    SignatureVerificationError(
                        REQUEST_SIGNATURE_REQUIRED,
                        step=0,
                        message=f"operation {label!r} requires a signature",
                    ),
                    accept_signature=self._config.enable_web_bot_auth,
                )
                return
            if posture == "warn":
                logger.warning(
                    "unsigned request to warn_for target",
                    extra={"adcp_operation": label, "reason": REQUEST_SIGNATURE_REQUIRED},
                )
            await self._app(scope, replay, send)
            return

        if wba:
            await self._serve_wba(scope, replay, send, body, target, posture)
            return
        if not self._config.rfc9421_profile:
            # The RFC 9421 profile is not offered here: ask for Web Bot Auth.
            await self._reject_wba(
                scope,
                replay,
                send,
                WbaVerificationError("only the Web Bot Auth profile is accepted"),
            )
            return

        try:
            signer, authorization_error = await self._verify(scope, body, label, posture)
        except SignatureVerificationError as exc:
            if posture == "warn" and exc.code != REQUEST_SIGNATURE_HEADER_MALFORMED:
                logger.warning(
                    "request signature failed on warn_for target",
                    extra={"adcp_operation": label, "reason": exc.code, "step": exc.step},
                )
                await self._app(scope, replay, send)
                return
            await self._reject(send, exc)
            return
        await self._call_with_signer(
            scope,
            replay,
            send,
            signer,
            authorization_error=authorization_error,
        )

    async def _serve_wba(
        self,
        scope: Any,
        receive: Any,
        send: Any,
        body: bytes,
        target: _Target,
        posture: SignaturePosture | None,
    ) -> None:
        """Verify a request selected for Web Bot Auth, then authorize it."""
        config = self._config
        if not config.enable_web_bot_auth:
            # A profile this endpoint does not accept: no challenge to sign
            # with it, and never a fallback to the RFC 9421 profile.
            await self._reject_wba(
                scope,
                receive,
                send,
                WbaVerificationError("Web Bot Auth is not enabled", challenge=False),
            )
            return
        assert config.replay_store is not None
        request = Request(scope)
        try:
            signers = await verify_wba_request(
                method=request.method,
                url=_received_target_uri(scope),
                headers=dict(request.headers),
                body=body,
                signer_keys=cast(WbaSignerKeyResolver, config.signer_keys),
                replay_store=config.replay_store,
                raw_headers=scope.get("headers"),
                authorities=config.authorities,
            )
        except WbaUnavailableError as exc:
            logger.warning("web bot auth verifier outage", extra={"reason": str(exc)})
            await self._project_wba_outage(scope, receive, send, body)
            return
        except WbaVerificationError as exc:
            if (
                exc.discovery_failure
                and config.wba_discovery_failure == "anonymous"
                and posture != "required"
            ):
                # Unverified (WBA §6.10): local policy serves it as an
                # anonymous request, with no verified signer.
                logger.info(
                    "web bot auth key discovery failed; serving as anonymous",
                    extra={"adcp_operation": target.operation or target.method or ""},
                )
                await self._app(scope, receive, send)
                return
            await self._reject_wba(scope, receive, send, exc)
            return
        await self._serve_verified_wba(scope, receive, send, signers, body, target)

    async def _serve_verified_wba(
        self,
        scope: Any,
        receive: Any,
        send: Any,
        signers: tuple[VerifiedSigner, ...],
        body: bytes,
        target: _Target,
    ) -> None:
        """The authorization layer for verified Web Bot Auth signers."""
        from adcp.decisioning.types import AdcpError

        config = self._config
        posture = config.posture_for(target.method, target.operation, opaque=target.opaque)
        operation = target.operation or target.method or ""
        if config.wba_revocation_checker is not None:
            try:
                signers = await check_wba_revocation(config.wba_revocation_checker, signers)
            except WbaRevokedKeyError as exc:
                await self._reject_wba(scope, receive, send, exc)
                return
        # The profile verifies exactly one signer per request.
        (signer,) = signers
        principal, authorization_error = await self._authorize_wba_binding(signer, operation)
        if authorization_error is not None:
            await self._call_with_signer(
                scope,
                receive,
                send,
                replace(signer, principal_id=None),
                authorization_error=authorization_error,
            )
            return
        if principal is None and operation not in self._unbound_operations():
            if posture == "required":
                # An anonymous caller would be refused too; the identity is
                # established, so the refusal is the AdCP envelope.
                await self._call_with_signer(
                    scope,
                    receive,
                    send,
                    replace(signer, principal_id=None),
                    authorization_error=AdcpError(
                        "PERMISSION_DENIED",
                        message="Caller is not authorized for this operation",
                        recovery="correctable",
                    ),
                )
                return
            # Outside what an identity-established caller may do: it gets
            # exactly what an anonymous caller gets, with no verified signer.
            logger.info(
                "web bot auth signer is identity-established; serving as anonymous",
                extra={"adcp_operation": operation},
            )
            # Drop a trusted edge's handshake too: nothing downstream may
            # attribute this request to the signer.
            scope.pop(VERIFIED_SIGNER_SCOPE_KEY, None)
            await self._app(scope, receive, send)
            return
        await self._call_with_signer(scope, receive, send, replace(signer, principal_id=principal))

    def _unbound_operations(self) -> frozenset[str]:
        allowed = self._config.unbound_for | {"get_principal"}
        if self._transport == "mcp":
            allowed |= _MCP_SESSION_METHODS
        return allowed

    async def _verify(
        self,
        scope: Any,
        body: bytes,
        label: str,
        posture: SignaturePosture | None,
    ) -> tuple[VerifiedSigner, AdcpError | None]:
        """Verify an RFC 9421 signature (the deprecated profile)."""
        resolved: dict[str, ResolvedSignerKey] = {}

        # The verifier looks the key up at its own checklist step 7, after
        # the cheap header, parameter, and window checks, so attacker-chosen
        # keyids never reach the resolver on a malformed request. It runs in
        # a worker thread (replay stores may do blocking I/O), so the async
        # resolver is bridged back onto the event loop.
        def jwks_resolver(keyid: str) -> dict[str, Any] | None:
            key = anyio.from_thread.run(self._resolve, keyid)
            if key is None:
                return None
            resolved[keyid] = key
            return dict(key.jwk)

        config = self._config
        request = Request(scope)
        options = VerifyOptions(
            now=time.time(),
            capability=VerifierCapability(
                covers_content_digest=config.covers_content_digest,
                required_for=config.required_for,
                supported_for=config.supported_for,
            ),
            operation=label,
            jwks_resolver=jwks_resolver,
            replay_store=config.replay_store,
            revocation_checker=config.revocation_checker,
            signing_profile_version=config.signing_profile_version,
            posture=posture,
        )
        signer = await anyio.to_thread.run_sync(
            partial(
                verify_request_signature,
                method=request.method,
                url=str(request.url),
                headers=dict(request.headers),
                body=body,
                options=options,
                raw_headers=scope.get("headers"),
            )
        )
        key = resolved.get(signer.key_id)
        if key is not None and config.origin_bindings is not None:
            from adcp.signing.canonical import canonical_origin

            if await config.origin_bindings(canonical_origin(key.agent_url)) is not None:
                from adcp.decisioning.types import AdcpError

                return replace(signer, agent_url=key.agent_url), AdcpError(
                    "PERMISSION_DENIED",
                    message="Bound origin requires Web Bot Auth",
                    recovery="correctable",
                )
        return replace(signer, agent_url=key.agent_url if key is not None else None), None

    async def _authorize_wba_binding(
        self, signer: VerifiedSigner, operation: str
    ) -> tuple[str | None, AdcpError | None]:
        """Ask the configured :class:`WbaAuthorizer` for the signer's principal."""
        from adcp.decisioning.types import AdcpError

        authorizer = self._config.effective_wba_authorizer
        if authorizer is None:
            return None, None
        try:
            return await authorizer(signer, operation, self._operation_params(signer)), None
        except AdcpError as error:
            return None, error
        except Exception:
            logger.exception("Web Bot Auth authorizer failed")
            return None, AdcpError(
                "PERMISSION_DENIED",
                message="Signer origin is not authorized",
                recovery="correctable",
            )

    def _operation_params(self, signer: VerifiedSigner) -> Mapping[str, Any] | None:
        """The operation's arguments from the verified body; ``None`` if unreadable."""
        payload = signer.parsed_body
        if not isinstance(payload, Mapping):
            return {}
        if self._transport == "mcp":
            envelope = payload.get("params")
            arguments = envelope.get("arguments") if isinstance(envelope, Mapping) else None
            return arguments if isinstance(arguments, Mapping) else {}
        from adcp.server.a2a_server import parse_a2a_jsonrpc_skill

        try:
            _skill, arguments = parse_a2a_jsonrpc_skill(dict(payload), self._message_parser)
        except Exception:
            # Unreadable arguments classify as spend (fail closed).
            return None
        return arguments if isinstance(arguments, Mapping) else {}

    def _is_spend_committing(self, signer: VerifiedSigner, operation: str) -> bool:
        params = self._operation_params(signer)
        return self._config.spend_classifier(operation, params if params is not None else None)

    @staticmethod
    async def _reject_wba(scope: Any, receive: Any, send: Any, exc: WbaVerificationError) -> None:
        """A signature-path failure: the WBA status and headers, a generic body."""
        logger.info(
            "web bot auth request rejected",
            extra={"status": exc.status, "reason": exc.reason, "label": exc.label},
        )
        response = JSONResponse(
            GENERIC_AUTHENTICATION_ERROR, status_code=exc.status, headers=exc.headers
        )
        await response(scope, receive, send)

    async def _project_wba_outage(self, scope: Any, receive: Any, send: Any, body: bytes) -> None:
        """Answer a verifier infrastructure outage at the application layer.

        No identity is established and no handler runs. A known AdCP skill
        receives the retryable ``SERVICE_UNAVAILABLE`` through the transport's
        error binding; anything else receives the AdCP error envelope (503).
        """
        from starlette.authentication import UnauthenticatedUser

        from adcp.server.mcp_tools import ADCP_TOOL_DEFINITIONS

        error = _service_unavailable("Signer identity is temporarily unavailable")
        target = _jsonrpc_target(body, self._transport, self._message_parser, parse_skill=True)
        if not self._defer_authorization_errors or not any(
            tool["name"] == target.operation for tool in ADCP_TOOL_DEFINITIONS
        ):
            await _authorization_error_response(error)(scope, receive, send)
            return
        if target.a2a_parsed is not None:
            from adcp.server.a2a_server import _A2A_PARSED_REQUEST_SCOPE_KEY

            scope[_A2A_PARSED_REQUEST_SCOPE_KEY] = target.a2a_parsed
        scope.pop(VERIFIED_SIGNER_SCOPE_KEY, None)
        scope.pop(_FRAMEWORK_VERIFIED_SCOPE_KEY, None)
        state = scope.setdefault("state", {})
        state.pop(VERIFIED_SIGNER_SCOPE_KEY, None)
        state[_WBA_AUTHORIZATION_ERROR_KEY] = error
        state[_WBA_SPEND_COMMITTING_KEY] = None
        scope[_WBA_ERROR_PROJECTION_ONLY_KEY] = True
        scope["user"] = UnauthenticatedUser()
        scope.pop("auth", None)
        signer_token = current_verified_signer.set(None)
        error_token = current_wba_authorization_error.set(error)
        spend_token = current_wba_spend_committing.set(None)
        projection_token = current_wba_error_projection_only.set(True)
        try:
            await self._app(scope, receive, send)
        finally:
            current_verified_signer.reset(signer_token)
            current_wba_authorization_error.reset(error_token)
            current_wba_spend_committing.reset(spend_token)
            current_wba_error_projection_only.reset(projection_token)

    async def _resolve(self, keyid: str) -> ResolvedSignerKey | None:
        try:
            return await self._config.signer_keys(keyid)
        except SignatureVerificationError:
            raise
        except Exception as exc:
            # A resolver bug or outage must not become a 500 that leaks
            # internals; it is a key-fetch failure from the buyer's view.
            logger.exception("signer key resolver raised")
            raise SignatureVerificationError(
                REQUEST_SIGNATURE_JWKS_UNAVAILABLE,
                step=7,
                message="signer key resolution failed",
            ) from exc

    async def _call_with_signer(
        self,
        scope: Any,
        receive: Any,
        send: Any,
        signer: VerifiedSigner,
        *,
        authorization_error: AdcpError | None = None,
    ) -> None:
        if authorization_error is not None and not self._defer_authorization_errors:
            await _authorization_error_response(authorization_error)(scope, receive, send)
            return
        target = _jsonrpc_target(
            json.dumps(signer.parsed_body).encode(),
            self._transport,
            self._message_parser,
            parse_skill=True,
        )
        raw_method = (
            signer.parsed_body.get("method") if isinstance(signer.parsed_body, Mapping) else None
        )
        skill_dispatch = target.operation is not None or (
            raw_method == "tools/call"
            if self._transport == "mcp"
            else isinstance(raw_method, str) and raw_method in _A2A_MESSAGE_METHODS
        )
        if authorization_error is not None and not skill_dispatch:
            # Protocol methods do not enter the AdCP skill error binding.
            await _authorization_error_response(authorization_error)(scope, receive, send)
            return
        scope[VERIFIED_SIGNER_SCOPE_KEY] = signer
        scope[_FRAMEWORK_VERIFIED_SCOPE_KEY] = True
        scope.setdefault("state", {})[VERIFIED_SIGNER_SCOPE_KEY] = signer
        scope["state"][_WBA_AUTHORIZATION_ERROR_KEY] = authorization_error
        spend_committing = None
        if signer.profile == "web-bot-auth":
            spend_committing = self._is_spend_committing(
                signer, target.operation or target.method or ""
            )
        scope["state"][_WBA_SPEND_COMMITTING_KEY] = spend_committing
        if (
            self._transport == "a2a"
            and signer.agent_url is not None
            and (signer.profile != "web-bot-auth" or signer.principal_id is not None)
            and not getattr(scope.get("user"), "is_authenticated", False)
        ):
            # a2a-sdk scopes task ownership by the authenticated user. Without
            # this every signed buyer shares one owner and can read or cancel
            # another buyer's tasks. Bearer auth (inner) overrides it when a
            # token is also presented.
            from adcp.server.auth import _A2AAuthenticatedUser

            scope["user"] = _A2AAuthenticatedUser(display_name=signer.agent_url)
        token = current_verified_signer.set(signer)
        error_token = current_wba_authorization_error.set(authorization_error)
        spend_token = current_wba_spend_committing.set(spend_committing)
        try:
            await self._app(scope, receive, send)
        finally:
            current_verified_signer.reset(token)
            current_wba_authorization_error.reset(error_token)
            current_wba_spend_committing.reset(spend_token)

    @staticmethod
    async def _reject(
        send: Any, exc: SignatureVerificationError, *, accept_signature: bool = False
    ) -> None:
        logger.info(
            "request signature rejected",
            extra={"reason": exc.code, "step": exc.step},
        )
        headers = dict(unauthorized_response_headers(exc))
        if accept_signature:
            # A verifier advertising both profiles also tells a Web Bot Auth
            # client how to sign (security.mdx, Coexistence).
            headers["Accept-Signature"] = accept_signature_value()
        response = JSONResponse(
            {"error": exc.code},
            status_code=401,
            headers=headers,
        )

        async def no_receive() -> dict[str, Any]:
            return {"type": "http.disconnect"}

        await response({"type": "http"}, no_receive, send)


def _service_unavailable(message: str) -> AdcpError:
    from adcp.decisioning.types import AdcpError

    return AdcpError("SERVICE_UNAVAILABLE", message=message, recovery="transient")


def _authorization_error_response(error: AdcpError) -> JSONResponse:
    """An authorization outcome in the AdCP error envelope, never a WBA challenge."""
    unavailable = error.code == "SERVICE_UNAVAILABLE"
    return JSONResponse(
        {"errors": [error.to_wire()]},
        status_code=503 if unavailable else 403,
        headers={"Retry-After": "30"} if unavailable else None,
    )


def verified_signer_for_request(request_context: Any) -> VerifiedSigner | None:
    """Return the framework-verified signer for a dispatching request, if any.

    When the originating HTTP request is available its scope is the only
    source consulted. The ContextVar is a fallback for callers without one:
    a long-lived task (such as a stateful MCP session) can carry the context
    of the request that created it, so trusting the ContextVar there could
    attribute that request's signer to later requests.

    Only framework verification writes ``scope["state"]``; the top-level
    scope key a seller's own ``verify_starlette_request`` sets is a reuse
    handshake and does not change identity for sellers who haven't opted in.
    """
    scope = getattr(request_context, "scope", None)
    if isinstance(scope, Mapping):
        state = scope.get("state")
        signer = state.get(VERIFIED_SIGNER_SCOPE_KEY) if isinstance(state, Mapping) else None
        return signer if isinstance(signer, VerifiedSigner) else None
    return current_verified_signer.get()


def signature_fallback_code(scope: Mapping[str, Any]) -> str | None:
    """Signature error code when bearer auth must succeed for this request.

    Set by framework verification for an unsigned request to a required
    operation admitted under ``allow_bearer_fallback``. Bearer middleware
    then disables its discovery and network-trust bypasses and answers its
    ``401`` with the ``Signature`` challenge in place of the ``Bearer`` one.
    ``warn_for`` shadow mode never
    sets it: shadow mode observes and leaves authentication to the
    configured ``auth=`` as-is.
    """
    code = scope.get(_SIGNATURE_FALLBACK_SCOPE_KEY)
    return code if isinstance(code, str) else None


def scope_has_verified_signer(scope: Mapping[str, Any]) -> bool:
    """True when framework verification authenticated this request's signer.

    Only the framework middleware sets this, so a seller that runs
    ``verify_starlette_request`` itself without opting in keeps its bearer
    requirement unchanged.
    """
    return scope.get(_FRAMEWORK_VERIFIED_SCOPE_KEY) is True


def scope_has_wba_error_projection(scope: Mapping[str, Any]) -> bool:
    """A known AdCP skill admitted only to render a framework-owned denial."""
    from adcp.decisioning.types import AdcpError

    state = scope.get("state")
    return (
        scope.get(_WBA_ERROR_PROJECTION_ONLY_KEY) is True
        and isinstance(state, Mapping)
        and isinstance(state.get(_WBA_AUTHORIZATION_ERROR_KEY), AdcpError)
    )


def apply_verified_signer(
    context: ToolContext | None,
    request_context: Any,
) -> ToolContext | None:
    """Overlay the verified signer's identity onto a dispatch ``ToolContext``.

    Returns a copy with ``metadata["adcp.auth_info"]`` set to an ``http_sig``
    :class:`AuthInfo` and ``caller_identity`` set to the signer's agent URL,
    so both identity surfaces name the same principal (a bearer principal
    presented alongside the signature is superseded). A context whose factory
    already supplied a typed credential is returned unchanged — the factory
    owns identity there. Also unchanged when the request carried no
    framework-verified signature.
    """
    from adcp.decisioning.context import AuthInfo
    from adcp.server.base import ToolContext

    request_scope = getattr(request_context, "scope", None)
    if isinstance(request_scope, Mapping):
        state = request_scope.get("state")
        authorization_error = (
            state.get(_WBA_AUTHORIZATION_ERROR_KEY) if isinstance(state, Mapping) else None
        )
        spend_committing = (
            state.get(_WBA_SPEND_COMMITTING_KEY) if isinstance(state, Mapping) else None
        )
    else:
        authorization_error = current_wba_authorization_error.get()
        spend_committing = current_wba_spend_committing.get()
    signer = verified_signer_for_request(request_context)
    if signer is None or signer.agent_url is None:
        if authorization_error is None:
            return context
        context = copy.copy(context) if context is not None else ToolContext()
        context.metadata = {**context.metadata, _WBA_AUTHORIZATION_ERROR_KEY: authorization_error}
        projection_only = (
            scope_has_wba_error_projection(request_scope)
            if isinstance(request_scope, Mapping)
            else current_wba_error_projection_only.get()
        )
        if projection_only:
            context.caller_identity = None
            context.metadata.pop("adcp.auth_info", None)
            context.metadata.pop("auth_info", None)
        return context
    if context is None:
        context = ToolContext()
    else:
        existing = context.metadata.get("adcp.auth_info")
        if existing is not None and getattr(existing, "credential", None) is not None:
            if signer.profile != "web-bot-auth" and authorization_error is not None:
                # Independent bearer credentials cannot revive the deprecated
                # signed profile after an origin's approved migration.
                context = copy.copy(context)
                context.metadata = {
                    **context.metadata,
                    _WBA_AUTHORIZATION_ERROR_KEY: authorization_error,
                }
            return context
        # Copy so a factory that returns a shared instance never carries one
        # request's signer into another.
        context = copy.copy(context)
        context.metadata = dict(context.metadata)
    if authorization_error is not None:
        context.metadata[_WBA_AUTHORIZATION_ERROR_KEY] = authorization_error
    if type(spend_committing) is bool:
        context.metadata[_WBA_SPEND_COMMITTING_KEY] = spend_committing
    context.metadata["adcp.auth_info"] = AuthInfo.from_verified_signer(
        signer,
        extra=(
            {
                "signing_profile": signer.profile,
                "principal_id": signer.principal_id,
                "wba_spend_committing": spend_committing,
                "wba_revocation": signer.revocation,
            }
            if signer.profile == "web-bot-auth"
            else None
        ),
    )
    context.caller_identity = signer.agent_url
    return context


def check_request_signature_verification(
    config: RequestSignatureVerification, *, bearer_configured: bool = False
) -> None:
    """Boot-time checks for :func:`adcp.server.serve`'s verification config.

    An in-memory replay store only protects one process; in production
    (``ADCP_ENV`` is ``prod``/``production``) it is refused unless
    ``ADCP_ALLOW_INMEMORY_REPLAY_STORE=1`` opts in, mirroring the
    in-memory TaskRegistry gate.
    """
    if not isinstance(config, RequestSignatureVerification):
        raise TypeError(
            "serve(request_signature_verification=...) expects "
            f"RequestSignatureVerification, got {type(config).__name__}"
        )
    if config.allow_bearer_fallback and not bearer_configured:
        raise ValueError(
            "RequestSignatureVerification(allow_bearer_fallback=True) needs a fallback "
            "authenticator; pass serve(auth=BearerTokenAuth(...))"
        )
    if config.enable_web_bot_auth and config.authorities is None:
        logger.warning(
            "Web Bot Auth verification has no authorities configured; set "
            "RequestSignatureVerification(authorities=...) to the host names this "
            "endpoint serves so a signature captured at another seller cannot be "
            "replayed here"
        )
    store = config.replay_store
    if store is None:
        logger.warning(
            "request signature verification is running without a replay store; "
            "captured signed requests can be replayed within their validity window"
        )
        return
    if not isinstance(store, InMemoryReplayStore):
        return
    production = os.environ.get("ADCP_ENV", "").strip().lower() in {"prod", "production"}
    if production and os.environ.get("ADCP_ALLOW_INMEMORY_REPLAY_STORE", "").strip() != "1":
        raise ValueError(
            "request signature verification is using a per-process InMemoryReplayStore "
            "in production (ADCP_ENV is 'prod' or 'production'). Replicas would not see "
            "each other's nonces, so a signed request could be replayed against another "
            "instance. Pass a shared store (e.g. adcp.signing.PgReplayStore) as "
            "replay_store=, or set ADCP_ALLOW_INMEMORY_REPLAY_STORE=1 for a "
            "single-process deployment."
        )
    logger.warning(
        "request signature verification is using a per-process InMemoryReplayStore; "
        "replay protection does not span replicas or restarts"
    )


def wrap_with_signature_verification(
    app: Any,
    config: RequestSignatureVerification | None,
    *,
    transport: Literal["mcp", "a2a"],
    message_parser: Any = None,
    bearer_configured: bool = False,
    defer_authorization_errors: bool = True,
) -> Any:
    """Wrap ``app`` with verification when ``config`` is set; else return it."""
    if config is None:
        return app
    return SignedRequestVerificationMiddleware(
        app,
        config,
        transport=transport,
        message_parser=message_parser,
        bearer_configured=bearer_configured,
        defer_authorization_errors=defer_authorization_errors,
    )


__all__ = [
    "RequestSignatureVerification",
    "SignedRequestVerificationMiddleware",
    "apply_verified_signer",
    "current_verified_signer",
    "verified_signer_for_request",
]
