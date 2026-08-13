"""Legacy sync-completion webhook compatibility support.

AdCP task webhooks describe status changes after the initial response.
When that response is already terminal, the buyer has the result inline
and no task webhook is emitted. ``auto_emit_completion_webhooks``
therefore defaults to ``False``.

Setting the flag to ``True`` preserves the former SDK behavior as a
non-conformant compatibility extension. It duplicates the inline result
in a webhook and synthesizes ``task_id`` as ``f"sync-{uuid4()}"``.
Because no registry task exists for a synchronous response, that ID
cannot be read through ``tasks/get``; compatibility consumers must
correlate through resource IDs embedded in ``result``.

**Fire-and-forget.** Webhook delivery runs in a background asyncio
task; the sync response returns inline immediately. A buyer-supplied
slowloris webhook URL must not be able to hold the seller's request
worker for the full retry budget — the JS round-2 fix (``7a887dfa``)
addressed this DoS vector and Python preserves the same posture.
``_BACKGROUND_WEBHOOK_TASKS`` strong-refs in-flight emissions so the
asyncio loop's weak-ref behavior doesn't garbage-collect them
mid-flight.

**Spec gate.** Only tools in :data:`SPEC_WEBHOOK_TASK_TYPES` (the
closed enum from ``schemas/cache/enums/task-type.json``)
emit. Spec-validating webhook receivers reject envelopes with
non-spec ``task_type`` values; tools the framework dispatches that
aren't in the spec enum (adopter-only specialism methods) skip
delivery and rely on ``publishStatusChange`` for state updates.

Async :class:`TaskHandoff` completion and failure webhooks are separate,
spec-required behavior and are not disabled by this compatibility flag.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from typing import TYPE_CHECKING, Any

from adcp.decisioning.account_projection import (
    strip_credentials_from_wire_result,
)

if TYPE_CHECKING:
    from adcp.decisioning.platform import DecisioningCapabilities
    from adcp.webhook_sender import WebhookSender
    from adcp.webhook_supervisor import WebhookDeliverySupervisor

    DeliveryTarget = WebhookSender | WebhookDeliverySupervisor

logger = logging.getLogger(__name__)


#: Tools eligible for sync-completion webhook auto-emit. Mirrors the
#: closed enum in ``schemas/cache/enums/task-type.json`` verbatim. The
#: framework dispatches a wider tool surface than this set; the JS side
#: maintains the same set at
#: ``src/lib/server/decisioning/runtime/protocol-for-tool.ts``.
#:
#: Drift policy: bump this constant AND the JS
#: ``SPEC_WEBHOOK_TASK_TYPES`` in lockstep when the spec enum widens.
#: A unit test pins this to the on-disk enum so out-of-band drift
#: surfaces in CI.
SPEC_WEBHOOK_TASK_TYPES: frozenset[str] = frozenset(
    {
        "create_media_buy",
        "update_media_buy",
        "media_buy_delivery",
        "build_creative",
        "sync_creatives",
        "activate_signal",
        "get_products",
        "get_signals",
        "create_property_list",
        "update_property_list",
        "get_property_list",
        "list_property_lists",
        "delete_property_list",
        "sync_accounts",
        "get_account_financials",
        "get_creative_delivery",
        "sync_event_sources",
        "sync_audiences",
        "sync_catalogs",
        "log_event",
        "get_brand_identity",
        "search_brands",
        "get_rights",
        "acquire_rights",
    }
)


#: Strong-ref the in-flight auto-emit tasks so the asyncio loop's
#: weak-ref behavior doesn't garbage-collect them mid-flight.
#: Module-level so the set survives across requests; framework-internal,
#: never exported. Mirrors ``_BACKGROUND_HANDOFF_TASKS`` in
#: ``dispatch.py``.
_BACKGROUND_WEBHOOK_TASKS: set[asyncio.Task[None]] = set()


def _extract_push_notification_url_and_token(
    params: Any,
) -> tuple[str, str | None] | None:
    """Pull ``(url, token)`` from ``params.push_notification_config``.

    Returns ``None`` when the request didn't carry the field, the field
    is None, or the URL is empty. Tolerates both Pydantic models and
    plain dicts on ``params`` since handler shims and test fixtures
    both call in. The URL is unwrapped via ``str()`` so the webhook
    sender sees a plain string (Pydantic AnyUrl stringifies to canonical
    form).
    """
    config = getattr(params, "push_notification_config", None)
    if config is None and isinstance(params, dict):
        config = params.get("push_notification_config")
    if config is None:
        return None
    url = getattr(config, "url", None)
    if url is None and isinstance(config, dict):
        url = config.get("url")
    if not url:
        return None
    token = getattr(config, "token", None)
    if token is None and isinstance(config, dict):
        token = config.get("token")
    return (str(url), token)


def _extract_push_operation_id(params: Any) -> str | None:
    """Pull the buyer-supplied ``operation_id`` off
    ``params.push_notification_config``.

    Per ``schemas/cache/core/push_notification_config.json`` the buyer
    registers ``operation_id`` on the push config and the seller MUST
    echo it verbatim into every webhook payload's ``operation_id``
    field. The seller MUST NOT recover it from the URL — the wire-level
    source of truth is this field. Tolerates Pydantic models and plain
    dicts; returns ``None`` when absent.
    """
    config = getattr(params, "push_notification_config", None)
    if config is None and isinstance(params, dict):
        config = params.get("push_notification_config")
    if config is None:
        return None
    operation_id = getattr(config, "operation_id", None)
    if operation_id is None and isinstance(config, dict):
        operation_id = config.get("operation_id")
    return operation_id


async def _emit_sync_completion_webhook(
    *,
    target: DeliveryTarget,
    url: str,
    token: str | None,
    method_name: str,
    result: Any,
) -> None:
    """Fire one sync-completion webhook. Logged-and-swallowed on failure.

    Wrapped by the caller in :func:`asyncio.create_task` so the sync
    response returns to the buyer immediately. Truncated to 16 hex
    chars (~64 bits) — adequate for buyer correlation. Buyers
    correlate primarily via resource ids on ``result``
    (``media_buy_id``, ``creative_id``, etc.); ``task_id`` here is
    informational for the spec's required-field shape.

    ``target`` is either a bare :class:`WebhookSender` (one attempt,
    no breaker) or a :class:`WebhookDeliverySupervisor` (retry, breaker,
    optional delivery log). Both expose ``send_mcp(...)`` with
    compatible kwargs; the call site is polymorphic.
    """
    task_id = f"sync-{uuid.uuid4().hex[:16]}"
    try:
        await target.send_mcp(
            url=url,
            task_id=task_id,
            status="completed",
            task_type=method_name,
            result=result,
            token=token,
        )
    except Exception:
        # Logged-and-swallowed: the sync response has already returned
        # to the buyer with the result inline.
        logger.warning(
            "[adcp.decisioning] sync completion webhook for %s "
            "task_id=%s failed; sync response already returned to buyer",
            method_name,
            task_id,
            exc_info=True,
        )


def maybe_emit_sync_completion(
    *,
    sender: WebhookSender | None,
    enabled: bool,
    method_name: str,
    params: Any,
    result: Any,
    supervisor: WebhookDeliverySupervisor | None = None,
) -> None:
    """Fire-and-forget auto-emit gate. Called by handler shims after
    the sync-success arm of mutating tools.

    Skips silently when:

    * ``enabled`` is False (the conformant default).
    * The request didn't carry ``push_notification_config.url``.

    Logs a WARNING when:

    * ``sender`` is None but the buyer DID register
      ``push_notification_config.url`` — the buyer's notification
      registration is being silently dropped, which the adopter
      explicitly requested compatibility delivery but did not configure
      its transport. Wire ``webhook_sender`` into
      :func:`adcp.decisioning.serve` or disable the compatibility flag.
    * ``method_name`` isn't in :data:`SPEC_WEBHOOK_TASK_TYPES` (the
      adopter extended the tool surface beyond the spec enum).

    Schedules the actual delivery via the running event loop's
    ``create_task`` so the sync response path is non-blocking.

    **Exception isolation.** The gate runs AFTER the platform method's
    successful return. ANY exception in here — extraction quirk on a
    weird ``params`` shape, ``loop.create_task`` failure — must NOT
    propagate to the handler shim, which would lose the buyer's sync
    response. The whole body is wrapped in ``try/except Exception``;
    logged-and-swallowed.
    """
    try:
        if not enabled:
            return

        # Cheap pre-check: did the buyer register ANY
        # ``push_notification_config``? Done BEFORE the full
        # extraction so the sender=None warning fires even on weird
        # ``params`` shapes that would have made
        # ``_extract_push_notification_url_and_token`` raise. The
        # outer ``try/except Exception`` would otherwise swallow such
        # extraction errors and we'd reproduce the very silent-drop
        # behavior this gate is supposed to eliminate.
        config = getattr(params, "push_notification_config", None)
        if config is None and isinstance(params, dict):
            config = params.get("push_notification_config")
        if config is None:
            return  # buyer didn't register — nothing to do

        target = supervisor or sender
        if target is None:
            # Buyer registered a webhook config but the adopter didn't
            # wire a sender. Without this branch, the buyer's
            # notification quietly disappears — they think they
            # registered for completion webhooks and just never
            # receive any. Surfacing a warning on first call gives
            # the adopter a fast path to the misconfig.
            #
            # Try to surface the URL for actionable error context;
            # fall back to the config repr when extraction raises
            # mid-traversal (still better than silent skip).
            try:
                url_for_log = getattr(config, "url", None)
                if url_for_log is None and isinstance(config, dict):
                    url_for_log = config.get("url")
            except Exception:
                url_for_log = None
            logger.warning(
                "[adcp.decisioning] buyer registered "
                "push_notification_config (url=%s) for %s but auto-emit "
                "has neither webhook_sender nor webhook_supervisor — "
                "webhook silently dropped. Pass one to "
                "adcp.decisioning.serve.create_adcp_server_from_platform, "
                "or disable auto_emit_completion_webhooks to silence "
                "this warning.",
                url_for_log if url_for_log else "<unextractable>",
                method_name,
            )
            return

        extracted = _extract_push_notification_url_and_token(params)
        if extracted is None:
            return
        url, token = extracted
        # Defense-in-depth: strip credentials from the result BEFORE the
        # webhook target sees it. The dispatcher already strips on the
        # synchronous return path (:func:`_invoke_platform_method`);
        # this is a second pass so the strip fires regardless of how
        # the result reached this gate (direct adopter call, custom
        # shim, future plumbing). Method-gated — non-account tools
        # short-circuit without walking the result.
        result = strip_credentials_from_wire_result(method_name, result)
        if method_name not in SPEC_WEBHOOK_TASK_TYPES:
            logger.warning(
                "[adcp.decisioning] sync completion webhook for %s skipped — "
                "tool not in spec task-type enum (closed set per "
                "schemas/cache/enums/task-type.json). Use "
                "publishStatusChange for long-running %s state.",
                method_name,
                method_name,
            )
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            # Production code that lands here is mis-wired (handler
            # shim called outside an event loop); bump to warning so
            # it's visible. Cost of one warning per misuse is
            # negligible vs. the cost of silent webhook loss.
            logger.warning(
                "[adcp.decisioning] sync completion webhook for %s "
                "skipped — no running event loop. The handler shim is "
                "expected to run inside an asyncio task; this branch "
                "fires when sync test code calls into the handler "
                "outside ``asyncio.run`` or ``pytest.mark.asyncio``.",
                method_name,
            )
            return
        bg = loop.create_task(
            _emit_sync_completion_webhook(
                target=target,
                url=url,
                token=token,
                method_name=method_name,
                result=result,
            ),
            name=f"adcp-sync-completion-{method_name}",
        )
        _BACKGROUND_WEBHOOK_TASKS.add(bg)
        bg.add_done_callback(_BACKGROUND_WEBHOOK_TASKS.discard)
    except Exception:
        # Last-line defense: an unexpected exception in the gate
        # itself (extraction quirk, scheduler error) must never
        # propagate to the handler shim, which has already produced
        # a successful sync response for the buyer.
        logger.warning(
            "[adcp.decisioning] sync completion webhook gate raised "
            "for %s; sync response unaffected",
            method_name,
            exc_info=True,
        )


async def emit_terminal_completion_webhook(
    *,
    target: DeliveryTarget | None,
    enabled: bool,
    method_name: str,
    params: Any,
    status: str,
    task_id: str,
    result: Any = None,
) -> None:
    """Deliver the terminal completion / failure webhook for an async task.

    Fired from the BACKGROUND completion path of
    :func:`adcp.decisioning.dispatch._project_handoff` — once, after the
    registry has recorded the terminal state. This is the async-path
    counterpart to :func:`maybe_emit_sync_completion`: when a seller
    returns a ``Submitted`` envelope (the request handed off to a task)
    AND the buyer supplied ``push_notification_config``, the spec
    (AdCP, adcp#5389) requires the seller to deliver at least the
    terminal completion / failure notification to that webhook. Buyers
    who registered a push config get notified without polling
    ``tasks/get``.

    Unlike the sync gate, this coroutine is already running inside the
    background task — there is no inline buyer response to protect, so
    the delivery is awaited directly rather than scheduled fire-and-
    forget. The whole body is wrapped in ``try/except Exception`` and
    logged-and-swallowed: a webhook delivery failure must never crash
    the background task or block the registry's terminal-state record
    (which the buyer can still read via ``tasks/get``).

    Skips silently when:

    * ``enabled`` is False (a low-level caller owns task delivery).
    * ``method_name`` isn't in :data:`SPEC_WEBHOOK_TASK_TYPES`. This
      gate runs FIRST, before any target check. SDK-internal,
      non-spec task types (e.g. ``finalize_proposal``, an interception
      of ``get_products`` in ``proposal_dispatch.py``) flow through
      ``_project_handoff`` like any async task but legitimately have no
      webhook target wired; per the spec-gate rule above
      :data:`SPEC_WEBHOOK_TASK_TYPES`, they skip delivery and rely on
      ``tasks/get`` polling / ``publishStatusChange``. Returning here
      before the ``target is None`` branch keeps a correctly-configured
      server from logging a spurious "silently dropped" WARNING on
      every async non-spec task.
    * The request didn't carry ``push_notification_config.url``
      (polling-only via ``tasks/get`` — the spec permits this).

    Logs a WARNING when:

    * ``target`` is None but the buyer DID register a push config for a
      SPEC-eligible task type — their terminal notification is being
      silently dropped, the same misconfig the sync gate warns on.

    :param status: ``'completed'`` on success or ``'failed'`` on a
        terminal failure. The wire ``GeneratedTaskStatus`` enum.
    :param result: On success, the projected terminal artifact (the
        same shape persisted to the registry). On failure, the
        structured error wire dict (``error.to_wire()``) so the buyer
        sees the failure inline. ``operation_id`` is echoed verbatim
        from ``push_notification_config.operation_id`` and ``task_id``
        is the registry-minted id.
    """
    try:
        if not enabled:
            return

        # Spec gate FIRST — before any target / config inspection. Task
        # types outside the closed spec enum (SDK-internal interceptions
        # like ``finalize_proposal``) are not webhook-eligible; they skip
        # silently and rely on ``tasks/get`` / ``publishStatusChange``.
        # Running this ahead of the ``target is None`` branch is what
        # stops a correctly-configured server from emitting a spurious
        # "silently dropped" WARNING on every async non-spec task. The
        # sync emitter (:func:`maybe_emit_sync_completion`) gates the
        # same way.
        if method_name not in SPEC_WEBHOOK_TASK_TYPES:
            return

        config = getattr(params, "push_notification_config", None)
        if config is None and isinstance(params, dict):
            config = params.get("push_notification_config")
        if config is None:
            return  # buyer didn't register — polling-only, nothing to do

        if target is None:
            # Buyer registered a push config but no sender / supervisor is
            # wired. Without this branch the terminal notification quietly
            # disappears — surfacing a warning gives the adopter a fast
            # path to the misconfig (mirrors the sync gate).
            try:
                url_for_log = getattr(config, "url", None)
                if url_for_log is None and isinstance(config, dict):
                    url_for_log = config.get("url")
            except Exception:
                url_for_log = None
            logger.warning(
                "[adcp.decisioning] buyer registered push_notification_config "
                "(url=%s) for async %s (task_id=%s) but neither webhook_sender "
                "nor webhook_supervisor is wired — terminal %s webhook silently "
                "dropped. Pass one to "
                "adcp.decisioning.serve.create_adcp_server_from_platform.",
                url_for_log if url_for_log else "<unextractable>",
                method_name,
                task_id,
                status,
            )
            return

        extracted = _extract_push_notification_url_and_token(params)
        if extracted is None:
            return
        url, token = extracted
        operation_id = _extract_push_operation_id(params)

        # Defense-in-depth: strip credentials from the artifact BEFORE the
        # webhook target sees it. The dispatcher already strips before
        # persisting to the registry (:func:`_project_handoff`); this is a
        # second pass at the delivery boundary. Method-gated — non-account
        # tools short-circuit without walking the result. Failure payloads
        # (error wire dicts) never carry credentials but pass through the
        # same gate harmlessly.
        if result is not None:
            result = strip_credentials_from_wire_result(method_name, result)

        await target.send_mcp(
            url=url,
            task_id=task_id,
            status=status,
            task_type=method_name,
            result=result,
            operation_id=operation_id,
            token=token,
        )
    except Exception:
        # Logged-and-swallowed: the background task's terminal state is
        # already recorded in the registry; the buyer can read it via
        # tasks/get regardless of webhook delivery outcome.
        logger.warning(
            "[adcp.decisioning] terminal %s webhook for async %s "
            "(task_id=%s) failed; registry terminal state already recorded",
            status,
            method_name,
            task_id,
            exc_info=True,
        )


def validate_webhook_sender_for_platform(
    *,
    advertised_tools: frozenset[str] | set[str],
    sender: Any,
    auto_emit: bool,
    supervisor: Any = None,
) -> None:
    """Validate explicit legacy sync-completion compatibility wiring.

    When an adopter explicitly enables compatibility mode and claims a
    specialism whose tool surface includes
    any spec-eligible webhook task type (e.g., ``create_media_buy``,
    ``activate_signal``, ``acquire_rights``) AND auto-emit is on AND
    neither ``webhook_sender`` nor ``webhook_supervisor`` is wired,
    every buyer who registers ``push_notification_config.url`` would
    have their notification silently dropped. The runtime gate at
    :func:`maybe_emit_sync_completion` warns on the FIRST call, but
    by then the buyer has already burned a request and the adopter
    has shipped without webhook wiring.

    This validator surfaces the misconfig at server boot — same
    posture as ``dispatch.validate_platform``'s governance opt-in
    gate. Keeps the runtime warning as the second line of defense
    (covers tool surfaces that can't be statically resolved).

    :raises AdcpError: ``code='INVALID_REQUEST'`` when the
        configuration would silently drop webhooks. Matches the
        exception class :func:`validate_platform` raises for sibling
        boot-time misconfigs (governance opt-in, missing required
        methods) so adopter ``except AdcpError`` clauses catch all
        platform-config failures uniformly.
    """
    if not auto_emit:
        return
    if sender is not None or supervisor is not None:
        return
    eligible = SPEC_WEBHOOK_TASK_TYPES & set(advertised_tools)
    if not eligible:
        return
    from adcp.decisioning.types import AdcpError

    raise AdcpError(
        "INVALID_REQUEST",
        message=(
            "legacy auto_emit_completion_webhooks compatibility mode is "
            "enabled and the platform's "
            "claimed specialisms expose webhook-eligible tools "
            f"{sorted(eligible)!r}, but neither webhook_sender nor "
            "webhook_supervisor was wired. Buyers who register "
            "push_notification_config.url on these tools would have their "
            "notifications silently dropped. Pass a configured "
            "WebhookSender (transport only) or InMemoryWebhookDeliverySupervisor "
            "(retry + circuit breaker) to "
            "adcp.decisioning.serve.create_adcp_server_from_platform, "
            "or disable auto_emit_completion_webhooks. This compatibility "
            "mode is non-conformant for synchronous terminal responses."
        ),
        recovery="terminal",
        details={
            "missing": "webhook_sender_or_supervisor",
            "webhook_eligible_tools": sorted(eligible),
        },
    )


def validate_webhook_signing_for_capabilities(
    *,
    capabilities: DecisioningCapabilities,
    sender: WebhookSender | None,
    supervisor: WebhookDeliverySupervisor | None = None,
) -> None:
    """Server-boot fail-fast for the #384 capabilities-vs-wiring invariant.

    When the platform's :class:`DecisioningCapabilities` declares
    ``webhook_signing.supported=True``, the AdCP capabilities schema
    binds the seller to producing RFC 9421 ``Signature`` headers on
    EVERY outbound webhook — the schema description on the ``supported``
    field reads "When false or absent, ... receivers MUST NOT expect a
    Signature header," so by contrapositive when ``true`` they MUST.
    There is no per-delivery opt-out in AdCP 3.x; ``legacy_hmac_fallback``
    is a downgrade switch for receivers that have NOT adopted RFC 9421,
    not a substitute for the seller's RFC 9421 capability.

    The wired :class:`~adcp.webhook_sender.WebhookSender` MUST therefore
    be configured with a JWK signing key whose ``alg`` is also present
    in the advertised ``algorithms`` list. A bearer-only or HMAC sender,
    or a JWK sender whose alg is not advertised, would emit deliveries
    that conformant verifiers reject — silent blackout for any buyer
    enforcing RFC 9421.

    The check keys on the capability advertisement, not on
    ``reporting_delivery_methods=["webhook"]``: 3.x explicitly permits
    HMAC/Bearer-only delivery via ``legacy_hmac_fallback``, so the
    delivery-method axis is a poor gate. ``webhook_signing.supported``
    is the self-consistency contract the spec supports directly.

    Sender resolution: this validator introspects the supervisor's
    ``_sender`` attribute when ``sender`` is ``None`` — both
    :class:`~adcp.webhook_supervisor.InMemoryWebhookDeliverySupervisor`
    and :class:`~adcp.webhook_supervisor_pg.PgWebhookDeliverySupervisor`
    expose it. Custom Protocol-only supervisors without an
    introspectable sender log a WARNING and skip validation; operators
    wiring those impls own the contract themselves but the gap is
    observable in boot logs.

    :raises AdcpError: ``code='INVALID_REQUEST'`` when capabilities
        declare RFC 9421 signing support but no sender (or a non-JWK
        sender, or a JWK sender whose alg doesn't match the advertised
        algorithms) is wired. Matches the recovery posture of sibling
        boot-time validators (terminal).
    """
    webhook_signing = getattr(capabilities, "webhook_signing", None)
    if webhook_signing is None or not getattr(webhook_signing, "supported", False):
        return

    adopter_managed = getattr(capabilities, "webhook_signing_managed_externally", False)

    from adcp.decisioning.types import AdcpError

    if not isinstance(adopter_managed, bool):
        raise AdcpError(
            "INVALID_REQUEST",
            message=(
                "DecisioningCapabilities.webhook_signing_managed_externally "
                "must be a bool. Non-bool values are rejected so a mistyped "
                "configuration cannot bypass SDK webhook-signing validation."
            ),
            recovery="terminal",
            details={
                "field": "webhook_signing_managed_externally",
                "value_type": type(adopter_managed).__name__,
            },
        )

    if adopter_managed is True and sender is None and supervisor is None:
        logger.info(
            "[adcp.decisioning] capabilities.webhook_signing.supported=True "
            "and DecisioningCapabilities.webhook_signing_managed_externally=True; "
            "skipping SDK WebhookSender validation. Operator owns the RFC 9421 "
            "delivery contract for outbound webhooks."
        )
        return

    resolved_sender: Any = sender
    if resolved_sender is None and supervisor is not None:
        # Both reference supervisors store the underlying WebhookSender
        # on ``_sender``. Custom Protocol-only impls (Celery/Kafka
        # queue-only adopters) may not — log a WARNING so the gap is
        # observable in boot logs, then skip rather than fail-noisy on
        # an unknowable structure.
        resolved_sender = getattr(supervisor, "_sender", None)
        if resolved_sender is None:
            logger.warning(
                "[adcp.decisioning] capabilities.webhook_signing.supported=True "
                "but supervisor %s has no introspectable _sender attribute; "
                "boot validator cannot verify the wired sender produces RFC 9421 "
                "headers. Operator owns the contract — confirm out-of-band that "
                "outbound deliveries from this supervisor carry Signature / "
                "Signature-Input.",
                type(supervisor).__name__,
            )
            return

    if resolved_sender is None:
        raise AdcpError(
            "INVALID_REQUEST",
            message=(
                "capabilities.webhook_signing.supported=True declares this "
                "platform signs outbound webhooks per RFC 9421, but neither "
                "webhook_sender nor webhook_supervisor was wired. Buyers "
                "enforcing RFC 9421 verification on inbound webhooks would "
                "see every delivery from this seller fail signature check. "
                "Either wire a WebhookSender via WebhookSender.from_jwk(...) "
                "or WebhookSender.from_pem(...), or remove "
                "webhook_signing.supported from the capabilities declaration."
            ),
            recovery="terminal",
            details={
                "missing": "webhook_sender_with_rfc9421_key",
                "capabilities_webhook_signing_supported": True,
            },
        )

    if not getattr(resolved_sender, "signs_with_rfc9421", False):
        raise AdcpError(
            "INVALID_REQUEST",
            message=(
                "capabilities.webhook_signing.supported=True declares this "
                "platform signs outbound webhooks per RFC 9421, but the "
                "wired WebhookSender is not configured for JWK signing "
                "(bearer-token, AdCP-legacy HMAC, and Standard-Webhooks "
                "HMAC senders do not produce RFC 9421 Signature / "
                "Signature-Input headers). Reconstruct the sender via "
                "WebhookSender.from_jwk(...) or WebhookSender.from_pem(...), "
                "or remove webhook_signing.supported from the capabilities "
                "declaration if this seller does not in fact sign per "
                "RFC 9421."
            ),
            recovery="terminal",
            details={
                "missing": "webhook_sender_with_rfc9421_key",
                "capabilities_webhook_signing_supported": True,
                "sender_auth_mode": type(getattr(resolved_sender, "_auth", None)).__name__,
            },
        )

    # Cross-check the wired sender's signature algorithm against the
    # advertised set. A seller declaring ``algorithms=["ed25519"]`` and
    # wiring an ES256 sender would emit deliveries pinned verifiers
    # reject — same silent-blackout failure mode the supported-check
    # closes, one axis deeper. ``algorithms`` is optional on the wire;
    # skip the cross-check when omitted (no advertisement to violate).
    advertised_algorithms = getattr(webhook_signing, "algorithms", None)
    if advertised_algorithms:
        sender_alg = getattr(getattr(resolved_sender, "_auth", None), "alg", None)
        advertised_alg_values = [getattr(a, "value", a) for a in advertised_algorithms]
        if sender_alg not in advertised_alg_values:
            raise AdcpError(
                "INVALID_REQUEST",
                message=(
                    "capabilities.webhook_signing.algorithms advertises "
                    f"{advertised_alg_values!r} but the wired WebhookSender "
                    f"signs with {sender_alg!r}. Buyers pinning their RFC 9421 "
                    "verifier to the advertised algorithms reject every "
                    "delivery whose Signature-Input ``alg=`` is outside the "
                    "set. Align the sender's alg with the capability "
                    "declaration, or widen ``algorithms`` to include the "
                    "sender's value."
                ),
                recovery="terminal",
                details={
                    "missing": "webhook_signing_algorithm_alignment",
                    "advertised_algorithms": advertised_alg_values,
                    "sender_alg": sender_alg,
                },
            )


__all__ = [
    "SPEC_WEBHOOK_TASK_TYPES",
    "emit_terminal_completion_webhook",
    "maybe_emit_sync_completion",
    "validate_webhook_sender_for_platform",
    "validate_webhook_signing_for_capabilities",
]
