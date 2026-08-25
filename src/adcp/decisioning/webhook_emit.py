"""Task-webhook delivery support.

AdCP task webhooks describe status changes after the initial response.
When that response is already terminal, the buyer has the result inline
and no task webhook is emitted. The deprecated
``auto_emit_completion_webhooks`` option is accepted for source
compatibility but ignored; enabling it emits a deprecation warning.

Async :class:`TaskHandoff` completion and failure webhooks require an atomic
terminal-state/outbox publisher. The SDK currently rejects framework-managed
push for that path; an external publisher may own it explicitly.
"""

from __future__ import annotations

import asyncio
import logging
import warnings
from typing import TYPE_CHECKING, Any, TypeAlias

from adcp.decisioning.account_projection import (
    strip_credentials_from_wire_result,
)

if TYPE_CHECKING:
    from adcp.decisioning.platform import DecisioningCapabilities
    from adcp.webhook_sender import WebhookSender
    from adcp.webhook_supervisor import WebhookDeliverySupervisor

    DeliveryTarget: TypeAlias = WebhookSender | WebhookDeliverySupervisor

logger = logging.getLogger(__name__)


def _sdk_task_outbox_pair_ready(registry: Any, task_outbox: Any) -> bool:
    """Accept only the exact registry/outbox types whose atomicity we own.

    Subclasses are deliberately rejected.  The callback-registration and
    terminal-enqueue methods are load-bearing parts of the audited contract;
    an override can silently discard webhook arguments or bypass the shared
    transaction while still passing an ``isinstance`` check.
    """
    if registry is None or task_outbox is None:
        return False
    try:
        from adcp.decisioning.pg.task_registry import PgTaskRegistry
        from adcp.decisioning.pg.task_webhook_outbox import PgTaskWebhookOutbox
    except ImportError:
        return False
    return (
        type(registry) is PgTaskRegistry
        and type(task_outbox) is PgTaskWebhookOutbox
        and registry.task_webhook_outbox is task_outbox
        and registry._pool is task_outbox._pool
    )


#: Tools eligible for asynchronous task webhooks. Mirrors the closed enum in
#: ``schemas/cache/enums/task-type.json`` verbatim. The framework dispatches a
#: wider tool surface than this set; the JS side maintains the same set at
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
        "buy_products",
        "accept_proposal",
        "control_media_buy",
        "media_buy_delivery",
        "build_creative",
        "preview_creative",
        "sync_creatives",
        "activate_signal",
        "get_products",
        "request_proposals",
        "refine_proposals",
        "decline_proposals",
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
        "update_rights",
        "sync_agent_notification_configs",
    }
)


#: Deprecated compatibility surface retained until the next major release.
#: Synchronous terminal responses no longer schedule tasks into this set.
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


def maybe_emit_sync_completion(
    *,
    sender: WebhookSender | None,
    enabled: bool,
    method_name: str,
    params: Any,
    result: Any,
    supervisor: WebhookDeliverySupervisor | None = None,
) -> None:
    """Preserve the retired option without emitting a task webhook.

    AdCP 3.2 requires an inline terminal response to remain silent on the
    task-webhook channel. Callers that still pass ``enabled=True`` receive a
    deprecation warning and must return a real :class:`TaskHandoff` when
    callback delivery is required.
    """
    del sender, method_name, params, result, supervisor
    if enabled:
        warnings.warn(
            "auto_emit_completion_webhooks is ignored under AdCP 3.2: an "
            "inline terminal response MUST remain silent on the task-webhook "
            "channel. Return a real async TaskHandoff when callback delivery "
            "is required.",
            DeprecationWarning,
            stacklevel=2,
        )
    return


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
        if operation_id is None:
            raise ValueError("push_notification_config.operation_id is required for task webhooks")

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
    """Accept the retired option without imposing sync-webhook wiring."""
    del advertised_tools, sender, auto_emit, supervisor


def validate_webhook_signing_for_capabilities(
    *,
    capabilities: DecisioningCapabilities,
    sender: WebhookSender | None,
    supervisor: WebhookDeliverySupervisor | None = None,
    auto_emit_task_webhooks: bool = True,
    registry: Any = None,
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

    SDK senders and supervisors are inspected to provide precise diagnostics.
    Framework-managed publication additionally requires a
    ``PgTaskWebhookOutbox`` attached to the task registry; external publishers
    declare external ownership and disable SDK automatic emission.

    :raises AdcpError: ``code='INVALID_REQUEST'`` when capabilities
        declare RFC 9421 signing support but no sender (or a non-JWK
        sender, or a JWK sender whose alg doesn't match the advertised
        algorithms) is wired. Matches the recovery posture of sibling
        boot-time validators (terminal).
    """
    adopter_managed = getattr(capabilities, "webhook_signing_managed_externally", False)
    task_outbox = getattr(registry, "task_webhook_outbox", None)

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

    webhook_signing = getattr(capabilities, "webhook_signing", None)
    if webhook_signing is None or not getattr(webhook_signing, "supported", False):
        if adopter_managed is True:
            raise AdcpError(
                "INVALID_REQUEST",
                message=(
                    "webhook_signing_managed_externally=True requires "
                    "capabilities.webhook_signing.supported=True and an "
                    "advertised delivery_retry_horizon_seconds"
                ),
                recovery="terminal",
                details={"missing": "webhook_signing.supported"},
            )
        return

    retry_horizon = getattr(webhook_signing, "delivery_retry_horizon_seconds", None)
    if type(retry_horizon) is not int or not 86400 <= retry_horizon <= 604800:
        raise AdcpError(
            "INVALID_REQUEST",
            message=(
                "capabilities.webhook_signing.supported=True on an AdCP 3.2 "
                "publisher requires delivery_retry_horizon_seconds. Receivers "
                "use this advertised window to retain immutable delivery-key "
                "bindings and publication proof. Declare a value from 86400 "
                "through 604800 seconds that your delivery system can honor."
            ),
            recovery="terminal",
            details={
                "missing": "webhook_signing.delivery_retry_horizon_seconds",
                "capabilities_webhook_signing_supported": True,
            },
        )

    if adopter_managed is True:
        if task_outbox is not None:
            raise AdcpError(
                "INVALID_REQUEST",
                message=(
                    "webhook_signing_managed_externally=True conflicts with the "
                    "PgTaskRegistry task_webhook_outbox; choose exactly one owner"
                ),
                recovery="terminal",
                details={"missing": "single_task_webhook_owner"},
            )
        if auto_emit_task_webhooks:
            raise AdcpError(
                "INVALID_REQUEST",
                message=(
                    "webhook_signing_managed_externally=True requires "
                    "auto_emit_task_webhooks=False so the SDK cannot race the "
                    "adopter's durable outbox"
                ),
                recovery="terminal",
                details={"missing": "external_task_webhook_ownership"},
            )
        if sender is not None or supervisor is not None:
            raise AdcpError(
                "INVALID_REQUEST",
                message=(
                    "Externally managed webhook publication must not wire an "
                    "SDK webhook_sender or webhook_supervisor into the automatic "
                    "TaskHandoff path"
                ),
                recovery="terminal",
                details={"missing": "unwired_sdk_webhook_target"},
            )
        logger.info(
            "[adcp.decisioning] capabilities.webhook_signing.supported=True "
            "and DecisioningCapabilities.webhook_signing_managed_externally=True; "
            "skipping SDK WebhookSender validation. Operator owns the RFC 9421 "
            "delivery contract for outbound webhooks."
        )
        return

    internal_outbox_ready = _sdk_task_outbox_pair_ready(registry, task_outbox)
    if task_outbox is not None and not internal_outbox_ready:
        raise AdcpError(
            "INVALID_REQUEST",
            message=(
                "SDK-managed task webhook publication requires the concrete "
                "PgTaskRegistry/PgTaskWebhookOutbox pair using the same pool; "
                "custom publishers must use webhook_signing_managed_externally=True"
            ),
            recovery="terminal",
            details={"missing": "verified_sdk_task_webhook_outbox_pair"},
        )
    if internal_outbox_ready:
        if sender is not None or supervisor is not None:
            raise AdcpError(
                "INVALID_REQUEST",
                message=(
                    "A registry-backed task_webhook_outbox is the sole SDK "
                    "delivery owner; do not also wire webhook_sender or "
                    "webhook_supervisor into the handler"
                ),
                recovery="terminal",
                details={"missing": "single_sdk_task_webhook_owner"},
            )
        if not auto_emit_task_webhooks:
            raise AdcpError(
                "INVALID_REQUEST",
                message=(
                    "A registry-backed task_webhook_outbox requires "
                    "auto_emit_task_webhooks=True; False declares external ownership"
                ),
                recovery="terminal",
                details={"missing": "sdk_task_webhook_ownership"},
            )
        outbox_horizon = getattr(task_outbox, "delivery_retry_horizon_seconds", None)
        if (
            getattr(task_outbox, "supports_atomic_task_outbox", False) is not True
            or getattr(task_outbox, "delivery_state_is_durable", False) is not True
            or type(outbox_horizon) is not int
            or outbox_horizon != retry_horizon
        ):
            raise AdcpError(
                "INVALID_REQUEST",
                message=(
                    "The registry task_webhook_outbox does not prove atomic durable "
                    "publication for the advertised retry horizon"
                ),
                recovery="terminal",
                details={
                    "missing": "atomic_durable_webhook_outbox",
                    "advertised_horizon_seconds": retry_horizon,
                    "outbox_horizon_seconds": outbox_horizon,
                },
            )

    outbox_sender_resolver = (
        getattr(task_outbox, "_sender_resolver", None) if internal_outbox_ready else None
    )
    resolved_sender: Any = (
        getattr(task_outbox, "_sender", None) if internal_outbox_ready else sender
    )
    # Tenant-aware outboxes resolve and validate the active RFC 9421 sender
    # on every attempt. There is intentionally no single boot-time key or
    # algorithm to introspect because rotation is part of the contract.
    sender_introspectable = outbox_sender_resolver is None
    if resolved_sender is None and supervisor is not None and not internal_outbox_ready:
        # Both reference supervisors store the underlying WebhookSender
        # on ``_sender``. Custom Protocol-only impls (Celery/Kafka
        # queue-only adopters) may not. Their supervisor must still expose the
        # durable-delivery contract checked below; log that signature bytes
        # cannot be inspected, but do not bypass the retention check.
        resolved_sender = getattr(supervisor, "_sender", None)
        if resolved_sender is None:
            sender_introspectable = False
            logger.warning(
                "[adcp.decisioning] capabilities.webhook_signing.supported=True "
                "but supervisor %s has no introspectable _sender attribute; "
                "boot validator cannot verify the wired sender produces RFC 9421 "
                "headers. Operator owns the contract — confirm out-of-band that "
                "outbound deliveries from this supervisor carry Signature / "
                "Signature-Input.",
                type(supervisor).__name__,
            )

    if resolved_sender is None and sender_introspectable:
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

    if sender_introspectable and not getattr(resolved_sender, "signs_with_rfc9421", False):
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
    if advertised_algorithms and sender_introspectable:
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

    if internal_outbox_ready:
        return

    raise AdcpError(
        "INVALID_REQUEST",
        message=(
            "No atomic terminal-state/outbox publisher is configured for the "
            "AdCP 3.2 retry-horizon contract. Attach PgTaskWebhookOutbox to "
            "PgTaskRegistry, or set webhook_signing_managed_externally=True "
            "with auto_emit_task_webhooks=False when adopter infrastructure "
            "owns atomic publication and reconciliation."
        ),
        recovery="terminal",
        details={"missing": "external_durable_webhook_outbox"},
    )


def external_task_webhook_owner_ready(
    *,
    capabilities: DecisioningCapabilities,
    sender: WebhookSender | None,
    supervisor: WebhookDeliverySupervisor | None,
    auto_emit_task_webhooks: bool,
) -> bool:
    """Return whether an external outbox may own TaskHandoff push delivery.

    This is deliberately stricter than checking ``auto_emit_task_webhooks``:
    disabling the SDK emitter alone does not prove that anybody will publish
    the promised terminal webhook.
    """
    webhook_signing = getattr(capabilities, "webhook_signing", None)
    retry_horizon = getattr(webhook_signing, "delivery_retry_horizon_seconds", None)
    return (
        auto_emit_task_webhooks is False
        and sender is None
        and supervisor is None
        and getattr(capabilities, "webhook_signing_managed_externally", False) is True
        and webhook_signing is not None
        and getattr(webhook_signing, "supported", False) is True
        and type(retry_horizon) is int
        and 86400 <= retry_horizon <= 604800
    )


def task_webhook_owner_ready(
    *,
    capabilities: DecisioningCapabilities,
    sender: WebhookSender | None,
    supervisor: WebhookDeliverySupervisor | None,
    auto_emit_task_webhooks: bool,
    registry: Any = None,
) -> bool:
    """Return whether either the SDK atomic outbox or an external owner is ready."""
    if external_task_webhook_owner_ready(
        capabilities=capabilities,
        sender=sender,
        supervisor=supervisor,
        auto_emit_task_webhooks=auto_emit_task_webhooks,
    ):
        return True

    if getattr(capabilities, "webhook_signing_managed_externally", False) is not False:
        return False
    webhook_signing = getattr(capabilities, "webhook_signing", None)
    advertised_horizon = getattr(webhook_signing, "delivery_retry_horizon_seconds", None)
    outbox = getattr(registry, "task_webhook_outbox", None)
    outbox_horizon = getattr(outbox, "delivery_retry_horizon_seconds", None)
    outbox_sender = getattr(outbox, "_sender", None)
    outbox_sender_resolver = getattr(outbox, "_sender_resolver", None)
    return (
        auto_emit_task_webhooks is True
        and sender is None
        and supervisor is None
        and _sdk_task_outbox_pair_ready(registry, outbox)
        and webhook_signing is not None
        and getattr(webhook_signing, "supported", False) is True
        and type(advertised_horizon) is int
        and getattr(outbox, "supports_atomic_task_outbox", False) is True
        and getattr(outbox, "delivery_state_is_durable", False) is True
        and type(outbox_horizon) is int
        and outbox_horizon == advertised_horizon
        and (
            getattr(outbox_sender, "signs_with_rfc9421", False) is True
            or outbox_sender_resolver is not None
        )
    )


__all__ = [
    "SPEC_WEBHOOK_TASK_TYPES",
    "emit_terminal_completion_webhook",
    "external_task_webhook_owner_ready",
    "task_webhook_owner_ready",
    "maybe_emit_sync_completion",
    "validate_webhook_sender_for_platform",
    "validate_webhook_signing_for_capabilities",
]
