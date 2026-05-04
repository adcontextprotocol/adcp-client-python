"""Request context for DecisioningPlatform method dispatch.

:class:`RequestContext` extends :class:`adcp.server.ToolContext` so the
existing framework's idempotency middleware, observability hooks, and
A2A executor — all of which consume ``ToolContext`` — keep working
unchanged. Adopters' Protocol method signatures take
``RequestContext[TMeta]`` and get typed access to the resolved
``account`` plus a typed metadata bag.

The dispatch adapter (in ``adcp.decisioning.dispatch``) constructs a
``RequestContext`` per request from the underlying ``ToolContext`` and
the platform's ``AccountStore.resolve(...)`` result.
"""

from __future__ import annotations

import warnings
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, ClassVar, Generic

from typing_extensions import TypeVar

from adcp.decisioning.resolve import ResourceResolver, _make_default_resolver
from adcp.decisioning.state import StateReader, _make_default_state_reader
from adcp.decisioning.types import Account, TaskHandoff, WorkflowHandoff
from adcp.server.base import ToolContext

if TYPE_CHECKING:
    from adcp.decisioning.recipe import Recipe
    from adcp.decisioning.registry import (
        BuyerAgent,
        Credential,
    )

#: Per-platform metadata generic; mirrors ``adcp.decisioning.types.TMeta``
#: but redeclared here so ``RequestContext[TMeta]`` parameterization
#: works without importing the same TypeVar from another module (mypy
#: treats same-name TypeVars from different modules as distinct types,
#: which breaks downstream Protocol matching).
TMeta = TypeVar("TMeta", default=dict[str, Any])

T = TypeVar("T")


@dataclass
class AuthInfo:
    """The verified principal authenticated for a request.

    Populated by the framework's signed-request verifier
    (:func:`adcp.signing.signed_request_verifier`) or a custom
    ``authenticate=`` callable wired via :func:`adcp.decisioning.serve`.
    Threaded onto :attr:`RequestContext.auth_info` so platform methods
    can read scopes, key_id, principal, etc., without parsing
    transport headers.

    **Two field families.** The flat fields (``kind`` / ``key_id`` /
    ``principal`` / ``scopes``) are the v6.0 surface — adopters built
    against the alpha pass these directly. The Tier 2 v3-identity
    fields (``credential`` / ``agent_url`` / ``operator`` / ``extra``)
    carry the typed AdCP v3 commercial identity context the
    :class:`adcp.decisioning.BuyerAgentRegistry` consumes. When an
    adopter constructs ``AuthInfo`` with only the flat fields,
    ``__post_init__`` synthesizes a typed bearer
    :class:`adcp.decisioning.Credential` from them and emits a
    :class:`DeprecationWarning` pointing at the adopter callsite.

    **Deprecation timeline:**

    * **4.4.0** (this release) — flat-field synthesis still works but
      warns. Adopter code stays runnable; the warning points at every
      callsite that constructs ``AuthInfo`` without an explicit
      ``credential=``.
    * **4.5.0** — synthesis is removed; flat-field-only construction
      stops auto-populating ``credential``, and the registry dispatch
      will reject the request with ``PERMISSION_DENIED``. Adopters
      must construct
      the typed credential explicitly:
      ``AuthInfo(credential=ApiKeyCredential(kind="api_key", key_id=...))``
      or use the bundled signed-request verifier middleware.

    The flat fields themselves stay (they carry useful audit / log
    context); only the synthesis-from-flat path is on the removal
    track.

    :param kind: One of ``'signed_request'``, ``'http_sig'``,
        ``'bearer'``, ``'api_key'``, ``'oauth'``, ``'mtls'``,
        ``'derived'``. Drives the legacy → ``credential`` synthesis.
    :param key_id: The signing key id (``kid``) for signed-request /
        http_sig auth, or the API-key id for bearer auth.
    :param principal: The authenticated principal label — for
        signed-request auth this is the verified ``agent_url`` (per
        AdCP v3 convention).
    :param scopes: Granted scopes / capabilities (OAuth or per-token).
    :param credential: Typed v3 :class:`adcp.decisioning.Credential` —
        the canonical surface the registry dispatches on. When
        unset, ``__post_init__`` synthesizes from the legacy fields.
        Adopters wiring v3 auth directly should construct the
        credential themselves and leave the legacy fields empty.
    :param agent_url: Verified buyer-agent URL — populated from
        ``credential.agent_url`` when ``credential`` is an
        :class:`adcp.decisioning.HttpSigCredential`. ``None`` for
        bearer / OAuth / unauthenticated traffic and for
        ``kind="signed_request"`` constructions that don't pass a
        typed credential (the SDK deliberately refuses to derive
        ``agent_url`` from the unverified ``principal`` string —
        see ``__post_init__`` for the rationale).
    :param operator: Operator / transport-tenant label — the AdCP v3
        operator binding (separate from the buyer agent). Distinct
        from ``ToolContext.tenant_id`` only for adopters running the
        AAO community proxy in front of a multi-operator deployment;
        most adopters leave this ``None``.
    :param extra: Adopter passthrough for auth-layer fields the SDK
        doesn't model (custom claims, MFA flags, internal session ids).
    """

    # Sentinel used as the default value of ``credential`` so
    # ``__post_init__`` can distinguish "adopter didn't pass credential
    # at all" (default → synthesize from flat fields) from "adopter
    # explicitly passed credential=None" (default → leave None,
    # don't re-synthesize). Without this sentinel,
    # ``dataclasses.replace(auth, credential=None)`` — the natural
    # idiom for clearing a credential — would re-trigger synthesis
    # from the still-present flat fields, contradicting the adopter's
    # intent.
    _UNSET_CREDENTIAL: ClassVar[Any] = object()

    kind: str
    key_id: str | None = None
    principal: str | None = None
    scopes: list[str] = field(default_factory=list)

    # ----- Tier 2 v3-identity fields -----
    credential: Credential | None = _UNSET_CREDENTIAL
    agent_url: str | None = None
    operator: str | None = None
    extra: Mapping[str, Any] = field(default_factory=dict)

    @staticmethod
    def _synthesize_bearer_credential(
        kind: str,
        key_id: str | None,
        principal: str | None,
        scopes: list[str],
    ) -> Credential | None:
        """Build a typed bearer :class:`Credential` from the flat
        fields, or return ``None`` when the flat fields don't describe
        a bearer credential.

        Signed-request kinds (``"signed_request"`` / ``"http_sig"``)
        intentionally never synthesize — a real
        :class:`HttpSigCredential` requires the
        ``verified_at`` timestamp from RFC 9421 verification, which
        only the verifier middleware has. Synthesizing one here would
        let any code that writes ``kind="signed_request"`` escalate
        bearer traffic onto the verified signed path. The verifier
        middleware constructs :class:`HttpSigCredential` explicitly
        and passes it via ``credential=``.
        """
        from adcp.decisioning.registry import (
            ApiKeyCredential,
            OAuthCredential,
        )

        if kind in {"api_key", "bearer"}:
            if key_id:
                return ApiKeyCredential(kind="api_key", key_id=key_id)
        elif kind == "oauth":
            client_id = key_id or principal
            if client_id:
                return OAuthCredential(
                    kind="oauth",
                    client_id=client_id,
                    scopes=tuple(scopes),
                )
        return None

    @classmethod
    def from_verified_signer(
        cls,
        signer: Any,
        *,
        scopes: list[str] | None = None,
        operator: str | None = None,
        extra: Mapping[str, Any] | None = None,
        max_verified_age_s: float | None = None,
        now: float | None = None,
    ) -> AuthInfo:
        """Build :class:`AuthInfo` from a :class:`adcp.signing.VerifiedSigner`.

        The supported migration target for the AuthInfo flat-field
        deprecation. Verifier middleware that runs RFC 9421
        verification produces a ``VerifiedSigner`` carrying the
        cryptographic claims (``key_id``, ``verified_at``, optional
        ``agent_url``); this helper projects that into a typed
        :class:`HttpSigCredential` + :class:`AuthInfo` ready for the
        commercial-identity registry dispatch.

        ::

            from adcp.signing import (
                VerifyOptions, verify_request_signature,
            )
            from adcp.decisioning import AuthInfo

            signer = verify_request_signature(
                method=request.method,
                url=request.url,
                headers=dict(request.headers),
                body=request.get_data(),
                options=VerifyOptions(...),
            )
            ctx.metadata["adcp.auth_info"] = AuthInfo.from_verified_signer(
                signer, max_verified_age_s=300.0,
            )

        The verifier MUST surface ``signer.agent_url`` for the
        commercial-identity registry to dispatch — without it, the
        framework has no key to look up. The verifier is configured
        with the ``agent_url`` claim shape per the AdCP v3 profile;
        if it's ``None`` here the verifier wasn't told to extract it
        and this helper raises :class:`ValueError` with a pointer to
        the misconfiguration. Buyers don't see this — server boot
        time error.

        :param signer: The :class:`VerifiedSigner` returned from
            :func:`adcp.signing.verify_request_signature`.
        :param scopes: Granted scopes / capabilities. The verifier
            doesn't extract these — adopter middleware fills in
            scopes derived from token introspection or per-key
            policy.
        :param operator: Operator label for AdCP v3 multi-operator
            deployments (AAO community proxy). Most adopters leave
            ``None``.
        :param extra: Adopter passthrough.
        :param max_verified_age_s: Maximum age (seconds) of
            ``signer.verified_at`` permitted at construction time.
            When set and ``now() - signer.verified_at >
            max_verified_age_s``, raises :class:`ValueError` —
            adopter caching a stale ``VerifiedSigner`` and replaying
            it later fails fast rather than passing a stale-but-
            cryptographically-valid credential into the registry
            dispatch. Default ``None`` keeps the v6.0-alpha
            behavior; production sellers SHOULD set a short window
            (e.g. 300s) matching the RFC 9421 nonce TTL.
        :param now: Override ``time.time()`` for ``max_verified_age_s``
            comparisons — only useful in tests. Default ``None``
            uses wall-clock.
        :raises ValueError: when ``signer.agent_url`` is ``None``,
            or when ``max_verified_age_s`` is set and
            ``signer.verified_at`` is older than the window.
        """
        import time

        from adcp.decisioning.registry import HttpSigCredential

        if signer.agent_url is None:
            raise ValueError(
                "VerifiedSigner.agent_url is None — the AdCP request-signing "
                "verifier wasn't configured to extract the agent_url claim. "
                "Set ``VerifyOptions.agent_url=`` (or its source) so the "
                "verifier surfaces it on success. Without an agent_url, the "
                "BuyerAgentRegistry has no key to dispatch on."
            )
        if max_verified_age_s is not None:
            current = now if now is not None else time.time()
            age = current - signer.verified_at
            if age > max_verified_age_s:
                raise ValueError(
                    f"VerifiedSigner.verified_at is {age:.1f}s old, exceeds "
                    f"max_verified_age_s={max_verified_age_s:.1f}s. The "
                    "verifier's signature was valid at the time of verification "
                    "but has aged out of the freshness window — typically "
                    "indicates a cached signer being replayed. Re-verify the "
                    "request signature instead of constructing AuthInfo from "
                    "a stale signer."
                )
        credential = HttpSigCredential(
            kind="http_sig",
            keyid=signer.key_id,
            agent_url=signer.agent_url,
            verified_at=signer.verified_at,
        )
        return cls(
            kind="http_sig",
            key_id=signer.key_id,
            principal=signer.agent_url,
            scopes=list(scopes) if scopes is not None else [],
            credential=credential,
            agent_url=signer.agent_url,
            operator=operator,
            extra=extra if extra is not None else {},
        )

    @classmethod
    def _from_legacy_dict(cls, raw: Mapping[str, Any]) -> AuthInfo:
        """Build :class:`AuthInfo` from a legacy dict-shape metadata
        payload without firing the :class:`DeprecationWarning`.

        The framework's :func:`_extract_auth_info` translates
        ``ctx.metadata['adcp.auth_info']`` dicts into typed
        ``AuthInfo``. That translation happens once per request and
        the warning's stack would point into framework code — not
        useful for adopters. Pre-synthesize the credential and pass
        it via ``credential=`` so :meth:`__post_init__`'s synthesis
        branch is skipped along with the warning.

        Adopter code that constructs ``AuthInfo`` directly (in their
        own ``context_factory`` / auth middleware) goes through
        :meth:`__post_init__` and *does* see the warning, pointing
        at the adopter callsite — which is the actionable signal
        this deprecation is meant to deliver.

        Validates the dict shape — adopters porting JS-side middleware
        sometimes write ``scopes`` as a string instead of a list, or
        pass a raw dict where a typed :class:`Credential` is expected.
        Silently coercing those would mask middleware bugs that only
        surface when the registry dispatch later tries to use the
        malformed credential. Raise :class:`TypeError` with the
        offending field name so adopter logs surface the issue
        immediately at server boot / first request.
        """
        from adcp.decisioning.registry import (
            ApiKeyCredential,
            HttpSigCredential,
            OAuthCredential,
        )

        # Type guards. Empty dict is fine — we project to a
        # ``kind="derived"`` AuthInfo with no credential.
        scopes_raw = raw.get("scopes", [])
        if not isinstance(scopes_raw, (list, tuple)):
            raise TypeError(
                "adcp.auth_info dict has scopes={scopes_raw!r} of type "
                f"{type(scopes_raw).__name__}; expected list / tuple of "
                "strings. Adopter middleware writing a string here (e.g. "
                "comma-separated scopes) needs to split before passing "
                "to AuthInfo.".format(scopes_raw=scopes_raw)
            )
        credential = raw.get("credential")
        if credential is not None and not isinstance(
            credential, (ApiKeyCredential, OAuthCredential, HttpSigCredential)
        ):
            raise TypeError(
                "adcp.auth_info dict has credential of type "
                f"{type(credential).__name__}; expected an instance of "
                "ApiKeyCredential / OAuthCredential / HttpSigCredential. "
                "Construct the typed credential explicitly in your auth "
                "middleware — the framework can't safely build it from a "
                "raw dict because it can't distinguish kinds."
            )
        extra = raw.get("extra", {})
        if not isinstance(extra, Mapping):
            raise TypeError(
                f"adcp.auth_info dict has extra={extra!r} of type "
                f"{type(extra).__name__}; expected a Mapping."
            )

        kind = raw.get("kind", "derived")
        key_id = raw.get("key_id")
        principal = raw.get("principal")
        scopes = list(scopes_raw)
        if credential is None:
            credential = cls._synthesize_bearer_credential(kind, key_id, principal, scopes)
        return cls(
            kind=kind,
            key_id=key_id,
            principal=principal,
            scopes=scopes,
            credential=credential,
            agent_url=raw.get("agent_url"),
            operator=raw.get("operator"),
            extra=extra,
        )

    def __post_init__(self) -> None:
        """Synthesize a typed bearer ``credential`` from the flat
        ``kind`` / ``key_id`` / ``principal`` fields when not supplied
        directly, and emit a :class:`DeprecationWarning` pointing at
        the adopter callsite that needs to migrate.

        Synthesis fires for bearer kinds only; signed-request kinds
        require an explicit :class:`HttpSigCredential` from the
        verifier (see :meth:`_synthesize_bearer_credential` for
        rationale). ``agent_url`` is derived from a present
        :class:`HttpSigCredential` only — never from the
        ``principal`` string, since unverified principals must not
        appear as verified agent URLs.

        Synthesis is one-way: explicit ``credential=`` always wins
        and suppresses the warning.
        """
        from adcp.decisioning.registry import HttpSigCredential

        if self.credential is AuthInfo._UNSET_CREDENTIAL:
            # Default — adopter didn't pass ``credential=`` at all.
            # Synthesize from flat fields if they describe a bearer
            # credential; warn so the adopter migrates.
            synthesized = self._synthesize_bearer_credential(
                self.kind, self.key_id, self.principal, self.scopes
            )
            if synthesized is not None:
                self.credential = synthesized
                warnings.warn(
                    "AuthInfo was constructed without an explicit "
                    "`credential=`; the SDK synthesized "
                    f"{type(synthesized).__name__} from the flat "
                    "`kind` / `key_id` / `principal` fields. The "
                    "synthesis path is deprecated and will be removed "
                    "in adcp 4.5.0. Construct the typed credential "
                    "explicitly, e.g. "
                    '`AuthInfo(credential=ApiKeyCredential(kind="api_key",'
                    " key_id=...))`. See "
                    "docs/proposals/v3-identity-bundle-design.md for "
                    "the v3 identity migration guide.",
                    DeprecationWarning,
                    stacklevel=2,
                )
            else:
                self.credential = None
        # ELSE: adopter passed ``credential=...`` explicitly (real
        # credential or ``None``). Honor their value verbatim — no
        # synthesis, no warning. This makes
        # ``dataclasses.replace(auth, credential=None)`` correctly
        # clear the credential without re-running synthesis.

        if self.agent_url is None and isinstance(self.credential, HttpSigCredential):
            self.agent_url = self.credential.agent_url


@dataclass
class RequestContext(ToolContext, Generic[TMeta]):
    """Per-request context passed to every Protocol method.

    Subclasses :class:`adcp.server.ToolContext` so the existing
    framework primitives (idempotency middleware, observability,
    A2A executor) consume it as a ``ToolContext`` while adopter
    Protocol methods read the typed :attr:`account` directly.

    **Framework-only construction.** Adopter code receives a
    ``RequestContext`` from the framework on every dispatch via the
    hydration helper in ``adcp.decisioning.dispatch``. Direct
    construction is supported for tests only — production code that
    builds a ``RequestContext`` from outside the dispatch seam is a
    bug. Adopters who need to modify the context (custom middleware,
    test doubles for ``state`` / ``resolve``) should use
    :func:`dataclasses.replace`, not raw construction. Mirrors the
    TS-side ``to-context.ts:buildRequestContext`` contract.

    :param account: The resolved account, with typed ``metadata: TMeta``.
        The framework's idempotency middleware reads
        ``ctx.caller_identity`` for cache scoping; the dispatch adapter
        sets ``caller_identity = account.id`` so caching scopes per
        resolved account, not per raw auth principal.
    :param auth_info: Optional verified principal info. ``None`` when
        the request is unauthenticated (dev / ``'derived'`` fixtures).
    :param now: Monotonic timestamp for the request — adopters use
        this rather than ``datetime.now()`` directly so tests can
        inject deterministic clocks.

    Adopters call :meth:`handoff_to_task` to promote a method to the
    HITL background-task path. The framework dispatcher detects the
    returned :class:`TaskHandoff` via type-identity and projects it
    to the wire ``Submitted`` envelope.

    **Identifier disambiguation — when to use which:**

    The context carries four identifier-shaped fields. Each has a
    distinct role; mixing them up is the most common adopter bug.

    ``account.id`` — "whose data is this?"
        The resolved tenant / account that owns the call. Read it to
        route the request to the right adapter instance, scope your
        DB queries, and stamp audit logs.

    ``auth_principal`` — "who's calling?"
        The verified caller's identity label and the typed read for
        adopter authorization checks. Populated from two sources
        depending on the auth flow:

        * **Signed-request flows** — sourced from
          :class:`AuthInfo.principal` (``agent_url`` for AdCP v3
          signed-request agents per spec).
        * **Bearer-token flows** — sourced from the
          :data:`adcp.server.auth.current_principal` ContextVar that
          :class:`BearerTokenAuthMiddleware` populates
          (``Principal.caller_identity`` from the validator).

        Read it for per-principal ACLs *within* an account ("can
        principal X mutate this buy?"). ``None`` for unauthenticated
        dev fixtures.

    ``caller_identity`` — "what's the cache scope key?"
        Starts as the bare principal at the transport layer
        (:class:`ToolContext.caller_identity`), then the framework
        dispatch helper mutates it into the composite scope key
        (``<store_module>.<store_qualname>:<account_id>``) before
        the handler sees the :class:`RequestContext`. The
        idempotency middleware reads the composite form to scope
        the replay cache.

        **Do not read this for identity decisions** — by the time a
        handler observes the field it's a cache key, not a
        principal label. Use ``auth_principal`` for "who's calling?"
        and treat ``caller_identity`` as opaque (log / forward only;
        don't parse, compare, or rewrite). The composite format is
        framework-internal and any adopter assumption about its
        shape will break when the scope-key composition changes.

    ``tenant_id`` — "which transport tenant?"
        Inherited from :class:`ToolContext`; set by the transport
        layer before dispatch (typically from the host header or URL
        path on multi-tenant deployments). Usually equals
        ``account.id`` for ``'explicit'``-resolution adopters; can
        diverge for ``'derived'`` / ``'implicit'`` modes.

    Common patterns:

    * Routing to the right adapter? → ``ctx.account.metadata.adapter``
      (typed via the ``TMeta`` generic).
    * Authorization check? → ``ctx.auth_principal`` (who's calling)
      against ``ctx.account.id`` (whose data they're touching).
    * Idempotency scope? → don't touch; the framework owns this.
    * Logging request provenance? → log all four; they're cheap.

    :param state: Sync reads of framework-owned in-flight workflow
        state. Default is :class:`adcp.decisioning.state._NotYetWiredStateReader`
        — returns empty values + emits one-time UserWarning per
        method on first call. v6.1 wires the backing store.
    :param resolve: Async framework-mediated fetches with cache +
        validation. Default is
        :class:`adcp.decisioning.resolve._NotYetWiredResolver` — raises
        ``NotImplementedError`` on every call. v6.1 wires the backing
        fetchers.
    :param auth_principal: Typed convenience field carrying the
        verified principal label. Sourced from
        :class:`AuthInfo.principal` on signed-request flows and from
        the :data:`adcp.server.auth.current_principal` ContextVar
        on bearer-token flows (the framework's
        :class:`BearerTokenAuthMiddleware` populates the
        ContextVar; the dispatch helper reads it when ``auth_info``
        is absent). The right read for "who's calling?" — distinct
        from ``caller_identity``, which the framework mutates into
        a composite cache scope key for idempotency.
    """

    # Default factories so ``RequestContext()`` works in tests; in
    # production the dispatch adapter populates every field.
    account: Account[TMeta] = field(default_factory=lambda: Account(id="<unset>"))
    auth_info: AuthInfo | None = None
    auth_principal: str | None = None
    buyer_agent: BuyerAgent | None = None
    now: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    state: StateReader = field(default_factory=_make_default_state_reader)
    resolve: ResourceResolver = field(default_factory=_make_default_resolver)
    # ``recipes`` — populated by the framework on dispatch paths that
    # hydrate a proposal (post-finalize ``create_media_buy`` /
    # ``update_media_buy`` / ``get_media_buy_delivery``). Empty mapping
    # by default so legacy / non-proposal flows see the v1 shape. See
    # ``docs/proposals/proposal-manager-v15-design.md`` § D3.
    recipes: Mapping[str, Recipe] = field(default_factory=dict)

    def handoff_to_task(
        self,
        fn: Callable[[Any], Awaitable[T] | T],
    ) -> TaskHandoff[T]:
        """Promote this call to a background task.

        The buyer sees ``{status: 'submitted', task_id}`` on the
        immediate response; the framework runs ``fn`` after returning,
        persists ``fn``'s terminal artifact to the task registry, and
        emits a push-notification webhook on terminal state.

        ``fn`` receives a ``TaskHandoffContext`` (defined in
        :mod:`adcp.decisioning.dispatch`) carrying:

        * ``id`` — framework-issued task UUID
        * ``update(progress)`` — write progress payload, transition
          ``'submitted'`` → ``'working'``
        * ``heartbeat()`` — liveness signal (v6.1 stub)

        Adopter code passes either a coroutine function (``async def
        review_async(task_ctx): ...``) or a sync callable; the
        dispatcher detects which and runs it appropriately.

        For external workflows that complete on their own schedule
        (human queue review, batch jobs, Airflow DAGs, ML pipelines)
        — use :meth:`handoff_to_workflow` instead. The split is purely
        about where the work runs (in-process / framework-managed vs.
        adopter-owned external system).
        """
        return TaskHandoff(fn)

    def handoff_to_workflow(
        self,
        fn: Callable[[Any], Awaitable[None] | None],
    ) -> WorkflowHandoff:
        """Promote this call to an externally-completed task.

        For workflows that run OUTSIDE the framework's process —
        human queue review (trafficker UI), nightly batch jobs,
        Airflow DAGs, ML pipelines, scheduled cron. The framework
        allocates a ``task_id``, calls ``fn`` ONCE synchronously
        (or awaits it if a coroutine) to register the work into the
        adopter's external system, persists ``submitted`` state, and
        returns the wire envelope. NO background coroutine runs in
        the framework.

        ``fn`` receives a :class:`TaskHandoffContext` carrying
        ``id`` (framework-allocated task_id) and ``_registry``
        (adopter can stash a reference for later completion). The
        adopter's external workflow later calls
        ``registry.complete(task_id, result)`` or
        ``registry.fail(task_id, error)`` directly when the work
        finishes — minutes, hours, or days later.

        Buyer experience is identical to :meth:`handoff_to_task` —
        same ``{task_id, status: 'submitted'}`` wire envelope, same
        ``tasks/get`` polling, same push-notification webhook on
        terminal state.

        **Rollback.** If ``fn`` raises during enqueue, the framework
        discards the just-allocated task_id from the registry and
        propagates the exception (wrapped to ``AdcpError`` per the
        dispatch contract). Adopter enqueue fns that need
        transactional persistence wrap their own DB write in their
        own transaction; the framework's rollback is registry-side
        only.

        Example::

            class TraffickerSeller(DecisioningPlatform):
                def __init__(self, review_queue, task_registry):
                    self.review_queue = review_queue
                    # Stash for later completion when human acts
                    self.task_registry = task_registry

                def create_media_buy(self, req, ctx):
                    if self._needs_human_approval(req):
                        return ctx.handoff_to_workflow(
                            lambda task_ctx: self._enqueue(task_ctx, req)
                        )
                    return CreateMediaBuySuccess(media_buy_id="mb_1", ...)

                def _enqueue(self, task_ctx, req):
                    self.review_queue.add(
                        task_id=task_ctx.id,
                        request_snapshot=req.model_dump(),
                    )

                # Elsewhere — Flask handler for the trafficker UI:
                async def on_decision(self, task_id, decision):
                    if decision.approved:
                        await self.task_registry.complete(
                            task_id,
                            CreateMediaBuySuccess(...).model_dump(),
                        )
                    else:
                        await self.task_registry.fail(
                            task_id, AdcpError(...).to_wire(),
                        )

        See :class:`adcp.decisioning.WorkflowHandoff` for the full
        semantics.
        """
        return WorkflowHandoff(fn)
