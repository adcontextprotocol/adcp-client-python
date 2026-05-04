"""Auto-emit completion webhook on sync-success arm of mutating tools.

When a buyer supplies ``push_notification_config.url`` on a request and
the seller answers via the sync fast path (NOT a :class:`TaskHandoff`),
the framework fires a completion webhook to that URL after the response
so buyers get consistent notification regardless of how the seller
routed the call. Without this, a buyer registering a webhook URL would
get notifications only on the HITL path — sync responses would leave
them polling.

Mirrors the JS-side ``emitSyncCompletionWebhook`` at
``src/lib/server/decisioning/runtime/from-platform.ts`` (commits
``8dc427f9`` and ``7a887dfa``). Wire-format is identical: same
``task_type``, ``status: 'completed'``, ``result`` field carrying the
projected sync response, and an echoed ``token`` if the buyer
registered one. ``task_id`` is synthesized as ``f"sync-{uuid4()}"``
since sync responses don't allocate a registry task; buyers correlate
via the resource ids embedded in ``result``.

**Fire-and-forget.** Webhook delivery runs in a background asyncio
task; the sync response returns inline immediately. A buyer-supplied
slowloris webhook URL must not be able to hold the seller's request
worker for the full retry budget — the JS round-2 fix (``7a887dfa``)
addressed this DoS vector and Python preserves the same posture.
``_BACKGROUND_WEBHOOK_TASKS`` strong-refs in-flight emissions so the
asyncio loop's weak-ref behavior doesn't garbage-collect them
mid-flight.

**Spec gate.** Only tools in :data:`SPEC_WEBHOOK_TASK_TYPES` (the
closed 20-value enum from ``schemas/cache/enums/task-type.json``)
emit. Spec-validating webhook receivers reject envelopes with
non-spec ``task_type`` values; tools the framework dispatches that
aren't in the spec enum (adopter-only specialism methods) skip
delivery and rely on ``publishStatusChange`` for state updates.

Adopters who emit webhooks manually inside their handlers pass
``auto_emit_completion_webhooks=False`` to
:func:`adcp.decisioning.serve` to avoid duplicate delivery.
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
        "sync_creatives",
        "activate_signal",
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

    * ``enabled`` is False (operator opted out).
    * The request didn't carry ``push_notification_config.url``.

    Logs a WARNING when:

    * ``sender`` is None but the buyer DID register
      ``push_notification_config.url`` — the buyer's notification
      registration is being silently dropped, which the adopter
      almost certainly didn't intend. Wire ``webhook_sender`` into
      :func:`adcp.decisioning.serve` or pass
      ``auto_emit_completion_webhooks=False`` to silence this.
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
                "or set auto_emit_completion_webhooks=False to silence "
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
                "tool not in spec task-type enum (closed 20-value set per "
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


def validate_webhook_sender_for_platform(
    *,
    advertised_tools: frozenset[str] | set[str],
    sender: Any,
    auto_emit: bool,
    supervisor: Any = None,
) -> None:
    """Server-boot fail-fast for the F12 misconfig (Emma sales-direct
    P0 root cause).

    When an adopter claims a specialism whose tool surface includes
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
            "auto_emit_completion_webhooks is enabled and the platform's "
            "claimed specialisms expose webhook-eligible tools "
            f"{sorted(eligible)!r}, but neither webhook_sender nor "
            "webhook_supervisor was wired. Buyers who register "
            "push_notification_config.url on these tools would have their "
            "notifications silently dropped. "
            "Wire a sender before calling serve():\n\n"
            "  # JWK signing (RFC 9421, spec-conformant):\n"
            "  from adcp.webhook_sender import WebhookSender\n"
            "  sender = WebhookSender.from_jwk(my_jwk)\n\n"
            "  # Bearer token (simplest — gateway-validated):\n"
            "  sender = WebhookSender.from_bearer_token('my-token')\n\n"
            "  # Standard Webhooks / Svix / Resend:\n"
            "  sender = WebhookSender.from_standard_webhooks_secret('whsec_...', key_id='wh-1')\n\n"
            "  # With retry + circuit breaker:\n"
            "  from adcp.webhook_supervisor import InMemoryWebhookDeliverySupervisor\n"
            "  supervisor = InMemoryWebhookDeliverySupervisor(sender)\n"
            "  serve(platform, webhook_supervisor=supervisor)\n\n"
            "Or set auto_emit_completion_webhooks=False if you handle "
            "webhooks manually inside your platform methods. "
            "See docs/handler-authoring.md#webhooks for wiring recipes."
        ),
        recovery="terminal",
        details={
            "missing": "webhook_sender_or_supervisor",
            "webhook_eligible_tools": sorted(eligible),
        },
    )


__all__ = [
    "SPEC_WEBHOOK_TASK_TYPES",
    "maybe_emit_sync_completion",
    "validate_webhook_sender_for_platform",
]
