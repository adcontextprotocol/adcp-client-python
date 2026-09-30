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
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from functools import partial
from typing import TYPE_CHECKING, Any, Literal

import anyio
import anyio.from_thread
import anyio.to_thread
from starlette.requests import Request
from starlette.responses import JSONResponse

from adcp.server._size_limit import make_replay_receive
from adcp.signing.errors import (
    REQUEST_SIGNATURE_HEADER_MALFORMED,
    REQUEST_SIGNATURE_JWKS_UNAVAILABLE,
    REQUEST_SIGNATURE_REQUIRED,
    SignatureVerificationError,
)
from adcp.signing.middleware import VERIFIED_SIGNER_SCOPE_KEY, unauthorized_response_headers
from adcp.signing.replay import InMemoryReplayStore, ReplayStore
from adcp.signing.revocation import RevocationChecker
from adcp.signing.verifier import (
    VerifiedSigner,
    VerifierCapability,
    VerifyOptions,
    verify_request_signature,
)

if TYPE_CHECKING:
    from adcp.server.base import ToolContext
    from adcp.signing.signer_keys import ResolvedSignerKey, SignerKeyResolver
    from adcp.signing.verifier import CoversDigestPolicy, SigningProfileVersion

logger = logging.getLogger(__name__)

#: Verified signer for the in-flight request. Carries on stateless MCP and
#: A2A, where dispatch shares the middleware's task; stateful MCP reads the
#: ``scope["state"]`` mirror instead.
current_verified_signer: contextvars.ContextVar[VerifiedSigner | None] = contextvars.ContextVar(
    "adcp_current_verified_signer", default=None
)

SignaturePosture = Literal["required", "warn", "supported"]

# Set only by SignedRequestVerificationMiddleware; bearer auth lets a request
# carrying it through without a token.
_FRAMEWORK_VERIFIED_SCOPE_KEY = "adcp.server.signature_verified"


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
    :param replay_store: Nonce store shared by every request. The default is
        one per-process :class:`~adcp.signing.InMemoryReplayStore`, which
        does not protect a multi-replica deployment; wire a shared store
        (e.g. ``PgReplayStore``) there. ``None`` disables replay checks.
    """

    signer_keys: SignerKeyResolver
    required_for: frozenset[str] = frozenset()
    warn_for: frozenset[str] = frozenset()
    supported_for: frozenset[str] = frozenset()
    protocol_methods_required_for: frozenset[str] = frozenset()
    protocol_methods_warn_for: frozenset[str] = frozenset()
    covers_content_digest: CoversDigestPolicy = "either"
    replay_store: ReplayStore | None = field(default_factory=InMemoryReplayStore)
    revocation_checker: RevocationChecker | None = None
    signing_profile_version: SigningProfileVersion = "3.1"

    def __post_init__(self) -> None:
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
        (``replay_store``, ``revocation_checker``, ``signing_profile_version``).
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


class SignedRequestVerificationMiddleware:
    """Pure-ASGI middleware enforcing :class:`RequestSignatureVerification`."""

    def __init__(
        self,
        app: Any,
        config: RequestSignatureVerification,
        *,
        transport: Literal["mcp", "a2a"],
        message_parser: Any = None,
    ) -> None:
        self._app = app
        self._config = config
        self._transport = transport
        self._message_parser = message_parser
        self._parse_skill = bool(config.required_for or config.warn_for)

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
        replay = make_replay_receive(chunks)

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
        if signature_input is None and signature is None:
            if posture == "required":
                await self._reject(
                    send,
                    SignatureVerificationError(
                        REQUEST_SIGNATURE_REQUIRED,
                        step=0,
                        message=f"operation {label!r} requires a signature",
                    ),
                )
                return
            if posture == "warn":
                logger.warning(
                    "unsigned request to warn_for target",
                    extra={"adcp_operation": label, "reason": REQUEST_SIGNATURE_REQUIRED},
                )
            await self._app(scope, replay, send)
            return

        try:
            signer = await self._verify(scope, body, label, posture)
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
        await self._call_with_signer(scope, replay, send, signer)

    async def _verify(
        self,
        scope: Any,
        body: bytes,
        label: str,
        posture: SignaturePosture | None,
    ) -> VerifiedSigner:
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
        return replace(signer, agent_url=key.agent_url if key is not None else None)

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
        self, scope: Any, receive: Any, send: Any, signer: VerifiedSigner
    ) -> None:
        scope[VERIFIED_SIGNER_SCOPE_KEY] = signer
        scope[_FRAMEWORK_VERIFIED_SCOPE_KEY] = True
        scope.setdefault("state", {})[VERIFIED_SIGNER_SCOPE_KEY] = signer
        if (
            self._transport == "a2a"
            and signer.agent_url is not None
            and not getattr(scope.get("user"), "is_authenticated", False)
        ):
            # a2a-sdk scopes task ownership by the authenticated user. Without
            # this every signed buyer shares one owner and can read or cancel
            # another buyer's tasks. Bearer auth (inner) overrides it when a
            # token is also presented.
            from adcp.server.auth import _A2AAuthenticatedUser

            scope["user"] = _A2AAuthenticatedUser(display_name=signer.agent_url)
        token = current_verified_signer.set(signer)
        try:
            await self._app(scope, receive, send)
        finally:
            current_verified_signer.reset(token)

    @staticmethod
    async def _reject(send: Any, exc: SignatureVerificationError) -> None:
        logger.info(
            "request signature rejected",
            extra={"reason": exc.code, "step": exc.step},
        )
        response = JSONResponse(
            {"error": exc.code},
            status_code=401,
            headers=unauthorized_response_headers(exc),
        )

        async def no_receive() -> dict[str, Any]:
            return {"type": "http.disconnect"}

        await response({"type": "http"}, no_receive, send)


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


def scope_has_verified_signer(scope: Mapping[str, Any]) -> bool:
    """True when framework verification authenticated this request's signer.

    Only the framework middleware sets this, so a seller that runs
    ``verify_starlette_request`` itself without opting in keeps its bearer
    requirement unchanged.
    """
    return scope.get(_FRAMEWORK_VERIFIED_SCOPE_KEY) is True


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
    signer = verified_signer_for_request(request_context)
    if signer is None or signer.agent_url is None:
        return context

    from adcp.decisioning.context import AuthInfo
    from adcp.server.base import ToolContext

    if context is None:
        context = ToolContext()
    else:
        existing = context.metadata.get("adcp.auth_info")
        if existing is not None and getattr(existing, "credential", None) is not None:
            return context
        # Copy so a factory that returns a shared instance never carries one
        # request's signer into another.
        context = copy.copy(context)
        context.metadata = dict(context.metadata)
    context.metadata["adcp.auth_info"] = AuthInfo.from_verified_signer(signer)
    context.caller_identity = signer.agent_url
    return context


def check_request_signature_verification(config: RequestSignatureVerification) -> None:
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
) -> Any:
    """Wrap ``app`` with verification when ``config`` is set; else return it."""
    if config is None:
        return app
    return SignedRequestVerificationMiddleware(
        app, config, transport=transport, message_parser=message_parser
    )


__all__ = [
    "RequestSignatureVerification",
    "SignedRequestVerificationMiddleware",
    "apply_verified_signer",
    "current_verified_signer",
    "verified_signer_for_request",
]
