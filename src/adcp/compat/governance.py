"""Lossless governance vocabulary changes at the AdCP 3.0/3.1 boundary."""

from __future__ import annotations

from typing import Any

from adcp.exceptions import ADCPFeatureUnsupportedError

_VERDICTS = frozenset({"approved", "denied", "conditions"})
_OUTCOME_STATES = frozenset({"accepted", "findings"})


def _upgrade_field(
    payload: dict[str, Any], field: str, values: frozenset[str], pointer: str
) -> None:
    value = payload.get("status")
    if not isinstance(value, str) or value not in values:
        return
    if field in payload and payload[field] != value:
        raise ADCPFeatureUnsupportedError(
            [f"Conflicting legacy and current governance fields at {pointer}/{field}"]
        )
    payload[field] = payload.pop("status")


def convert_governance_fields(
    tool: str,
    payload: dict[str, Any],
    source_version: str,
    target_version: str,
    direction: str,
) -> None:
    """Mutate one adjacent response hop before generic envelope projection.

    AdCP 3.1 reserves top-level ``status`` for task execution. Governance
    decisions moved to ``verdict`` and outcome acknowledgements moved to
    ``outcome_state``. Audit check entries use the same verdict vocabulary;
    plan and governed-action lifecycle statuses keep their original meaning.
    """
    if direction == "request" or {source_version, target_version} != {"3.0", "3.1"}:
        return
    upgrading = source_version == "3.0"
    if tool in {"check_governance", "report_plan_outcome"}:
        field = "verdict" if tool == "check_governance" else "outcome_state"
        if upgrading:
            _upgrade_field(
                payload, field, _VERDICTS if tool == "check_governance" else _OUTCOME_STATES, ""
            )
        elif field in payload:
            payload["status"] = payload.pop(field)
    elif tool == "get_plan_audit_logs":
        plans = payload.get("plans")
        if not isinstance(plans, list):
            return
        for plan_index, plan in enumerate(plans):
            if not isinstance(plan, dict):
                continue
            entries = plan.get("entries")
            if not isinstance(entries, list):
                continue
            for entry_index, entry in enumerate(entries):
                if not isinstance(entry, dict) or entry.get("type") != "check":
                    continue
                if upgrading:
                    _upgrade_field(
                        entry,
                        "verdict",
                        _VERDICTS,
                        f"/plans/{plan_index}/entries/{entry_index}",
                    )
                elif "verdict" in entry:
                    entry["status"] = entry.pop("verdict")
