"""Rejection guards for async (handoff) discovery on get_products / get_signals.

The rc.9 spec admits async discovery for ``buying_mode='brief'|'refine'``
(get_products) and ``discovery_mode='brief'`` (get_signals): the seller MAY
return a ``submitted`` envelope and the buyer polls ``tasks/get`` for the
terminal catalog. The spec is equally explicit about what async discovery
MUST NOT do, and these guards enforce those MUST-NOTs at the framework seam
so adopters can't ship a non-conformant async discovery surface by accident.

Four guards, all projecting
``AdcpError('INVALID_REQUEST', recovery='correctable')``:

(a) **Wholesale + push pre-dispatch reject.** ``buying_mode='wholesale'`` /
    ``discovery_mode='wholesale'`` is a raw rate-card read with no async
    lifecycle. The spec: *"agents MUST NOT route a 'wholesale' request
    through the async/Submitted arm or emit async delivery solely because
    push_notification_config is present."* A wholesale request carrying
    ``push_notification_config`` is malformed — reject BEFORE invoking the
    platform method (``field='push_notification_config'``). Lives next to
    ``assert_buying_mode_consistent`` in :mod:`adcp.decisioning.refine`
    (products) and is called from the get_signals shim (signals).

(b) **Wholesale + adopter handoff post-dispatch reject.** Belt-and-braces
    for (a): even with no push config, an adopter whose wholesale code path
    returns ``ctx.handoff_to_task(fn)`` violates the wholesale-is-sync
    contract. The handoff has already been projected to a ``submitted``
    dict by dispatch; reject it (``field='buying_mode'`` / ``'discovery_mode'``)
    so the buyer never sees a wholesale task_id.

(c) **Async + unresolved account reject.** An async discovery task is
    addressable only by ``task_id`` scoped to a resolved account — the
    registry issues against ``ctx.account.id`` and ``tasks/get`` is
    account-scoped. If the request entered the async path (the adopter
    handed off, OR the buyer supplied ``push_notification_config``) but the
    account resolved to the sentinel/empty id, the task would be
    unreachable. Reject (``field='account'``). Mirrors JS #2170's
    accountless-async rejection.

(d) **Hand-rolled submitted reject.** An adopter who returns a literal
    ``{'status': 'submitted', 'task_id': ...}`` dict from the sync arm —
    instead of ``ctx.handoff_to_task(fn)`` — bypasses the framework's task
    registry: no row is issued, ``tasks/get`` 404s, no completion webhook
    fires. The dispatch handoff projection emits the EXACT 2-key dict
    ``{'task_id', 'status'}``; a hand-rolled submitted carries either extra
    keys or a task_id the registry never minted. Raise a guiding error
    pointing the adopter at ``ctx.handoff_to_task``.

Asymmetry: products' mode field is ``buying_mode`` with values
{brief, wholesale, refine}; signals' mode field is ``discovery_mode`` with
values {brief, wholesale}. The guards take the field name as a parameter so
the same logic serves both verbs.
"""

from __future__ import annotations

from typing import Any

from adcp.decisioning.types import AdcpError


def _coerce_mode(req: Any, mode_field: str) -> str | None:
    """Read ``req.<mode_field>`` as a plain string (enum or str, None passthrough)."""
    value = getattr(req, mode_field, None)
    if value is None:
        return None
    return value.value if hasattr(value, "value") else str(value)


def _has_push_config(req: Any) -> bool:
    """True when the request carries a non-empty ``push_notification_config``."""
    config = getattr(req, "push_notification_config", None)
    if config is None and isinstance(req, dict):
        config = req.get("push_notification_config")
    return config is not None


def _is_submitted_projection(result: Any) -> bool:
    """True for the framework's handoff projection — the exact 2-key
    ``{'task_id', 'status': 'submitted'}`` dict emitted by
    :func:`adcp.decisioning.dispatch._project_handoff`."""
    return (
        isinstance(result, dict)
        and set(result.keys()) == {"task_id", "status"}
        and result.get("status") == "submitted"
    )


def assert_discovery_push_consistent(req: Any, *, mode_field: str) -> None:
    """Guard (a): pre-dispatch reject of wholesale + push_notification_config.

    ``mode_field`` is ``'buying_mode'`` (get_products) or ``'discovery_mode'``
    (get_signals). Wholesale is a synchronous rate-card read; a wholesale
    request carrying ``push_notification_config`` asks for an async
    delivery channel the verb does not offer in that mode. Reject before
    the platform method runs (``field='push_notification_config'``).

    :raises AdcpError: ``INVALID_REQUEST`` / ``correctable`` when mode is
        wholesale AND push_notification_config is present.
    """
    if _coerce_mode(req, mode_field) == "wholesale" and _has_push_config(req):
        raise AdcpError(
            "INVALID_REQUEST",
            message=(
                f"{mode_field}='wholesale' must not carry "
                "push_notification_config. Wholesale discovery is a "
                "synchronous rate-card read with no async lifecycle — the "
                "spec forbids routing it through the submitted arm or "
                "emitting async delivery because a push config is present. "
                "Drop push_notification_config, or use the 'brief' mode "
                "which supports the async (submitted) arm."
            ),
            field="push_notification_config",
            recovery="correctable",
        )


def reject_wholesale_handoff(result: Any, *, mode: str | None, mode_field: str) -> None:
    """Guard (b): post-dispatch reject of an adopter handoff on a wholesale call.

    Called after ``_invoke_platform_method`` with the (already projected)
    result. When the resolved mode is wholesale and the result is the
    framework's submitted projection, the adopter's wholesale code path
    handed off — a contract violation. Reject (``field=<mode_field>``).

    :param mode: The request's coerced mode string.
    :raises AdcpError: ``INVALID_REQUEST`` / ``correctable``.
    """
    if mode == "wholesale" and _is_submitted_projection(result):
        raise AdcpError(
            "INVALID_REQUEST",
            message=(
                f"{mode_field}='wholesale' returned a task handoff, but "
                "wholesale discovery MUST complete synchronously. Return the "
                "catalog directly; signal partial completion via the "
                "response's incomplete[] field rather than handing off to a "
                "background task. The 'brief' mode is the async-capable path."
            ),
            field=mode_field,
            recovery="correctable",
        )


def assert_account_resolved_for_async(
    result: Any,
    *,
    account_id: str | None,
    has_push: bool,
) -> None:
    """Guard (c): reject an async discovery call against an unresolved account.

    An async discovery task is addressable only by a ``task_id`` scoped to a
    resolved account. The request is "async" when EITHER the adopter handed
    off (the result is the framework's submitted projection) OR the buyer
    supplied ``push_notification_config``. ``account_id`` is "unresolved"
    when it is empty/whitespace or the ``'<unset>'`` sentinel — matching
    :func:`adcp.decisioning.dispatch.compose_caller_identity`'s contract
    across derived / implicit / explicit account modes.

    Call this both pre-dispatch (with ``result=None``) when the buyer
    supplied ``push_notification_config`` — so the rejection fires as a
    clean ``INVALID_REQUEST`` before any platform / registry interaction —
    and post-dispatch (passing the projected ``result``) so an adopter
    handoff against an unresolved account is also caught. The framework's
    registry independently rejects an empty ``account_id`` at issue-time;
    this guard surfaces the buyer-facing ``field='account'`` diagnostic.

    :raises AdcpError: ``INVALID_REQUEST`` / ``correctable`` with
        ``field='account'``.
    """
    is_async = has_push or _is_submitted_projection(result)
    if not is_async:
        return
    if _account_resolved(account_id):
        return
    raise AdcpError(
        "INVALID_REQUEST",
        message=(
            "Async discovery requires a resolved account. This request "
            "entered the async path (the seller handed off, or the request "
            "carried push_notification_config) but no account resolved — the "
            "submitted task_id would be unreachable via tasks/get, and a "
            "push callback could not be scoped. Supply an account reference "
            "(or authenticate so the account store can derive one), or use a "
            "synchronous discovery call."
        ),
        field="account",
        recovery="correctable",
    )


def _account_resolved(account_id: str | None) -> bool:
    """False when ``account_id`` is empty/whitespace or the ``'<unset>'`` sentinel."""
    if not account_id or not account_id.strip():
        return False
    return account_id != "<unset>"


def reject_hand_rolled_submitted(result: Any) -> None:
    """Guard (d): reject a literal hand-rolled submitted dict from the sync arm.

    The framework's handoff projection emits the EXACT 2-key dict
    ``{'task_id', 'status': 'submitted'}`` — that shape is produced ONLY by
    :func:`adcp.decisioning.dispatch._project_handoff` and is therefore
    already a legitimate registry-backed task. An adopter who instead returns
    a dict with ``status='submitted'`` plus other keys (or builds the
    submitted envelope by hand) bypassed the registry: ``tasks/get`` will
    404 and no completion webhook fires. Raise a guiding error.

    Pydantic ``GetProductsSubmitted`` / ``GetSignalsSubmitted`` instances
    returned directly are caught here too — they carry a ``task_id`` the
    framework never minted.

    :raises AdcpError: ``INVALID_REQUEST`` / ``correctable``.
    """
    status: Any = None
    if isinstance(result, dict):
        status = result.get("status")
    elif hasattr(result, "status"):
        status = getattr(result, "status", None)
    status_str = status.value if hasattr(status, "value") else status

    if status_str != "submitted":
        return
    # The framework's own projection — legitimate, leave it.
    if _is_submitted_projection(result):
        return
    raise AdcpError(
        "INVALID_REQUEST",
        message=(
            "Discovery returned a hand-rolled 'submitted' envelope. Do not "
            "construct the submitted arm yourself — the framework never "
            "issued a task for this task_id, so tasks/get would 404 and no "
            "completion webhook would fire. To run discovery asynchronously, "
            "return ctx.handoff_to_task(fn) (or ctx.handoff_to_workflow(fn) "
            "for adopter-owned external completion); the framework allocates "
            "the task_id, persists the submitted state, and emits the wire "
            "envelope for you."
        ),
        recovery="correctable",
    )


__all__ = [
    "assert_account_resolved_for_async",
    "assert_discovery_push_consistent",
    "reject_hand_rolled_submitted",
    "reject_wholesale_handoff",
]
