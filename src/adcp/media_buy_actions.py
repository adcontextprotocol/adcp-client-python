"""Buyer and seller helpers for proposal-bound MediaBuy change rights.

The helpers in this module deliberately keep three protocol surfaces separate:

* product ``allowed_actions`` are advisory possibilities;
* accepted proposal ``commercial_terms.change_terms`` are binding rights; and
* MediaBuy ``available_actions`` are the seller's current-state projection.

Legacy fields are readable, but never promoted into proposal authority.  In
particular, a 3.1 ``terms_ref`` is opaque unless this module itself emitted it
as the compatibility projection of a known ``change_term_id``.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field, RootModel, ValidationError, model_validator

from adcp.decisioning.state_machines import MEDIA_BUY_TRANSITIONS
from adcp.decisioning.update_media_buy import (
    UNKNOWN_UPDATE_ACTION,
    UpdateMediaBuyMutation,
    decompose_update_media_buy,
)
from adcp.types import MediaBuyChangeTerm
from adcp.types._str_enum import StrEnum

_ACTION_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_.:-]{0,127}$")
_TERM_ID_RE = re.compile(r"^[A-Za-z0-9_.:-]+$")
_NON_TERMINAL_STATUSES = frozenset(
    status for status, transitions in MEDIA_BUY_TRANSITIONS.items() if transitions
)
_TERMINAL_STATUSES = frozenset(
    status for status, transitions in MEDIA_BUY_TRANSITIONS.items() if not transitions
)

_CONTROL_ACTIONS = frozenset(
    {
        "pause",
        "resume",
        "cancel",
        "update_name",
        "increase_budget",
        "decrease_budget",
        "reallocate_budget",
        "update_budget_allocation",
        "update_targeting",
        "update_pacing",
        "update_bidding",
        "update_frequency_caps",
        "update_catalog_assignments",
        "update_keywords",
        "update_optimization_goals",
        "update_impression_goal",
        "update_spend_target",
        "update_reporting_webhook",
        "remove_packages",
    }
)
_REFINEMENT_ACTIONS = frozenset(
    {
        "cancel",
        "extend_flight",
        "shorten_flight",
        "update_flight_dates",
        "increase_budget",
        "decrease_budget",
        "reallocate_budget",
        "update_budget_allocation",
        "update_targeting",
        "update_pacing",
        "update_bidding",
        "update_frequency_caps",
        "add_packages",
        "remove_packages",
    }
)
_CREATIVE_ACTIONS = frozenset(
    {"replace_creative", "update_creative_assignments", "remove_creative"}
)
_CANONICAL_ACTIONS = _CONTROL_ACTIONS | _REFINEMENT_ACTIONS | _CREATIVE_ACTIONS

_ALLOWED_TASKS_BY_ACTION: dict[str, frozenset[ActionTask]]

_LEGACY_ROLLUPS: dict[str, frozenset[str]] = {
    "update_budget": frozenset(
        {"increase_budget", "decrease_budget", "reallocate_budget", "update_budget_allocation"}
    ),
    "update_dates": frozenset({"extend_flight", "shorten_flight", "update_flight_dates"}),
    "update_packages": frozenset(
        {
            "update_targeting",
            "update_pacing",
            "update_bidding",
            "update_frequency_caps",
            "reallocate_budget",
            "remove_packages",
        }
    ),
    "sync_creatives": _CREATIVE_ACTIONS,
}

_CONSTRAINT_ACTIONS: dict[str, frozenset[str]] = {
    "budget": frozenset(
        {
            "increase_budget",
            "decrease_budget",
            "reallocate_budget",
            "update_budget_allocation",
            "update_spend_target",
        }
    ),
    "flight": frozenset({"extend_flight", "shorten_flight", "update_flight_dates"}),
    "package_count": frozenset({"add_packages", "remove_packages"}),
    "effective_timing": frozenset({"pause", "resume", "cancel"}),
}


class ActionKnowledge(StrEnum):
    """Whether a protocol surface proves, disproves, or cannot answer a fact."""

    yes = "yes"
    no = "no"
    unknown = "unknown"


class ActionAvailabilityStatus(StrEnum):
    """Normalized buyer-facing action assessment result."""

    available_now = "available_now"
    wrong_status = "wrong_status"
    not_negotiated = "not_negotiated"
    unsupported_by_product = "unsupported_by_product"
    currently_unavailable = "currently_unavailable"
    legacy_unknown = "legacy_unknown"


class ActionTask(StrEnum):
    """Compact-lifecycle task used to exercise an action."""

    control_media_buy = "control_media_buy"
    refine_proposals = "refine_proposals"
    sync_creatives = "sync_creatives"


class ConstraintOutcome(StrEnum):
    """Result of preflighting one portable change-term constraint."""

    satisfied = "satisfied"
    violated = "violated"
    unknown = "unknown"


class ActionDiagnosticCode(StrEnum):
    """Stable machine-readable explanations emitted by the resolver."""

    alias_mismatch = "alias_mismatch"
    condition_unresolved = "condition_unresolved"
    constraint_violated = "constraint_violated"
    duplicate_action = "duplicate_action"
    duplicate_term_id = "duplicate_term_id"
    invalid_projection = "invalid_projection"
    legacy_coarse_action = "legacy_coarse_action"
    legacy_terms_unknown = "legacy_terms_unknown"
    missing_change_term_link = "missing_change_term_link"
    mode_mismatch = "mode_mismatch"
    route_mismatch = "route_mismatch"
    sla_mismatch = "sla_mismatch"
    unknown_action = "unknown_action"


class ActionDiagnostic(BaseModel):
    """Bounded diagnostic suitable for UI, logs, and agent reasoning."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    code: ActionDiagnosticCode
    field: str | None = Field(default=None, max_length=128, pattern=r"^[A-Za-z0-9_.\[\]-]+$")
    detail: str | None = Field(
        default=None,
        max_length=128,
        pattern=r"^[A-Za-z][A-Za-z0-9_.:-]{0,127}$",
    )


class ConstraintCheck(BaseModel):
    """Evaluation of one portable constraint field."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: str = Field(max_length=32, pattern=r"^[a-z_]+$")
    constraint: str = Field(max_length=64, pattern=r"^[a-z_]+$")
    outcome: ConstraintOutcome
    field: str | None = Field(default=None, max_length=128, pattern=r"^[A-Za-z0-9_.\[\]-]+$")


class ActionIntent(BaseModel):
    """Portable current/result state used to preflight typed constraints.

    Callers may provide this directly.  :func:`assess_update_media_buy_actions`
    derives the same fields from an update patch when enough current state is
    available.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    current_amount: Decimal | None = None
    result_amount: Decimal | None = None
    currency: str | None = Field(default=None, pattern=r"^[A-Z]{3}$")
    current_time: datetime | None = None
    result_time: datetime | None = None
    effective_at: datetime | None = None
    additions: int | None = Field(default=None, ge=0)
    removals: int | None = Field(default=None, ge=0)
    current_package_count: int | None = Field(default=None, ge=0)
    result_package_count: int | None = Field(default=None, ge=0)
    field: str | None = Field(default=None, max_length=128, pattern=r"^[A-Za-z0-9_.\[\]-]+$")


class MediaBuyActionAssessment(BaseModel):
    """The joined possible/promised/current answer for one action."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    action: str = Field(max_length=128, pattern=r"^[A-Za-z][A-Za-z0-9_.:-]{0,127}$")
    status: ActionAvailabilityStatus
    possible: ActionKnowledge
    promised: ActionKnowledge
    available: ActionKnowledge
    task: ActionTask | None = None
    mode: str | None = Field(default=None, max_length=64, pattern=r"^[A-Za-z][A-Za-z0-9_.:-]*$")
    change_term_id: str | None = Field(default=None, max_length=255, pattern=r"^[A-Za-z0-9_.:-]+$")
    async_processing: bool = False
    constraints: tuple[ConstraintCheck, ...] = ()
    diagnostics: tuple[ActionDiagnostic, ...] = ()


class ChangeTermSelection(BaseModel):
    """A seller's explicit decision to bind one advertised product action."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    action: str = Field(max_length=128, pattern=r"^[A-Za-z][A-Za-z0-9_.:-]{0,127}$")
    term_id: str = Field(max_length=255, pattern=r"^[A-Za-z0-9_.:-]+$")
    service_mode: str | None = Field(
        default=None, max_length=64, pattern=r"^[A-Za-z][A-Za-z0-9_.:-]*$"
    )
    allowed_statuses: tuple[str, ...] | None = None
    conditions: tuple[str, ...] | None = None
    terms_ref: str | None = Field(default=None, min_length=1, max_length=1000)
    description: str | None = Field(default=None, min_length=1, max_length=1000)

    @model_validator(mode="after")
    def _validate_values(self) -> ChangeTermSelection:
        if self.allowed_statuses is not None:
            if not self.allowed_statuses or len(set(self.allowed_statuses)) != len(
                self.allowed_statuses
            ):
                raise ValueError("allowed_statuses must be non-empty and unique")
            if any(status not in _NON_TERMINAL_STATUSES for status in self.allowed_statuses):
                raise ValueError("allowed_statuses may contain only non-terminal statuses")
        if self.conditions is not None:
            if not self.conditions or len(set(self.conditions)) != len(self.conditions):
                raise ValueError("conditions must be non-empty and unique")
            if any(
                re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,199}", condition) is None
                for condition in self.conditions
            ):
                raise ValueError("condition identifiers must use the protocol token grammar")
        return self


class ProjectedMediaBuyAction(BaseModel):
    """Version-adaptable seller projection for one currently available action."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    action: str = Field(max_length=128, pattern=r"^[A-Za-z][A-Za-z0-9_.:-]{0,127}$")
    mode: str = Field(max_length=64, pattern=r"^[A-Za-z][A-Za-z0-9_.:-]*$")
    task: ActionTask | None = None
    sla: dict[str, object] | None = None
    change_term_id: str | None = Field(default=None, max_length=255, pattern=r"^[A-Za-z0-9_.:-]+$")
    terms_ref: str | None = Field(default=None, max_length=1000)


class MediaBuyActionProjection(BaseModel):
    """Seller current-state projection plus any fail-closed omissions."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    actions: tuple[ProjectedMediaBuyAction, ...] = ()
    diagnostics: tuple[ActionDiagnostic, ...] = ()

    def to_wire(self) -> list[dict[str, object]]:
        """Return the JSON-ready ``available_actions`` array."""

        return [action.model_dump(mode="json", exclude_none=True) for action in self.actions]


class MediaBuyActionError(ValueError):
    """A safe local validation error for invalid seller helper inputs."""

    def __init__(self, code: str, field: str | None = None) -> None:
        self.code = code
        self.field = field
        suffix = f" at {field}" if field is not None else ""
        super().__init__(f"media-buy action validation failed: {code}{suffix}")


class ActionDispatchClient(Protocol):
    """Minimal client surface consumed by :func:`dispatch_media_buy_action`."""

    async def execute_task(
        self,
        task_name: str,
        request: BaseModel,
        *,
        options: Any | None = None,
    ) -> Any:
        raise NotImplementedError


def route_media_buy_action(action: str) -> ActionTask | None:
    """Return the canonical compact task for an in-envelope action.

    Flight and package-addition changes require proposal refinement; creative
    lifecycle changes use ``sync_creatives``; the remaining accepted controls
    use ``control_media_buy``.  Unknown future actions fail closed with
    ``None`` so callers do not guess a route.
    """

    if action in _CONTROL_ACTIONS:
        return ActionTask.control_media_buy
    if action in _REFINEMENT_ACTIONS:
        return ActionTask.refine_proposals
    if action in _CREATIVE_ACTIONS:
        return ActionTask.sync_creatives
    return None


_ALLOWED_TASKS_BY_ACTION = {
    action: frozenset(
        task
        for task, actions in (
            (ActionTask.control_media_buy, _CONTROL_ACTIONS),
            (
                ActionTask.refine_proposals,
                _REFINEMENT_ACTIONS,
            ),
            (ActionTask.sync_creatives, _CREATIVE_ACTIONS),
        )
        if action in actions
    )
    for action in _CANONICAL_ACTIONS
}


async def dispatch_media_buy_action(
    client: ActionDispatchClient,
    assessment: MediaBuyActionAssessment,
    request: BaseModel,
    *,
    options: Any | None = None,
) -> Any:
    """Dispatch an already-assessed action through its canonical task.

    The caller still supplies the task-specific generated request model.  This
    helper owns only the action-to-task decision and refuses unavailable or
    unknown actions.  ``seller_managed`` uses the same ordinary async task
    lifecycle as every other mode; no internal approval workflow is inferred.
    """

    if assessment.status is not ActionAvailabilityStatus.available_now:
        raise MediaBuyActionError("action_not_available", "status")
    if assessment.task is None:
        raise MediaBuyActionError("unknown_action_route", "task")
    return await client.execute_task(assessment.task.value, request, options=options)


def assess_media_buy_action(
    action: str,
    *,
    product: Any | None = None,
    proposal: Any | None = None,
    media_buy: Any | None = None,
    intent: ActionIntent | Mapping[str, Any] | None = None,
    now: datetime | None = None,
) -> MediaBuyActionAssessment:
    """Join product, accepted proposal, and live-buy action surfaces.

    Inputs may be generated Pydantic models or wire mappings.  Unknown future
    enum values and legacy coarse actions are retained as unknown evidence but
    are never treated as authority.
    """

    if _ACTION_RE.fullmatch(action) is None:
        raise MediaBuyActionError("invalid_action", "action")

    diagnostics: list[ActionDiagnostic] = []
    possible = _assess_product_support(action, product, diagnostics)
    change_terms_present, change_terms = _extract_change_terms(proposal, media_buy)
    term, term_diagnostics = _find_change_term(action, change_terms)
    diagnostics.extend(term_diagnostics)

    if not change_terms_present:
        promised = ActionKnowledge.unknown
        diagnostics.append(ActionDiagnostic(code=ActionDiagnosticCode.legacy_terms_unknown))
    elif term is None:
        promised = ActionKnowledge.no
    else:
        promised = ActionKnowledge.yes

    status_value = _string_field(media_buy, "status")
    wrong_status = False
    if term is not None and status_value is not None:
        allowed_statuses = _string_sequence(term.get("allowed_statuses"))
        wrong_status = status_value in _TERMINAL_STATUSES or (
            bool(allowed_statuses) and status_value not in allowed_statuses
        )

    live_state, live_action = _assess_live_action(action, media_buy, diagnostics)
    task: ActionTask | None = None
    mode: str | None = None
    change_term_id: str | None = None
    constraints: tuple[ConstraintCheck, ...] = ()

    if live_action is not None:
        task = _task_field(live_action) if "task" in live_action else route_media_buy_action(action)
        mode = _string_value(live_action.get("mode"))
        change_term_id = _string_value(live_action.get("change_term_id"))
        if term is not None and not _projection_matches_term(
            action, live_action, term, task, diagnostics
        ):
            live_state = ActionKnowledge.no
    elif term is not None:
        task = route_media_buy_action(action)
        mode = _string_value(term.get("service_mode"))
        change_term_id = _string_value(term.get("term_id"))

    if term is not None and intent is not None:
        parsed_intent = (
            intent if isinstance(intent, ActionIntent) else ActionIntent.model_validate(intent)
        )
        constraints = evaluate_action_constraints(
            action,
            term.get("constraints"),
            parsed_intent,
            now=now,
        )
        if any(check.outcome is ConstraintOutcome.violated for check in constraints):
            diagnostics.append(ActionDiagnostic(code=ActionDiagnosticCode.constraint_violated))
            live_state = ActionKnowledge.no

    integrity_failure = any(
        diagnostic.code
        in {
            ActionDiagnosticCode.alias_mismatch,
            ActionDiagnosticCode.duplicate_action,
            ActionDiagnosticCode.duplicate_term_id,
            ActionDiagnosticCode.invalid_projection,
            ActionDiagnosticCode.missing_change_term_link,
            ActionDiagnosticCode.mode_mismatch,
            ActionDiagnosticCode.route_mismatch,
            ActionDiagnosticCode.sla_mismatch,
        }
        for diagnostic in diagnostics
    )

    if action not in _CANONICAL_ACTIONS:
        diagnostics.append(ActionDiagnostic(code=ActionDiagnosticCode.unknown_action))
        final_status = ActionAvailabilityStatus.currently_unavailable
        live_state = ActionKnowledge.unknown
    elif integrity_failure:
        final_status = ActionAvailabilityStatus.currently_unavailable
        live_state = ActionKnowledge.no
    elif possible is ActionKnowledge.no:
        final_status = ActionAvailabilityStatus.unsupported_by_product
    elif promised is ActionKnowledge.no:
        final_status = ActionAvailabilityStatus.not_negotiated
    elif promised is ActionKnowledge.unknown:
        final_status = ActionAvailabilityStatus.legacy_unknown
    elif wrong_status:
        final_status = ActionAvailabilityStatus.wrong_status
        live_state = ActionKnowledge.no
    elif live_state is ActionKnowledge.yes:
        final_status = ActionAvailabilityStatus.available_now
    elif live_state is ActionKnowledge.unknown:
        final_status = ActionAvailabilityStatus.legacy_unknown
    else:
        final_status = ActionAvailabilityStatus.currently_unavailable

    return MediaBuyActionAssessment(
        action=action,
        status=final_status,
        possible=possible,
        promised=promised,
        available=live_state,
        task=task,
        mode=mode,
        change_term_id=change_term_id,
        async_processing=mode in {"seller_managed", "requires_approval"},
        constraints=constraints,
        diagnostics=tuple(_dedupe_diagnostics(diagnostics)),
    )


def assess_update_media_buy_actions(
    patch: Any,
    current_media_buy: Any,
    *,
    product: Any | None = None,
    proposal: Any | None = None,
    now: datetime | None = None,
) -> tuple[MediaBuyActionAssessment, ...]:
    """Assess every logical action represented by an update compatibility patch."""

    assessments: list[MediaBuyActionAssessment] = []
    for mutation in decompose_update_media_buy(patch, current_media_buy):
        if mutation.action == UNKNOWN_UPDATE_ACTION:
            continue
        intent = _intent_from_mutation(mutation, current_media_buy, now=now)
        assessments.append(
            assess_media_buy_action(
                mutation.action,
                product=product,
                proposal=proposal,
                media_buy=current_media_buy,
                intent=intent,
                now=now,
            )
        )
    return tuple(assessments)


def evaluate_action_constraints(
    action: str,
    constraints: Any | None,
    intent: ActionIntent,
    *,
    now: datetime | None = None,
) -> tuple[ConstraintCheck, ...]:
    """Evaluate portable constraints without interpreting opaque conditions."""

    constraint = _as_mapping(constraints)
    if not constraint:
        return ()
    kind = _string_value(constraint.get("kind"))
    if kind is None or action not in _CONSTRAINT_ACTIONS.get(kind, frozenset()):
        return (
            ConstraintCheck(
                kind=kind or "unknown",
                constraint="kind",
                outcome=ConstraintOutcome.unknown,
                field=intent.field,
            ),
        )

    if kind == "budget":
        return _evaluate_budget_constraints(constraint, intent)
    if kind == "flight":
        return _evaluate_flight_constraints(constraint, intent, now=now)
    if kind == "package_count":
        return _evaluate_package_constraints(constraint, intent)
    if kind == "effective_timing":
        return _evaluate_effective_timing_constraints(constraint, intent, now=now)
    return ()


def reassess_media_buy_action(
    action: str,
    *,
    currently_available_actions: Iterable[Any],
    media_buy: Any,
    product: Any | None = None,
    proposal: Any | None = None,
    intent: ActionIntent | Mapping[str, Any] | None = None,
    now: datetime | None = None,
) -> MediaBuyActionAssessment:
    """Reassess against an authoritative action-set echo after a race.

    ``ACTION_NOT_ALLOWED`` may carry ``currently_available_actions`` resolved
    at rejection time.  This helper replaces only the stale action projection;
    proposal rights and all other MediaBuy state remain unchanged.  Callers
    should still refresh the full MediaBuy before retrying with a new revision.
    """

    refreshed = dict(_as_mapping(media_buy))
    refreshed["available_actions"] = [
        dict(_as_mapping(value)) for value in currently_available_actions
    ]
    return assess_media_buy_action(
        action,
        product=product,
        proposal=proposal,
        media_buy=refreshed,
        intent=intent,
        now=now,
    )


def materialize_change_terms(
    product_actions: Iterable[Any],
    selections: Iterable[ChangeTermSelection | Mapping[str, Any]],
) -> tuple[MediaBuyChangeTerm, ...]:
    """Materialize explicitly selected product templates as binding terms.

    Product declarations are never copied wholesale.  Every returned term
    requires a :class:`ChangeTermSelection`, and all portable bounds, SLA, and
    status scope are copied from (or narrowed relative to) that template.
    """

    product_index = _unique_action_index(product_actions, field="product_actions")
    parsed = [
        (
            value
            if isinstance(value, ChangeTermSelection)
            else ChangeTermSelection.model_validate(value)
        )
        for value in selections
    ]
    if len({selection.action for selection in parsed}) != len(parsed):
        raise MediaBuyActionError("duplicate_action", "selections")
    if len({selection.term_id for selection in parsed}) != len(parsed):
        raise MediaBuyActionError("duplicate_term_id", "selections")

    terms: list[MediaBuyChangeTerm] = []
    for index, selection in enumerate(parsed):
        product_action = product_index.get(selection.action)
        if product_action is None:
            raise MediaBuyActionError("action_not_advertised", f"selections[{index}].action")
        if selection.action not in _CANONICAL_ACTIONS:
            raise MediaBuyActionError("unknown_action", f"selections[{index}].action")

        modes = _string_sequence(product_action.get("modes"))
        mode = selection.service_mode
        if mode is None:
            if len(modes) != 1:
                raise MediaBuyActionError(
                    "service_mode_required", f"selections[{index}].service_mode"
                )
            mode = modes[0]
        if mode not in modes:
            raise MediaBuyActionError(
                "service_mode_not_advertised", f"selections[{index}].service_mode"
            )

        product_statuses = _string_sequence(product_action.get("allowed_statuses"))
        selected_statuses = selection.allowed_statuses
        if (
            selected_statuses is not None
            and product_statuses
            and not set(selected_statuses) <= set(product_statuses)
        ):
            raise MediaBuyActionError(
                "status_scope_expanded", f"selections[{index}].allowed_statuses"
            )
        statuses = selected_statuses or (tuple(product_statuses) if product_statuses else None)

        constraint = _as_mapping(product_action.get("constraints")) or None
        if constraint is not None:
            kind = _string_value(constraint.get("kind"))
            if selection.action not in _CONSTRAINT_ACTIONS.get(kind or "", frozenset()):
                raise MediaBuyActionError(
                    "constraint_action_mismatch",
                    f"product_actions[{index}].constraints",
                )

        term_payload: dict[str, Any] = {
            "term_id": selection.term_id,
            "action": selection.action,
            "service_mode": mode,
        }
        if statuses is not None:
            term_payload["allowed_statuses"] = list(statuses)
        sla = _as_mapping(product_action.get("sla"))
        if sla:
            term_payload["processing_sla"] = sla
        if constraint is not None:
            term_payload["constraints"] = constraint
        if selection.conditions is not None:
            term_payload["conditions"] = list(selection.conditions)
        terms_ref = selection.terms_ref or _string_value(product_action.get("terms_ref"))
        if terms_ref is not None:
            term_payload["terms_ref"] = terms_ref
        if selection.description is not None:
            term_payload["description"] = selection.description
        terms.append(MediaBuyChangeTerm.model_validate(term_payload))
    return tuple(terms)


def project_available_actions(
    change_terms: Iterable[Any] | None,
    status: str,
    *,
    product_actions: Iterable[Any] | None = None,
    authorized_actions: Iterable[str] | None = None,
    delegated_actions: Iterable[str] | None = None,
    policy_actions: Iterable[str] | None = None,
    resolved_conditions: Mapping[str, bool] | None = None,
    protocol_version: str = "3.2",
) -> MediaBuyActionProjection:
    """Derive a fail-closed seller ``available_actions`` projection.

    ``None`` for an optional narrowing set means that gate has already been
    satisfied or is not applicable.  An explicit empty set denies every
    action.  Conditions must be explicitly resolved ``True``; absent, false,
    or unknown conditions omit the action.
    """

    diagnostics: list[ActionDiagnostic] = []
    if change_terms is None:
        return MediaBuyActionProjection(
            diagnostics=(ActionDiagnostic(code=ActionDiagnosticCode.legacy_terms_unknown),)
        )
    if status not in _NON_TERMINAL_STATUSES:
        return MediaBuyActionProjection()

    terms: list[Mapping[str, Any]] = []
    for index, raw_term in enumerate(change_terms):
        try:
            parsed_term = MediaBuyChangeTerm.model_validate(raw_term)
        except ValidationError:
            diagnostics.append(
                ActionDiagnostic(
                    code=ActionDiagnosticCode.invalid_projection,
                    field=f"change_terms[{index}]",
                )
            )
            continue
        terms.append(parsed_term.model_dump(mode="python", by_alias=True, exclude_unset=True))
    seen_actions: set[str] = set()
    seen_ids: set[str] = set()
    product_index = (
        _unique_action_index(product_actions, field="product_actions")
        if product_actions is not None
        else None
    )
    gates = [
        set(values) if values is not None else None
        for values in (authorized_actions, delegated_actions, policy_actions)
    ]
    supports_link = _supports_change_term_id(protocol_version)
    supports_task = _supports_action_task(protocol_version)
    actions: list[ProjectedMediaBuyAction] = []

    for index, term in enumerate(terms):
        action = _string_value(term.get("action"))
        term_id = _string_value(term.get("term_id"))
        if action is None or term_id is None or _TERM_ID_RE.fullmatch(term_id) is None:
            diagnostics.append(
                ActionDiagnostic(
                    code=ActionDiagnosticCode.invalid_projection,
                    field=f"change_terms[{index}]",
                )
            )
            continue
        if action in seen_actions:
            diagnostics.append(_diagnostic(ActionDiagnosticCode.duplicate_action, detail=action))
            continue
        if term_id in seen_ids:
            diagnostics.append(_diagnostic(ActionDiagnosticCode.duplicate_term_id, detail=term_id))
            continue
        seen_actions.add(action)
        seen_ids.add(term_id)

        task = route_media_buy_action(action)
        if task is None:
            diagnostics.append(_diagnostic(ActionDiagnosticCode.unknown_action, detail=action))
            continue
        allowed_statuses = _string_sequence(term.get("allowed_statuses"))
        if allowed_statuses and status not in allowed_statuses:
            continue
        if any(gate is not None and action not in gate for gate in gates):
            continue

        product_action = product_index.get(action) if product_index is not None else None
        if product_index is not None and product_action is None:
            continue
        mode = _string_value(term.get("service_mode"))
        if mode is None:
            diagnostics.append(
                ActionDiagnostic(
                    code=ActionDiagnosticCode.invalid_projection,
                    field=f"change_terms[{index}].service_mode",
                )
            )
            continue
        if product_action is not None:
            if mode not in _string_sequence(product_action.get("modes")):
                continue
            product_statuses = _string_sequence(product_action.get("allowed_statuses"))
            if product_statuses and status not in product_statuses:
                continue

        conditions = _string_sequence(term.get("conditions"))
        if conditions and (
            resolved_conditions is None
            or any(resolved_conditions.get(condition) is not True for condition in conditions)
        ):
            diagnostics.append(
                _diagnostic(ActionDiagnosticCode.condition_unresolved, detail=action)
            )
            continue

        constraint = _as_mapping(term.get("constraints"))
        if constraint:
            kind = _string_value(constraint.get("kind"))
            if action not in _CONSTRAINT_ACTIONS.get(kind or "", frozenset()):
                diagnostics.append(
                    _diagnostic(ActionDiagnosticCode.invalid_projection, detail=action)
                )
                continue

        wire_mode = "requires_approval" if not supports_link and mode == "seller_managed" else mode
        action_payload: dict[str, Any] = {
            "action": action,
            "mode": wire_mode,
            "sla": _as_mapping(term.get("processing_sla")) or None,
        }
        if supports_task:
            action_payload["task"] = task
        if supports_link:
            action_payload["change_term_id"] = term_id
        else:
            # This is an explicit adapter-generated alias.  Arbitrary inbound
            # 3.1 terms_ref values are never interpreted this way.
            action_payload["terms_ref"] = term_id
        actions.append(ProjectedMediaBuyAction.model_validate(action_payload))

    return MediaBuyActionProjection(
        actions=tuple(actions), diagnostics=tuple(_dedupe_diagnostics(diagnostics))
    )


def _assess_product_support(
    action: str,
    product: Any | None,
    diagnostics: list[ActionDiagnostic],
) -> ActionKnowledge:
    if product is None:
        return ActionKnowledge.unknown
    product_mapping = _as_mapping(product)
    if not _has_field(product, "allowed_actions"):
        return ActionKnowledge.unknown
    declarations = _as_sequence(product_mapping.get("allowed_actions"))
    declared = {_string_field(value, "action") for value in declarations}
    if action in declared:
        return ActionKnowledge.yes
    for coarse, fine_actions in _LEGACY_ROLLUPS.items():
        if coarse in declared and action in fine_actions:
            diagnostics.append(
                _diagnostic(ActionDiagnosticCode.legacy_coarse_action, detail=coarse)
            )
            return ActionKnowledge.unknown
    return ActionKnowledge.no


def _extract_change_terms(
    proposal: Any | None,
    media_buy: Any | None,
) -> tuple[bool, list[Mapping[str, Any]]]:
    source = proposal
    if source is None and media_buy is not None:
        media_mapping = _as_mapping(media_buy)
        source = media_mapping.get("accepted_proposal")
    if source is None:
        return False, []
    source_mapping = _as_mapping(source)
    if "commercial_terms" in source_mapping:
        source = source_mapping.get("commercial_terms")
        if source is None:
            return False, []
    terms_mapping = _as_mapping(source)
    if not _has_field(source, "change_terms"):
        return False, []
    return True, [_as_mapping(value) for value in _as_sequence(terms_mapping.get("change_terms"))]


def _find_change_term(
    action: str,
    terms: Sequence[Mapping[str, Any]],
) -> tuple[Mapping[str, Any] | None, list[ActionDiagnostic]]:
    matches = [term for term in terms if _string_value(term.get("action")) == action]
    diagnostics: list[ActionDiagnostic] = []
    if len(matches) > 1:
        diagnostics.append(_diagnostic(ActionDiagnosticCode.duplicate_action, detail=action))
        return None, diagnostics
    ids = [_string_value(term.get("term_id")) for term in terms]
    concrete_ids = [value for value in ids if value is not None]
    if len(set(concrete_ids)) != len(concrete_ids):
        diagnostics.append(ActionDiagnostic(code=ActionDiagnosticCode.duplicate_term_id))
        return None, diagnostics
    return (matches[0] if matches else None), diagnostics


def _assess_live_action(
    action: str,
    media_buy: Any | None,
    diagnostics: list[ActionDiagnostic],
) -> tuple[ActionKnowledge, Mapping[str, Any] | None]:
    if media_buy is None:
        return ActionKnowledge.unknown, None
    media_mapping = _as_mapping(media_buy)
    if _has_field(media_buy, "available_actions"):
        matches = [
            _as_mapping(value)
            for value in _as_sequence(media_mapping.get("available_actions"))
            if _string_field(value, "action") == action
        ]
        if len(matches) > 1:
            diagnostics.append(_diagnostic(ActionDiagnosticCode.duplicate_action, detail=action))
            return ActionKnowledge.no, None
        return (ActionKnowledge.yes, matches[0]) if matches else (ActionKnowledge.no, None)

    valid_actions = _string_sequence(media_mapping.get("valid_actions"))
    if action in valid_actions or any(
        coarse in valid_actions and action in fine for coarse, fine in _LEGACY_ROLLUPS.items()
    ):
        diagnostics.append(ActionDiagnostic(code=ActionDiagnosticCode.legacy_coarse_action))
    return ActionKnowledge.unknown, None


def _projection_matches_term(
    action: str,
    live_action: Mapping[str, Any],
    term: Mapping[str, Any],
    task: ActionTask | None,
    diagnostics: list[ActionDiagnostic],
) -> bool:
    valid = True
    term_id = _string_value(term.get("term_id"))
    change_term_id = _string_value(live_action.get("change_term_id"))
    terms_ref = _string_value(live_action.get("terms_ref"))
    if change_term_id is not None and terms_ref is not None and change_term_id != terms_ref:
        diagnostics.append(ActionDiagnostic(code=ActionDiagnosticCode.alias_mismatch))
        valid = False
    if change_term_id is not None and change_term_id != term_id:
        diagnostics.append(ActionDiagnostic(code=ActionDiagnosticCode.missing_change_term_link))
        valid = False
    if change_term_id is None and terms_ref is not None:
        # An arbitrary legacy terms_ref stays opaque.  It neither proves nor
        # disproves the proposal link.
        pass
    elif change_term_id is None:
        diagnostics.append(ActionDiagnostic(code=ActionDiagnosticCode.missing_change_term_link))
        valid = False

    live_mode = _string_value(live_action.get("mode"))
    term_mode = _string_value(term.get("service_mode"))
    modes_match = live_mode == term_mode or (
        term_mode == "seller_managed"
        and live_mode == "requires_approval"
        and change_term_id is None
    )
    if not modes_match:
        diagnostics.append(ActionDiagnostic(code=ActionDiagnosticCode.mode_mismatch))
        valid = False

    live_task = _string_value(live_action.get("task"))
    if "task" in live_action and live_task is None:
        diagnostics.append(ActionDiagnostic(code=ActionDiagnosticCode.route_mismatch))
        valid = False
    elif live_task is not None:
        try:
            parsed_task = ActionTask(live_task)
        except ValueError:
            parsed_task = None
        if (
            parsed_task is None
            or task is None
            or parsed_task is not task
            or parsed_task not in _ALLOWED_TASKS_BY_ACTION.get(action, frozenset())
        ):
            diagnostics.append(ActionDiagnostic(code=ActionDiagnosticCode.route_mismatch))
            valid = False

    if _normalized_value(live_action.get("sla")) != _normalized_value(term.get("processing_sla")):
        diagnostics.append(ActionDiagnostic(code=ActionDiagnosticCode.sla_mismatch))
        valid = False
    return valid


def _evaluate_budget_constraints(
    constraint: Mapping[str, Any], intent: ActionIntent
) -> tuple[ConstraintCheck, ...]:
    checks: list[ConstraintCheck] = []
    current = intent.current_amount
    result = intent.result_amount
    delta = abs(result - current) if current is not None and result is not None else None

    if "max_delta_amount" in constraint:
        money = _as_mapping(constraint.get("max_delta_amount"))
        limit = _decimal(money.get("amount"))
        outcome = _money_outcome(
            delta,
            limit,
            intent.currency,
            _string_value(money.get("currency")),
        )
        checks.append(_check("budget", "max_delta_amount", outcome, intent.field))
    if "max_delta_percent" in constraint:
        limit_percent = _decimal(constraint.get("max_delta_percent"))
        if current is None or result is None or limit_percent is None:
            outcome = ConstraintOutcome.unknown
        elif current == 0:
            outcome = ConstraintOutcome.satisfied if result == 0 else ConstraintOutcome.violated
        else:
            percent = abs(result - current) * Decimal(100) / abs(current)
            outcome = (
                ConstraintOutcome.satisfied
                if percent <= limit_percent
                else ConstraintOutcome.violated
            )
        checks.append(_check("budget", "max_delta_percent", outcome, intent.field))
    for name, comparator in (
        ("min_result_amount", "minimum"),
        ("max_result_amount", "maximum"),
    ):
        if name not in constraint:
            continue
        money = _as_mapping(constraint.get(name))
        bound = _decimal(money.get("amount"))
        currency_matches = _currencies_match(intent.currency, _string_value(money.get("currency")))
        if result is None or bound is None or currency_matches is None:
            outcome = ConstraintOutcome.unknown
        elif not currency_matches:
            outcome = ConstraintOutcome.violated
        elif comparator == "minimum":
            outcome = ConstraintOutcome.satisfied if result >= bound else ConstraintOutcome.violated
        else:
            outcome = ConstraintOutcome.satisfied if result <= bound else ConstraintOutcome.violated
        checks.append(_check("budget", name, outcome, intent.field))
    return tuple(checks)


def _evaluate_flight_constraints(
    constraint: Mapping[str, Any],
    intent: ActionIntent,
    *,
    now: datetime | None,
) -> tuple[ConstraintCheck, ...]:
    checks: list[ConstraintCheck] = []
    current = _aware(intent.current_time)
    result = _aware(intent.result_time)
    if "max_change" in constraint:
        maximum = _duration(constraint.get("max_change"))
        if current is None or result is None or maximum is None:
            outcome = ConstraintOutcome.unknown
        else:
            outcome = (
                ConstraintOutcome.satisfied
                if abs(result - current) <= maximum
                else ConstraintOutcome.violated
            )
        checks.append(_check("flight", "max_change", outcome, intent.field))
    for name, lower in (("earliest_result", True), ("latest_result", False)):
        if name not in constraint:
            continue
        bound = _datetime(constraint.get(name))
        if result is None or bound is None:
            outcome = ConstraintOutcome.unknown
        elif (result >= bound) if lower else (result <= bound):
            outcome = ConstraintOutcome.satisfied
        else:
            outcome = ConstraintOutcome.violated
        checks.append(_check("flight", name, outcome, intent.field))
    if "minimum_notice" in constraint:
        minimum = _duration(constraint.get("minimum_notice"))
        effective = result
        clock = _aware(now or datetime.now(timezone.utc))
        if minimum is None or effective is None or clock is None:
            outcome = ConstraintOutcome.unknown
        else:
            outcome = (
                ConstraintOutcome.satisfied
                if effective - clock >= minimum
                else ConstraintOutcome.violated
            )
        checks.append(_check("flight", "minimum_notice", outcome, intent.field))
    return tuple(checks)


def _evaluate_package_constraints(
    constraint: Mapping[str, Any], intent: ActionIntent
) -> tuple[ConstraintCheck, ...]:
    checks: list[ConstraintCheck] = []
    for name, actual in (
        ("max_additions", intent.additions),
        ("max_removals", intent.removals),
        ("max_result_count", intent.result_package_count),
    ):
        if name not in constraint:
            continue
        limit = constraint.get(name)
        if not isinstance(limit, int) or actual is None:
            outcome = ConstraintOutcome.unknown
        else:
            outcome = ConstraintOutcome.satisfied if actual <= limit else ConstraintOutcome.violated
        checks.append(_check("package_count", name, outcome, intent.field))
    return tuple(checks)


def _evaluate_effective_timing_constraints(
    constraint: Mapping[str, Any],
    intent: ActionIntent,
    *,
    now: datetime | None,
) -> tuple[ConstraintCheck, ...]:
    checks: list[ConstraintCheck] = []
    effective = _aware(intent.effective_at)
    clock = _aware(now or datetime.now(timezone.utc))
    if "minimum_notice" in constraint:
        minimum = _duration(constraint.get("minimum_notice"))
        if effective is None or clock is None or minimum is None:
            outcome = ConstraintOutcome.unknown
        else:
            outcome = (
                ConstraintOutcome.satisfied
                if effective - clock >= minimum
                else ConstraintOutcome.violated
            )
        checks.append(_check("effective_timing", "minimum_notice", outcome, intent.field))
    for name, lower in (("earliest_effective_at", True), ("latest_effective_at", False)):
        if name not in constraint:
            continue
        bound = _datetime(constraint.get(name))
        if effective is None or bound is None:
            outcome = ConstraintOutcome.unknown
        elif (effective >= bound) if lower else (effective <= bound):
            outcome = ConstraintOutcome.satisfied
        else:
            outcome = ConstraintOutcome.violated
        checks.append(_check("effective_timing", name, outcome, intent.field))
    return tuple(checks)


def _intent_from_mutation(
    mutation: UpdateMediaBuyMutation,
    current_media_buy: Any,
    *,
    now: datetime | None,
) -> ActionIntent:
    current = _as_mapping(current_media_buy)
    current_amount: Decimal | None = None
    result_amount: Decimal | None = None
    currency: str | None = None
    current_time: datetime | None = None
    result_time: datetime | None = None
    additions: int | None = None
    removals: int | None = None
    result_count: int | None = None
    effective_at: datetime | None = None

    if mutation.action in _CONSTRAINT_ACTIONS["budget"]:
        current_amount = _amount(mutation.before)
        result_amount = _amount(mutation.after)
        currency = _money_currency(mutation.after) or _money_currency(mutation.before)
        if (
            isinstance(mutation.before, Mapping)
            and isinstance(mutation.after, Mapping)
            and "amount" not in mutation.before
            and "amount" not in mutation.after
        ):
            current_amount = sum(
                ((_decimal(value) or Decimal(0)) for value in mutation.before.values()),
                start=Decimal(0),
            )
            result_amount = sum(
                ((_decimal(value) or Decimal(0)) for value in mutation.after.values()),
                start=Decimal(0),
            )
    elif mutation.action in _CONSTRAINT_ACTIONS["flight"]:
        current_time = _time_from_mutation(mutation.before)
        result_time = _time_from_mutation(mutation.after)
    elif mutation.action == "add_packages":
        additions = len(_as_sequence(mutation.after))
    elif mutation.action == "remove_packages":
        removals = 1
    elif mutation.action in _CONSTRAINT_ACTIONS["effective_timing"]:
        effective_at = now or datetime.now(timezone.utc)

    packages = _as_sequence(current.get("packages"))
    current_count = len(
        [package for package in packages if _string_field(package, "status") != "canceled"]
    )
    if additions is not None:
        result_count = current_count + additions
    elif removals is not None:
        result_count = max(0, current_count - removals)

    return ActionIntent(
        current_amount=current_amount,
        result_amount=result_amount,
        currency=currency or _string_value(current.get("currency")),
        current_time=current_time,
        result_time=result_time,
        effective_at=effective_at,
        additions=additions,
        removals=removals,
        current_package_count=current_count,
        result_package_count=result_count,
        field=mutation.field_paths[0] if mutation.field_paths else None,
    )


def _unique_action_index(values: Iterable[Any], *, field: str) -> dict[str, Mapping[str, Any]]:
    result: dict[str, Mapping[str, Any]] = {}
    for index, value in enumerate(values):
        mapped = _as_mapping(value)
        action = _string_value(mapped.get("action"))
        if action is None:
            raise MediaBuyActionError("missing_action", f"{field}[{index}].action")
        if action in result:
            raise MediaBuyActionError("duplicate_action", f"{field}[{index}].action")
        result[action] = mapped
    return result


def _supports_change_term_id(version: str) -> bool:
    match = re.fullmatch(r"v?(\d+)\.(\d+)(?:\.\d+)?(?:-beta\.(\d+))?", version)
    if match is None:
        raise MediaBuyActionError("invalid_protocol_version", "protocol_version")
    major, minor = int(match.group(1)), int(match.group(2))
    beta = int(match.group(3)) if match.group(3) is not None else None
    if (major, minor) < (3, 2):
        return False
    return beta is None or beta >= 9


def _supports_action_task(version: str) -> bool:
    match = re.fullmatch(r"v?(\d+)\.(\d+)(?:\.\d+)?(?:-beta\.(\d+))?", version)
    if match is None:
        raise MediaBuyActionError("invalid_protocol_version", "protocol_version")
    return (int(match.group(1)), int(match.group(2))) >= (3, 2)


def _task_field(value: Mapping[str, Any]) -> ActionTask | None:
    raw = _string_value(value.get("task"))
    if raw is None:
        return None
    try:
        return ActionTask(raw)
    except ValueError:
        return None


def _as_mapping(value: Any) -> Mapping[str, Any]:
    if value is None:
        return {}
    if isinstance(value, RootModel):
        return _as_mapping(value.root)
    if isinstance(value, BaseModel):
        return value.model_dump(mode="python", by_alias=True, exclude_unset=True)
    if isinstance(value, Mapping):
        return value
    return {}


def _as_sequence(value: Any) -> list[Any]:
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return list(value)
    return []


def _has_field(value: Any, field: str) -> bool:
    if isinstance(value, BaseModel):
        return field in value.model_fields_set
    return isinstance(value, Mapping) and field in value


def _string_value(value: Any) -> str | None:
    if isinstance(value, RootModel):
        return _string_value(value.root)
    if isinstance(value, StrEnum):
        return str(value.value)
    if isinstance(value, str):
        return value
    return None


def _string_field(value: Any, field: str) -> str | None:
    return _string_value(_as_mapping(value).get(field))


def _string_sequence(value: Any) -> tuple[str, ...]:
    result: list[str] = []
    for item in _as_sequence(value):
        string = _string_value(item)
        if string is not None:
            result.append(string)
    return tuple(result)


def _normalized_value(value: Any) -> Any:
    if isinstance(value, RootModel):
        return _normalized_value(value.root)
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json", exclude_none=True)
    if isinstance(value, Mapping):
        return {str(key): _normalized_value(item) for key, item in sorted(value.items())}
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [_normalized_value(item) for item in value]
    if isinstance(value, StrEnum):
        return value.value
    if isinstance(value, datetime):
        return value.isoformat()
    return value


def _check(kind: str, name: str, outcome: ConstraintOutcome, field: str | None) -> ConstraintCheck:
    return ConstraintCheck(kind=kind, constraint=name, outcome=outcome, field=field)


def _diagnostic(
    code: ActionDiagnosticCode,
    *,
    detail: str | None = None,
    field: str | None = None,
) -> ActionDiagnostic:
    safe_detail = detail if detail is not None and _ACTION_RE.fullmatch(detail) else None
    return ActionDiagnostic(code=code, detail=safe_detail, field=field)


def _decimal(value: Any) -> Decimal | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def _amount(value: Any) -> Decimal | None:
    mapped = _as_mapping(value)
    if mapped:
        return _decimal(mapped.get("amount"))
    return _decimal(value)


def _money_currency(value: Any) -> str | None:
    return _string_value(_as_mapping(value).get("currency"))


def _currencies_match(left: str | None, right: str | None) -> bool | None:
    if left is None or right is None:
        return None
    return left == right


def _money_outcome(
    actual: Decimal | None,
    limit: Decimal | None,
    actual_currency: str | None,
    limit_currency: str | None,
) -> ConstraintOutcome:
    currencies_match = _currencies_match(actual_currency, limit_currency)
    if actual is None or limit is None or currencies_match is None:
        return ConstraintOutcome.unknown
    if not currencies_match:
        return ConstraintOutcome.violated
    return ConstraintOutcome.satisfied if actual <= limit else ConstraintOutcome.violated


def _duration(value: Any) -> timedelta | None:
    mapped = _as_mapping(value)
    interval = mapped.get("interval")
    unit = _string_value(mapped.get("unit"))
    if not isinstance(interval, int) or isinstance(interval, bool) or interval < 1:
        return None
    if unit == "seconds":
        return timedelta(seconds=interval)
    if unit == "minutes":
        return timedelta(minutes=interval)
    if unit == "hours":
        return timedelta(hours=interval)
    if unit == "days":
        return timedelta(days=interval)
    return None


def _datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return _aware(value)
    if isinstance(value, str):
        try:
            return _aware(datetime.fromisoformat(value.replace("Z", "+00:00")))
        except ValueError:
            return None
    return None


def _aware(value: datetime | None) -> datetime | None:
    if value is None or value.tzinfo is None or value.utcoffset() is None:
        return None
    return value.astimezone(timezone.utc)


def _time_from_mutation(value: Any) -> datetime | None:
    mapped = _as_mapping(value)
    if mapped:
        for field in ("end_time", "start_time"):
            if field in mapped:
                return _datetime(mapped[field])
        return None
    return _datetime(value)


def _dedupe_diagnostics(values: Iterable[ActionDiagnostic]) -> list[ActionDiagnostic]:
    result: list[ActionDiagnostic] = []
    seen: set[tuple[str, str | None, str | None]] = set()
    for value in values:
        key = (value.code.value, value.field, value.detail)
        if key not in seen:
            seen.add(key)
            result.append(value)
    return result


__all__ = [
    "ActionAvailabilityStatus",
    "ActionDiagnostic",
    "ActionDiagnosticCode",
    "ActionDispatchClient",
    "ActionIntent",
    "ActionKnowledge",
    "ActionTask",
    "ChangeTermSelection",
    "ConstraintCheck",
    "ConstraintOutcome",
    "MediaBuyActionAssessment",
    "MediaBuyActionError",
    "MediaBuyActionProjection",
    "ProjectedMediaBuyAction",
    "assess_media_buy_action",
    "assess_update_media_buy_actions",
    "dispatch_media_buy_action",
    "evaluate_action_constraints",
    "materialize_change_terms",
    "project_available_actions",
    "route_media_buy_action",
]
